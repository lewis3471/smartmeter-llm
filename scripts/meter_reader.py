#!/usr/bin/env python3
"""Nulleinspeisung: ESP32-Cam -> Gemini Vision -> Plausibilitätsfilter
-> MQTT (Home Assistant Logging) -> OpenDTU Limit-Regelung.

Läuft als Endlosschleife im INTERVAL_S-Takt (Free Tier: 1000 req/Tag).
"""

import asyncio
import base64
import builtins
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

socket.setdefaulttimeout(20)  # MQTT & Co. duerfen nie ewig haengen


def print(*args, **kwargs):  # noqa: A001 — Log immer mit Zeitstempel
    builtins.print(time.strftime("[%m-%d %H:%M:%S]"), *args, **kwargs)

import requests

try:
    import paho.mqtt.client as mqtt_client
except ImportError:
    mqtt_client = None

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

# Komma-Listen; bei 429/503 rotiert erst der Key (eigene Quota je Account),
# dann das Modell
GEMINI_API_KEYS = [
    k.strip()
    for k in os.environ.get(
        "GEMINI_API_KEYS", os.environ.get("GEMINI_API_KEY", "")
    ).split(",")
    if k.strip()
]
GEMINI_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODELS",
        os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
    ).split(",")
    if m.strip()
]
_combo_idx = 0  # Index in (Modell x Key)-Kombinationen
_combo_day = time.strftime("%Y-%m-%d")
ESPHOME_HOST = os.environ.get("ESPHOME_HOST", "")
ESPHOME_API_KEY = os.environ.get("ESPHOME_API_KEY", "")
CAM_WARMUP_S = float(os.environ.get("CAM_WARMUP_S", "3.5"))
CAM_FRAMES = int(os.environ.get("CAM_FRAMES", "5"))       # Warm-up-Frames
LED_BRIGHTNESS = float(os.environ.get("LED_BRIGHTNESS", "1.0"))
# flash: LED pro Zyklus an/aus (Standard) | continuous: LED dauerhaft an,
# Verbindung offen -> Belichtung bleibt eingependelt, 1 Frame pro Zyklus,
# ermoeglicht Intervalle bis 1s
CAM_MODE = os.environ.get("CAM_MODE", "flash")
CONTROL_EVERY = int(os.environ.get("CONTROL_EVERY", "1"))  # Regeln alle N Zyklen
FAILSAFE_AFTER = int(os.environ.get("FAILSAFE_AFTER", "3"))
# Snapshots + Gemini-Label als Trainingsdaten fuer lokales OCR ablegen
SAVE_SAMPLES_DIR = os.environ.get("SAVE_SAMPLES_DIR", "")

# --- Lokales OCR ---
# gemini: nur Cloud | local: nur lokales kNN-OCR | hybrid: lokal lesen,
# Gemini bei niedriger Confidence/Fehler und als Kreuz-Check alle N Zyklen
READER_MODE = os.environ.get("READER_MODE", "gemini")
OCR_MIN_CONF = float(os.environ.get("OCR_MIN_CONF", "0.85"))
CROSS_CHECK_EVERY = int(os.environ.get("CROSS_CHECK_EVERY", "20"))

_local_reader = None
if READER_MODE in ("local", "hybrid"):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "ocr"))
        from local_reader import LocalReader
        _local_reader = LocalReader()
    except Exception as e:
        print(f"Lokales OCR nicht verfuegbar ({e})"
              f"{' -> Gemini-only' if READER_MODE == 'hybrid' else ''}",
              file=sys.stderr)
        if READER_MODE == "local":
            raise
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

STATE_FILE = Path(
    os.environ.get("STATE_FILE", Path(__file__).resolve().parent.parent / "state.json")
)

