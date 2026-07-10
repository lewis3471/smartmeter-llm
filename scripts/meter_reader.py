#!/usr/bin/env python3
"""Nulleinspeisung: ESP32-Cam -> Gemini Vision -> Plausibilitätsfilter
-> MQTT (Home Assistant Logging) -> OpenDTU Limit-Regelung.

Läuft als Endlosschleife im INTERVAL_S-Takt (Free Tier: 1000 req/Tag).
"""

import asyncio
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

try:
    import paho.mqtt.publish as mqtt_publish
except ImportError:
    mqtt_publish = None

try:
    from aioesphomeapi import APIClient
except ImportError:
    APIClient = None

# --- Konfiguration aus .env / Environment ---
def load_env():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ESPHOME_HOST = os.environ.get("ESPHOME_HOST", "")
ESPHOME_API_KEY = os.environ.get("ESPHOME_API_KEY", "")
CAM_WARMUP_S = float(os.environ.get("CAM_WARMUP_S", "3.5"))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_PROMPT = os.environ.get("GEMINI_PROMPT", 'Lies das LCD. Antworte nur: {"kwh":int,"w":int}')
CAM_SNAPSHOT_URL = os.environ["CAM_SNAPSHOT_URL"]
OPENDTU_URL = os.environ["OPENDTU_URL"].rstrip("/")
OPENDTU_AUTH = (os.environ["OPENDTU_USER"], os.environ["OPENDTU_PASS"])
INVERTER_SERIAL = os.environ.get("INVERTER_SERIAL", "")

MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_AUTH = (
    {"username": os.environ["MQTT_USER"], "password": os.environ["MQTT_PASS"]}
    if os.environ.get("MQTT_USER") and os.environ.get("MQTT_USER") != "CHANGE_ME"
    else None
)
TOPIC = os.environ.get("MQTT_TOPIC_PREFIX", "smartmeter")

INTERVAL_S = int(os.environ.get("INTERVAL_S", "90"))
TARGET_GRID_W = int(os.environ.get("TARGET_GRID_W", "50"))
MAX_STEP_W = int(os.environ.get("MAX_STEP_W", "200"))
HYSTERESIS_W = int(os.environ.get("HYSTERESIS_W", "25"))
MIN_LIMIT_W = int(os.environ.get("MIN_LIMIT_W", "50"))
MAX_LIMIT_W = int(os.environ.get("MAX_LIMIT_W", "1500"))
FAILSAFE_LIMIT_W = int(os.environ.get("FAILSAFE_LIMIT_W", "200"))
MAX_JUMP_W = int(os.environ.get("MAX_JUMP_W", "5000"))

STATE_FILE = Path(__file__).resolve().parent.parent / "state.json"

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)


async def _capture_esphome() -> bytes:
    """Bild über die ESPHome Native API: Blitz-LED an, Belichtung
    einpendeln lassen, Frame holen, LED aus."""
    client = APIClient(ESPHOME_HOST, 6053, password=None, noise_psk=ESPHOME_API_KEY)
    await client.connect(login=True)
    light_key = None
    try:
        entities, _ = await client.list_entities_services()
        light_key = next(
            (e.key for e in entities if type(e).__name__ == "LightInfo"), None
        )
        frames: list[bytes] = []

        def on_state(state):
            if getattr(state, "data", None):
                frames.append(bytes(state.data))

        client.subscribe_states(on_state)
        if light_key is not None:
            client.light_command(key=light_key, state=True, brightness=1.0)
        await asyncio.sleep(CAM_WARMUP_S)
        # Belichtung passt sich nur waehrend laufender Aufnahmen an:
        # mehrere Frames anfordern, erst der 4./5. ist korrekt belichtet
        for _ in range(5):
            n = len(frames)
            client.request_single_image()
            for _ in range(75):
                await asyncio.sleep(0.2)
                if len(frames) > n:
                    break
        if not frames:
            raise RuntimeError("Kamera hat keinen Frame geliefert")
        return frames[-1]
    finally:
        # LED IMMER ausschalten (Hitze!), auch wenn die Aufnahme fehlschlaegt
        try:
            if light_key is not None:
                client.light_command(key=light_key, state=False)
                await asyncio.sleep(0.5)
        except Exception as e:
            print(f"WARNUNG: LED-Aus fehlgeschlagen: {e}", file=sys.stderr)
        try:
            await client.disconnect()
        except Exception:
            pass  # Cam schliesst den Socket teils selbst -> egal


def get_snapshot() -> bytes:
    if ESPHOME_API_KEY and ESPHOME_API_KEY != "CHANGE_ME" and APIClient:
        return asyncio.run(_capture_esphome())
    return requests.get(CAM_SNAPSHOT_URL, timeout=15).content


def read_meter() -> dict:
    """Snapshot holen und von Gemini lesen lassen. Wirft Exception bei Fehler."""
    img = get_snapshot()
    body = {
        "contents": [{
            "parts": [
                {"text": GEMINI_PROMPT},
                {"inline_data": {"mime_type": "image/jpeg",
                                 "data": base64.b64encode(img).decode()}},
            ]
        }],
        "generationConfig": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}},
    }
    r = requests.post(
        GEMINI_URL,
        headers={"Content-Type": "application/json", "X-goog-api-key": GEMINI_API_KEY},
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"kein JSON in Gemini-Antwort: {text[:100]!r}")
    data = json.loads(match.group(0))
    return {"kwh": int(data["kwh"]), "w": int(data["w"])}