def gemini_combo(idx: int) -> tuple[str, str]:
    """(model, key) fuer Kombination idx: erst alle Keys je Modell durchgehen."""
    n_keys = len(GEMINI_API_KEYS)
    model = GEMINI_MODELS[(idx // n_keys) % len(GEMINI_MODELS)]
    key = GEMINI_API_KEYS[idx % n_keys]
    return model, key


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
            client.light_command(key=light_key, state=True,
                                 brightness=LED_BRIGHTNESS)
        await asyncio.sleep(CAM_WARMUP_S)
        # Belichtung passt sich nur waehrend laufender Aufnahmen an:
        # mehrere Frames anfordern, erst der 4./5. ist korrekt belichtet.
        # Bei fester Belichtung (aec_mode: manual) reicht CAM_FRAMES=1.
        for _ in range(CAM_FRAMES):
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


async def _capture_with_timeout() -> bytes:
    # Harter Deckel: haengender WLAN-Connect darf den Zyklus nicht blockieren
    return await asyncio.wait_for(_capture_esphome(), timeout=90)


class ContinuousCam:
    """Persistente Cam-Verbindung: LED bleibt an, Belichtung eingependelt,
    ein Frame pro Abruf. Reconnect bei Verbindungsabriss."""

    def __init__(self):
        import threading

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.client = None
        self.light_key = None
        self.frames: list[bytes] = []

    async def _ensure(self):
        if self.client is not None:
            return
        client = APIClient(ESPHOME_HOST, 6053, password=None,
                           noise_psk=ESPHOME_API_KEY)
        await client.connect(login=True)
        entities, _ = await client.list_entities_services()
        self.light_key = next(
            (e.key for e in entities if type(e).__name__ == "LightInfo"), None
        )
        client.subscribe_states(
            lambda s: self.frames.append(bytes(s.data))
            if getattr(s, "data", None) else None
        )
        if self.light_key is not None:
            client.light_command(key=self.light_key, state=True,
                                 brightness=LED_BRIGHTNESS)
        self.client = client
        # Belichtung einpendeln lassen (nur nach (Re-)Connect noetig)
        await asyncio.sleep(1.5)
        for _ in range(CAM_FRAMES):
            n = len(self.frames)
            client.request_single_image()
            for _ in range(40):
                await asyncio.sleep(0.1)
                if len(self.frames) > n:
                    break
        print("Cam verbunden, LED an, Belichtung eingependelt")

    async def _snap(self) -> bytes:
        try:
            await self._ensure()
            n = len(self.frames)
            self.client.request_single_image()
            for _ in range(60):
                await asyncio.sleep(0.1)
                if len(self.frames) > n:
                    frame = self.frames[-1]
                    del self.frames[:-1]  # Speicher begrenzen
                    return frame
            raise RuntimeError("kein Frame innerhalb 6s")
        except Exception:
            await self._teardown(light_off=False)
            raise

    async def _teardown(self, light_off: bool):
        client, self.client = self.client, None
        if client is None:
            return
        try:
            if light_off and self.light_key is not None:
                client.light_command(key=self.light_key, state=False)
                await asyncio.sleep(0.3)
            await client.disconnect()
        except Exception:
            pass

    def snapshot(self) -> bytes:
        fut = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(self._snap(), timeout=60), self.loop)
        return fut.result(timeout=70)

    def reassert(self):
        """Verbindung neu aufbauen (inkl. LED an + Belichtungs-Warm-up) —
        z.B. wenn jemand die LED von aussen ausgeschaltet hat."""
        try:
            asyncio.run_coroutine_threadsafe(
                self._teardown(light_off=False), self.loop).result(timeout=15)
        except Exception:
            pass

    def shutdown(self):
        try:
            asyncio.run_coroutine_threadsafe(
                self._teardown(light_off=True), self.loop).result(timeout=10)
        except Exception:
            pass


_cam: "ContinuousCam | None" = None


def get_snapshot() -> bytes:
    global _cam
    if ESPHOME_API_KEY and ESPHOME_API_KEY != "CHANGE_ME" and APIClient:
        if CAM_MODE == "continuous":
            if _cam is None:
                _cam = ContinuousCam()
            return _cam.snapshot()
        return asyncio.run(_capture_with_timeout())
    return requests.get(CAM_SNAPSHOT_URL, timeout=15).content


GEMINI_COOLDOWN_S = int(os.environ.get("GEMINI_COOLDOWN_S", "30"))
_last_gemini_call = 0.0


def image_brightness(img: bytes) -> float:
    try:
        import cv2
        import numpy as np
        g = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_GRAYSCALE)
        return float(g.mean()) if g is not None else 0.0
    except ImportError:
        return 255.0  # ohne OpenCV keine Pruefung


def read_meter(cycle: int = 0) -> tuple[dict, str]:
    """Snapshot holen und lesen. -> (Lesung, Quelle 'local c=0.97'/'gemini')."""
    global _last_gemini_call
    img = get_snapshot()
    # Schwarzes Bild = LED aus (externe Automation?) -> LED neu setzen,
    # keinesfalls an Gemini schicken
    if image_brightness(img) < 12:
        if _cam is not None:
            _cam.reassert()
        raise ValueError("Bild dunkel — LED-Reassert ausgeloest")
    if _local_reader is not None:
        local, conf, err = None, 0.0, None
        try:
            local, conf = _local_reader.read(img)
        except ValueError as e:
            if "Segmenttest" in str(e):
                raise  # eindeutig, Gemini braucht's nicht zu bestaetigen
            err = e
        cross_check = READER_MODE == "hybrid" and cycle % CROSS_CHECK_EVERY == 0
        if local is not None and conf >= OCR_MIN_CONF and not cross_check:
            return local, f"local c={conf:.2f}"
        if READER_MODE == "local":
            if local is None:
                raise err
            return local, f"local c={conf:.2f} (unter Schwelle)"
        # hybrid: Gemini fragen (Kreuz-Check / niedrige Confidence / Fehler)
        # Cooldown: bei Dauerfehlern im Sekundentakt nicht die Quota verbrennen
        if not cross_check and time.time() - _last_gemini_call < GEMINI_COOLDOWN_S:
            if local is not None:
                return local, f"local c={conf:.2f} (Gemini-Cooldown)"
            raise err if err else ValueError("unlesbar (Gemini-Cooldown)")
        _last_gemini_call = time.time()
        try:
            gem = gemini_read(img)
        except Exception as e:
            if local is not None and conf >= OCR_MIN_CONF:
                # Kreuz-Check gescheitert -> lokale Lesung reicht
                print(f"Gemini-Ausfall ({e}) -> nutze lokale Lesung",
                      file=sys.stderr)
                return local, f"local c={conf:.2f} (Gemini-Ausfall)"
            raise
        if local is not None and local != gem:
            d = Path(SAVE_SAMPLES_DIR or "samples") / "disagreements"
            d.mkdir(parents=True, exist_ok=True)
            stem = time.strftime("%Y%m%d_%H%M%S")
            (d / f"{stem}.jpg").write_bytes(img)
            (d / f"{stem}.json").write_text(json.dumps(
                {"local": local, "conf": conf, "gemini": gem}))
            print(f"OCR-Abweichung: local={local} (c={conf:.2f})"
                  f" vs gemini={gem} -> gespeichert", file=sys.stderr)
        return gem, "gemini" + (" (cross-check)" if cross_check else "")
    return gemini_read(img), "gemini"