def plausible(reading: dict, state: dict) -> str | None:
    """Gibt Fehlergrund zurück oder None wenn die Lesung plausibel ist."""
    kwh, w = reading["kwh"], reading["w"]
    if kwh == 888888 or abs(w) in (88888, 888888):
        return "LCD-Segmenttest (alles 8er)"
    if kwh <= 0:
        return "kwh<=0 — LCD vermutlich dunkel/unlesbar"
    if abs(w) > 20000:
        return f"unplausible Leistung {w} W"
    if state.get("w") is not None and abs(w - state["w"]) > MAX_JUMP_W:
        return f"Sprung {w - state['w']:+d} W > {MAX_JUMP_W} W"
    if state.get("kwh") is not None:
        if kwh < state["kwh"]:
            return f"kWh rückläufig ({state['kwh']} -> {kwh})"
        if kwh > state["kwh"] + 10:
            return f"kWh-Sprung ({state['kwh']} -> {kwh})"
    return None


def get_inverter_power() -> float:
    """Aktuelle AC-Leistung des Inverters aus OpenDTU-Livedata."""
    r = requests.get(f"{OPENDTU_URL}/api/livedata/status", timeout=10)
    r.raise_for_status()
    return float(r.json()["total"]["Power"]["v"])


def set_limit(watts: int):
    """Nicht-persistentes absolutes Limit setzen (schont den Flash der DTU)."""
    payload = {"serial": INVERTER_SERIAL, "limit_type": 0, "limit_value": watts}
    r = requests.post(
        f"{OPENDTU_URL}/api/limit/config",
        auth=OPENDTU_AUTH,
        data={"data": json.dumps(payload)},
        timeout=10,
    )
    r.raise_for_status()


def publish(reading: dict | None, status: str, limit: int | None):
    if not MQTT_HOST or mqtt_publish is None:
        return
    msgs = [(f"{TOPIC}/status", status, 0, True)]
    if reading:
        msgs += [(f"{TOPIC}/kwh", str(reading["kwh"]), 0, True),
                 (f"{TOPIC}/w", str(reading["w"]), 0, True)]
    if limit is not None:
        msgs.append((f"{TOPIC}/limit_w", str(limit), 0, True))
    try:
        mqtt_publish.multiple(msgs, hostname=MQTT_HOST, port=MQTT_PORT, auth=MQTT_AUTH)
    except Exception as e:
        print(f"MQTT-Fehler: {e}", file=sys.stderr)


def control(grid_w: int, state: dict) -> tuple[int | None, float | None]:
    """Nulleinspeisungs-Regler: neues Limit berechnen/setzen. -> (limit, pv_w)"""
    if not INVERTER_SERIAL or INVERTER_SERIAL == "CHANGE_ME":
        return None, None
    try:
        pv_w = get_inverter_power()
    except Exception as e:
        print(f"OpenDTU nicht erreichbar: {e}", file=sys.stderr)
        return None, None
    # grid_w > 0 = Bezug, < 0 = Einspeisung. Ziel: leichter Bezug.
    target = pv_w + grid_w - TARGET_GRID_W
    current = state.get("limit_w", int(pv_w))
    step = max(-MAX_STEP_W, min(MAX_STEP_W, int(target) - current))
    new_limit = max(MIN_LIMIT_W, min(MAX_LIMIT_W, current + step))
    if abs(new_limit - current) < HYSTERESIS_W and "limit_w" in state:
        return current, pv_w
    try:
        set_limit(new_limit)
        return new_limit, pv_w
    except Exception as e:
        print(f"Limit setzen fehlgeschlagen: {e}", file=sys.stderr)
        return None, pv_w


def main(once: bool = False):
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    while True:
        limit = None
        try:
            reading = read_meter()
            reason = plausible(reading, state)
            if reason:
                raise ValueError(f"verworfen: {reason}")
            state.update(reading)
            state["failures"] = 0
            limit, pv_w = control(reading["w"], state)
            if limit is not None:
                state["limit_w"] = limit
            publish(reading, "ok", limit)
            pv = f"{pv_w:.0f}" if pv_w is not None else "?"
            print(f"kwh={reading['kwh']} w={reading['w']:+d} pv={pv} limit={limit}")
        except Exception as e:
            state["failures"] = state.get("failures", 0) + 1
            print(f"Fehler ({state['failures']}x): {e}", file=sys.stderr)
            if state["failures"] >= 3:
                # Failsafe: Inverter drosseln statt blind weiter einspeisen
                try:
                    set_limit(FAILSAFE_LIMIT_W)
                    state["limit_w"] = FAILSAFE_LIMIT_W
                    publish(None, "failsafe", FAILSAFE_LIMIT_W)
                except Exception as e2:
                    print(f"Failsafe fehlgeschlagen: {e2}", file=sys.stderr)
                    publish(None, "error", None)
            else:
                publish(None, "retry", None)
        STATE_FILE.write_text(json.dumps(state))
        if once:
            break
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main(once="--once" in sys.argv)