def gemini_read(img: bytes) -> dict:
    """Bild von Gemini lesen lassen. Wirft Exception bei Fehler."""
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
    global _combo_idx, _combo_day
    today = time.strftime("%Y-%m-%d")
    if today != _combo_day:  # Quota-Reset -> wieder mit bestem Modell/Key starten
        _combo_idx, _combo_day = 0, today
    n_combos = len(GEMINI_MODELS) * len(GEMINI_API_KEYS)
    r = None
    for _ in range(n_combos):
        model, key = gemini_combo(_combo_idx)
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"Content-Type": "application/json", "X-goog-api-key": key},
            json=body,
            timeout=30,
        )
        if r.status_code in (429, 503):
            _combo_idx += 1
            nm, nk = gemini_combo(_combo_idx)
            print(
                f"{model}/Key…{key[-4:]}: HTTP {r.status_code}"
                f" -> rotiere zu {nm}/Key…{nk[-4:]}",
                file=sys.stderr,
            )
            continue
        break
    r.raise_for_status()
    # Antwort-Part suchen: Thinking-Modelle liefern zusaetzlich "thought"-Parts
    parts = r.json()["candidates"][0]["content"]["parts"]
    text = next(
        (p["text"] for p in parts if "text" in p and not p.get("thought")), ""
    )
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"kein JSON in Gemini-Antwort: {text[:100]!r}")
    data = json.loads(match.group(0))
    if not isinstance(data.get("kwh"), (int, float)) or not isinstance(
        data.get("w"), (int, float)
    ):
        raise ValueError(f"Gemini-Antwort unvollstaendig: {data}")
    reading = {"kwh": int(data["kwh"]), "w": int(data["w"])}
    if SAVE_SAMPLES_DIR:
        d = Path(SAVE_SAMPLES_DIR) / time.strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        stem = time.strftime("%H%M%S")
        (d / f"{stem}.jpg").write_bytes(img)
        (d / f"{stem}.json").write_text(json.dumps(reading))
    return reading


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
        if kwh > state["kwh"] + 2:
            return f"kWh-Sprung ({state['kwh']} -> {kwh})"
    return None


def rebaseline(reading: dict, state: dict) -> bool:
    """Kommt dieselbe 'unplausible' kWh-Lesung mehrfach in Folge, wird sie
    per Gemini verifiziert und bei Bestaetigung als neuer Stand akzeptiert.
    Verhindert, dass eine einmal akzeptierte Fehl-Lesung alles blockiert."""
    kwh = reading["kwh"]
    if abs(kwh - state.get("rb_kwh", -9999)) <= 1:
        state["rb_count"] = state.get("rb_count", 0) + 1
    else:
        state["rb_kwh"], state["rb_count"] = kwh, 1
    if state["rb_count"] < 4:
        return False
    state["rb_count"] = 0
    for attempt in (1, 2):
        try:
            gem = gemini_read(get_snapshot())
            if abs(gem["kwh"] - kwh) <= 2:
                print(f"Re-Baseline: Gemini bestätigt kWh={gem['kwh']} "
                      f"(alter Stand {state.get('kwh')}) -> akzeptiert")
                return True
            print(f"Re-Baseline abgelehnt: Gemini liest {gem['kwh']}, "
                  f"nicht {kwh}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"Re-Baseline Versuch {attempt}: {e}", file=sys.stderr)
    return False


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


_mqtt = None


def _get_mqtt():
    """Persistente MQTT-Verbindung mit Auto-Reconnect (statt Connect-Flut
    im Sekundentakt, die den Broker irgendwann wegwuergt)."""
    global _mqtt
    if _mqtt is None and MQTT_HOST and mqtt_client is not None:
        c = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2,
                               client_id="smartmeter-llm")
        if MQTT_AUTH:
            c.username_pw_set(MQTT_AUTH["username"], MQTT_AUTH["password"])
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        c.loop_start()
        _mqtt = c
    return _mqtt


def publish(reading: dict | None, status: str, limit: int | None):
    c = _get_mqtt()
    if c is None:
        return
    msgs = [(f"{TOPIC}/status", status)]
    if reading:
        msgs += [(f"{TOPIC}/kwh", str(reading["kwh"])),
                 (f"{TOPIC}/w", str(reading["w"]))]
    if limit is not None:
        msgs.append((f"{TOPIC}/limit_w", str(limit)))
    try:
        for topic, payload in msgs:
            c.publish(topic, payload, retain=True)
    except Exception as e:
        print(f"MQTT-Fehler: {e}", file=sys.stderr)


def publish_discovery():
    """HA-MQTT-Discovery: Sensoren melden sich selbst an (retained configs)."""
    c = _get_mqtt()
    if c is None:
        return
    device = {
        "identifiers": ["smartmeter_llm"],
        "name": "Smartmeter LLM",
        "manufacturer": "smartmeter-llm",
        "model": "ESP32-Cam + Gemini",
    }
    sensors = {
        "kwh": {"name": "Zählerstand", "unit_of_measurement": "kWh",
                "device_class": "energy", "state_class": "total_increasing",
                "icon": "mdi:counter"},
        "w": {"name": "Netzleistung", "unit_of_measurement": "W",
              "device_class": "power", "state_class": "measurement",
              "icon": "mdi:transmission-tower"},
        "limit_w": {"name": "Inverter Limit", "unit_of_measurement": "W",
                    "device_class": "power", "state_class": "measurement",
                    "icon": "mdi:speedometer"},
        "status": {"name": "Status", "icon": "mdi:eye-check"},
    }
    msgs = []
    for key, cfg in sensors.items():
        cfg.update({
            "unique_id": f"smartmeter_llm_{key}",
            "state_topic": f"{TOPIC}/{key}",
            "device": device,
        })
        msgs.append((f"homeassistant/sensor/smartmeter_llm/{key}/config",
                     json.dumps(cfg), 0, True))
    try:
        import time as _t
        for _ in range(50):  # auf Async-Connect warten
            if c.is_connected():
                break
            _t.sleep(0.1)
        for topic, payload, qos, retain in msgs:
            c.publish(topic, payload, qos=qos, retain=retain)
        print("MQTT-Discovery veröffentlicht (4 Sensoren)")
    except Exception as e:
        print(f"MQTT-Discovery fehlgeschlagen: {e}", file=sys.stderr)


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
    import atexit
    import signal

    def _bye(*_):  # LED nicht brennen lassen (continuous-Modus)
        if _cam is not None:
            _cam.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _bye)
    atexit.register(lambda: _cam is not None and _cam.shutdown())

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    publish_discovery()
    w_hist: list[int] = []  # Median-3: einzelner Ausreisser-Frame regelt nicht
    while True:
        limit = None
        try:
            state["cycle"] = state.get("cycle", 0) + 1
            reading, source = read_meter(state["cycle"])
            reason = plausible(reading, state)
            if reason and ("rückläufig" in reason or "Sprung" in reason):
                if rebaseline(reading, state):
                    reason = None
                    source += " (re-baseline)"
            if reason:
                raise ValueError(f"verworfen: {reason}")
            state.update(reading)
            state["failures"] = 0
            w_hist.append(reading["w"])
            del w_hist[:-3]
            w_ctrl = sorted(w_hist)[len(w_hist) // 2]
            if state["cycle"] % CONTROL_EVERY == 0:
                limit, pv_w = control(w_ctrl, state)
            else:
                limit = state.get("limit_w")
                try:  # PV jede Sekunde loggen (Telemetrie fuer Regler v2)
                    pv_w = get_inverter_power()
                except Exception:
                    pv_w = None
            if limit is not None:
                state["limit_w"] = limit
            publish(reading, "ok", limit)
            pv = f"{pv_w:.0f}" if pv_w is not None else "?"
            print(f"kwh={reading['kwh']} w={reading['w']:+d} pv={pv}"
                  f" limit={limit} [{source}]")
        except Exception as e:
            state["failures"] = state.get("failures", 0) + 1
            print(f"Fehler ({state['failures']}x): {e}", file=sys.stderr)
            if state["failures"] >= FAILSAFE_AFTER:
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
