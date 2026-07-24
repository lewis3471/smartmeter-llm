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
import math
import re
import socket
import sys
import time
from pathlib import Path

socket.setdefaulttimeout(20)  # MQTT & Co. duerfen nie ewig haengen


def print(*args, **kwargs):  # noqa: A001 — Zeitstempel + LOG_LEVEL-Filter
    level = os.environ.get("LOG_LEVEL", "all")  # all | error | none
    if level == "none":
        return
    if level == "error" and kwargs.get("file") is not sys.stderr:
        return
    builtins.print(time.strftime("[%m-%d %H:%M:%S]"), *args, **kwargs)

import requests
from feedback import save_event

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
    # Praefix-Normalisierung: "flash-latest" -> "gemini-flash-latest"
    (m.strip() if m.strip().startswith("gemini") else "gemini-" + m.strip())
    for m in os.environ.get(
        "GEMINI_MODELS",
        os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
    ).split(",")
    if m.strip()
]
_combo_idx = 0  # Index in (Modell x Key)-Kombinationen
_combo_day = time.strftime("%Y-%m-%d")
# Modelle, die heute 404 lieferten (aus der API entfernt / kein Free-Tier-
# Zugriff mehr): fuer den Rest des Tages aus der Rotation nehmen. Heilt
# auch veraltete Modell-Listen in gespeicherten Add-on-Optionen.
_dead_models: set = set()
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
GEMINI_PROMPT = os.environ.get("GEMINI_PROMPT", (
    'Foto eines EasyMeter-Stromzaehler-LCDs mit zwei Zeilen. '
    'Zeile 1 = Zaehlerstand in kWh: IMMER exakt 6 Ziffern, ggf. mit '
    'fuehrender Null (z.B. 035774) — gib ALLE 6 Ziffern an, lass niemals '
    'die letzte Ziffer weg. Zeile 2 = aktuelle Leistung in W (1-5 Ziffern), '
    'KANN NEGATIV sein: pruefe genau, ob links ein Minuszeichen steht. '
    'Sonderfaelle: LCD-Segmenttest (beide Zeilen zeigen nur 8er) -> '
    '{"kwh":888888,"w":888888}; Display dunkel oder unlesbar -> '
    '{"kwh":0,"w":0}. Antworte NUR mit JSON: {"kwh":int,"w":int}'))

# --- Akku-Waechter: Strings mit Batterie (Victron) vor Tiefentladung
# schuetzen. Der HMS kann nicht pro String limitieren — der Waechter senkt
# stattdessen das Gesamtlimit adaptiv, bis die gemessene Entnahme aus den
# Akku-Strings ~0 ist, und gibt per Spannungs-Hysterese wieder frei.
BATT_STRINGS = [int(s) for s in os.environ.get("BATT_STRINGS", "").replace(
    " ", "").split(",") if s.strip().isdigit()]
BATT_LOW_V = float(os.environ.get("BATT_LOW_V", "36"))
BATT_HIGH_V = float(os.environ.get("BATT_HIGH_V", "38"))
BATT_MAX_DRAIN_W = int(os.environ.get("BATT_MAX_DRAIN_W", "10"))
# Freigabe erst nach durchgehend gehaltener Spannung: die Victron-LADE-
# Spannung liegt sonst sofort ueber der Schwelle, obwohl der Akku leer ist
BATT_RELEASE_S = float(os.environ.get("BATT_RELEASE_S", "300"))
BATT_PROBE_W = 25       # Sonnen-Probe: Cap-Anhebung pro Minute im Hold
BATT_PROBE_S = 60
CAM_SNAPSHOT_URL = os.environ.get("CAM_SNAPSHOT_URL", "")  # Legacy-HTTP-Fallback
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

INTERVAL_S = float(os.environ.get("INTERVAL_S", "90"))
TARGET_GRID_W = int(os.environ.get("TARGET_GRID_W", "50"))
DEADBAND_W = int(os.environ.get("DEADBAND_W", "15"))
# Regelkreis-Totzeit Limit->Wirkung (gemessen ~6-8s inkl. MPPT/LCD/Median);
# gilt nur fuer Abwaerts-Korrekturen — hoch geht immer sofort
LATENCY_S = float(os.environ.get("LATENCY_S", "8"))
# Pending-Kompensation (Smith-Predictor): eigene Limit-Schritte werden mit
# ihrer erwarteten UNSICHTBARKEIT gewichtet vom Fehler abgezogen — voll bis
# theta (Totzeit), danach exponentiell abklingend mit tau (beide aus
# analyze_latency gefittet). Kein hartes Fenster: 1.6.4 schnitt bei 5s ab,
# genau wenn die Wirkung halb angekommen war -> Ueberreaktion auf den Rest.
PENDING_THETA_S = float(os.environ.get("PENDING_THETA_S", "4"))
PENDING_TAU_S = float(os.environ.get("PENDING_TAU_S", "2.5"))
MIN_STEP_W = int(os.environ.get("MIN_STEP_W", "15"))
# MPPT-Stuck-Kick: der HMS verklemmt sich an der Batterie gelegentlich weit
# unter dem Limit (z.B. 178W bei Limit 420) und reagiert auf kleine Schritte
# kaum — ein grosser Limit-Sprung zwingt den Tracker zum Neu-Akquirieren,
# danach laeuft er auch auf niedrigeren Limits normal. Detektion: Bezug ueber
# Deadband + Limit deutlich ueber Ist + keine Bewegung ueber STUCK_S.
STUCK_S = float(os.environ.get("STUCK_S", "25"))
STUCK_GAP_W = int(os.environ.get("STUCK_GAP_W", "150"))
KICK_COOLDOWN_S = float(os.environ.get("KICK_COOLDOWN_S", "180"))
# Eskalationstreppe statt Verdopplung: Schwelle, ab der der Tracker sich
# loest, ist unbekannt — wir tasten uns hoch und LOGGEN den loesenden
# Schritt (ev=kick_result), um die HMS-Schwelle zu vermessen.
KICK_STEPS_W = (100, 200, 400, 800)
KICK_STEP_HOLD_S = float(os.environ.get("KICK_STEP_HOLD_S", "10"))
KICK_UNSTUCK_W = 50   # so viel pv-Bewegung gilt als "geloest"


def pending_weight(age_s: float) -> float:
    """Anteil eines Limit-Schritts, der nach age_s noch NICHT messbar ist."""
    if age_s <= PENDING_THETA_S:
        return 1.0
    return math.exp(-(age_s - PENDING_THETA_S) / PENDING_TAU_S)
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
_last_snapshot: bytes | None = None


def get_snapshot() -> bytes:
    global _cam, _last_snapshot
    if ESPHOME_API_KEY and ESPHOME_API_KEY != "CHANGE_ME" and APIClient:
        if CAM_MODE == "continuous":
            if _cam is None:
                _cam = ContinuousCam()
            _last_snapshot = _cam.snapshot()
        else:
            _last_snapshot = asyncio.run(_capture_with_timeout())
    else:
        _last_snapshot = requests.get(CAM_SNAPSHOT_URL, timeout=15).content
    return _last_snapshot


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


_seg_reader = None
_last_seg_save = 0.0

# --- Retrain-Alarm: rollierende 6h-Zaehler. Wird eine Schwelle gerissen,
# meldet der HA-Sensor "OCR Retrain faellig" — Training bleibt eine bewusste
# Entscheidung auf der Trainings-Maschine (make retrain), der NUC alarmiert
# nur (Befund des Auto-Train-Reviews: Autonomie vergiftet sich selbst).
from collections import deque as _rt_deque

_retrain_ev: dict[str, "_rt_deque[float]"] = {
    "seg": _rt_deque(), "failsafe": _rt_deque(), "disagree": _rt_deque()}
_RETRAIN_WIN_S = 6 * 3600
_RETRAIN_LIMITS = {"seg": 3, "failsafe": 2, "disagree": 20}


def retrain_mark(kind: str):
    q = _retrain_ev[kind]
    q.append(time.time())


def retrain_due() -> str:
    """Leer = nichts faellig, sonst Begruendung fuer den HA-Sensor."""
    cutoff = time.time() - _RETRAIN_WIN_S
    reasons = []
    for kind, q in _retrain_ev.items():
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= _RETRAIN_LIMITS[kind]:
            reasons.append(f"{kind}={len(q)}")
    return ", ".join(reasons)



def seg_confirm(expected_lo: int, expected_hi: int,
                state: dict | None = None) -> int | None:
    """7-Segment-Zweitmeinung auf dem letzten Frame (Rollover-Schiedsrichter).

    Der deterministische Segment-Dekoder braucht keine Trainingsdaten — eine
    neue Ziffer an neuer Position liest er mit 96-97% (kNN dort: 5-66%).
    Liest er eine kWh im monotonen Erwartungsfenster, gilt sie als
    bestaetigt: kein Failsafe, und der Frame wird als Trainingslabel
    gesichert (samples/seg/, kWh-only)."""
    global _seg_reader, _last_seg_save
    if _last_snapshot is None or _local_reader is None:
        return None
    try:
        import cv2
        import numpy as np
        if _seg_reader is None:
            from seg_decoder import SegReader
            _seg_reader = SegReader(anchor_ref=_local_reader.ex._anchor_ref)
        gray = cv2.imdecode(np.frombuffer(_last_snapshot, np.uint8),
                            cv2.IMREAD_GRAYSCALE)
        labels, confs, _ = _seg_reader.read_cells(gray)
        kwh_s = "".join(labels[:6])
        if not kwh_s.isdigit():
            return None
        kwh = int(kwh_s)
        if not (expected_lo <= kwh <= expected_hi):
            return None
        # Monotonie-Sperre: der Dekoder verwechselt in der rechten
        # Schattenzone die letzte Ziffer (24.07. nachts: 35873 -> 35871).
        # Ein Wert UNTER dem hoechsten kuerzlich gesehenen ist physikalisch
        # unmoeglich -> Fehllesung. Ohne die Sperre pendelte der Stand und
        # das falsche Label landete im Korpus (Selbstvergiftung).
        now = time.time()
        if state is not None:
            top, top_ts, top_n = state.get("seg_top", (0, 0.0, 0))
            if now - top_ts > 1800:  # 30-min-Fenster
                top, top_n = 0, 0
            if kwh < top:
                print(f"Seg-Schiedsrichter: {kwh} < gesehene {top} — "
                      f"Fehllesung verworfen", file=sys.stderr)
                return None
            top_n = top_n + 1 if kwh == top else 1
            state["seg_top"] = (kwh, now, top_n)
            if top_n < 2:
                return None  # erst die zweite konsistente Lesung zaehlt
        retrain_mark("seg")
        if SAVE_SAMPLES_DIR and now - _last_seg_save >= 60:
            d = Path(SAVE_SAMPLES_DIR) / "seg"
            d.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            (d / f"{ts}.jpg").write_bytes(_last_snapshot)
            (d / f"{ts}.json").write_text(json.dumps({"kwh": kwh}))
            _last_seg_save = now
        return kwh
    except Exception as e:
        print(f"Seg-Schiedsrichter-Fehler: {e}", file=sys.stderr)
        return None


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
        if local is not None and local != gem and SAVE_SAMPLES_DIR:
            d = Path(SAVE_SAMPLES_DIR) / "disagreements"
            d.mkdir(parents=True, exist_ok=True)
            stem = time.strftime("%Y%m%d_%H%M%S")
            (d / f"{stem}.jpg").write_bytes(img)
            (d / f"{stem}.json").write_text(json.dumps(
                {"local": local, "conf": conf, "gemini": gem}))
            print(f"OCR-Abweichung: local={local} (c={conf:.2f})"
                  f" vs gemini={gem} -> gespeichert", file=sys.stderr)
            retrain_mark("disagree")
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
        _dead_models.clear()
    n_combos = len(GEMINI_MODELS) * len(GEMINI_API_KEYS)
    r = None
    for _ in range(n_combos):
        model, key = gemini_combo(_combo_idx)
        if model in _dead_models:
            _combo_idx += 1
            continue
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"Content-Type": "application/json", "X-goog-api-key": key},
            json=body,
            timeout=30,
        )
        if r.status_code in (404, 429, 503):
            if r.status_code == 404:
                _dead_models.add(model)  # Modell existiert nicht (mehr)
            _combo_idx += 1
            nm, nk = gemini_combo(_combo_idx)
            print(
                f"{model}/Key…{key[-4:]}: HTTP {r.status_code}"
                f" -> rotiere zu {nm}/Key…{nk[-4:]}",
                file=sys.stderr,
            )
            continue
        break
    if r is None:
        raise RuntimeError("alle Gemini-Modelle tot (404) — Rotation leer")
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


_event_counts: dict = {}
_event_day = ""


def _event_worth_saving(reason: str) -> bool:
    """Ersten 5 Frames je Fehlergrund/Tag speichern, danach jeden 50. —
    Segmenttest-Rotationen und Rueckläufig-Stuerme fluteten sonst das Repo
    (2300+ Frames/Tag) ohne neuen Informationswert."""
    global _event_day
    today = time.strftime("%Y%m%d")
    if today != _event_day:
        _event_day = today
        _event_counts.clear()
    key = reason[:40]
    n = _event_counts[key] = _event_counts.get(key, 0) + 1
    return n <= 5 or n % 50 == 0


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
        # Heilpfad: 4 konsistente Lesungen auf neuem Niveau heissen, dass
        # der GESPEICHERTE Stand vergiftet war (23.07.: Geister-8443 beim
        # Erststart -> jede echte Lesung "Sprung >5000" -> Deadlock).
        cand, n = state.get("wjump", (None, 0))
        if cand is not None and abs(w - cand) <= max(100, abs(cand) // 5):
            n += 1
            if n >= 4:
                state.pop("wjump", None)
                print(f"W-Re-Baseline: {state['w']} W war vergiftet, "
                      f"4x konsistent ~{w} W -> uebernehme")
                return None
            state["wjump"] = (cand, n)
        else:
            state["wjump"] = (w, 1)
        return f"Sprung {w - state['w']:+d} W > {MAX_JUMP_W} W"
    # Erststart-Loch: ohne Vergleichswert wuerde die allererste Lesung
    # bedingungslos akzeptiert — ein Geisterziffer-Frame (z.B. 8443 statt
    # 443, 23.07. 07:30) landet dann ungefiltert in HA und im Init-Limit.
    # Grosse |W| brauchen direkt nach dem Start eine zweite, konsistente
    # Lesung (+-20%, min. 100 W); der naechste Zyklus kommt ja in ~1s.
    if state.get("w") is None and abs(w) > 1000:
        first = state.get("w_first")
        state["w_first"] = w
        if first is None or abs(w - first) > max(100, abs(w) // 5):
            return f"Erststart: {w} W braucht Bestaetigung"
    # Vorzeichen-Flip bei ~gleichem Betrag = fast immer ein Minus-OCR-Fehler
    # (die '-'-Zelle ist klein und selten im Training). Erst nach 4
    # konsistenten Lesungen akzeptieren (echter Nulldurchgang bleibt moeglich).
    w_prev = state.get("w")
    if (w_prev is not None and abs(w) > 100
            and abs(w + w_prev) <= max(40, abs(w) // 5)):
        state["signflip"] = state.get("signflip", 0) + 1
        if state["signflip"] < 4:
            return f"Vorzeichen-Flip verdächtig ({w_prev:+d} -> {w:+d})"
    else:
        state["signflip"] = 0
    if state.get("kwh") is not None:
        if kwh < state["kwh"]:
            return f"kWh rückläufig ({state['kwh']} -> {kwh})"
        if kwh > state["kwh"] + 2:
            return f"kWh-Sprung ({state['kwh']} -> {kwh})"
    state.pop("wjump", None)
    return None


def rebaseline(reading: dict, state: dict) -> bool:
    """Kommt dieselbe 'unplausible' kWh-Lesung mehrfach in Folge, wird sie
    per Gemini verifiziert und bei Bestaetigung als neuer Stand akzeptiert.
    Verhindert, dass eine einmal akzeptierte Fehl-Lesung alles blockiert."""
    kwh = reading["kwh"]
    # Zaehler JE KANDIDAT: eingestreute Dunkel-Fehl-Lesungen (500, 3570...)
    # duerfen den Konsens fuer den echten Stand nicht mehr zuruecksetzen
    counts = state.setdefault("rb_counts", {})
    counts[kwh] = counts.get(kwh, 0) + 1
    if len(counts) > 20:
        state["rb_counts"] = counts = {kwh: counts[kwh]}
    if counts[kwh] < 4:
        return False
    # Gemini-Cooldown gilt auch hier — aber der Zaehler bleibt stehen,
    # damit der naechste freie Slot sofort verifiziert
    global _last_gemini_call
    if time.time() - _last_gemini_call < GEMINI_COOLDOWN_S:
        return False
    state["rb_counts"] = {}
    _last_gemini_call = time.time()
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


_livedata_cache: tuple[float, tuple] | None = None
LIVEDATA_CACHE_S = float(os.environ.get("LIVEDATA_CACHE_S", "2.5"))


def get_livedata() -> tuple[float, dict[int, tuple[float, float]]]:
    """OpenDTU-Livedata: (AC-Leistung, {String-Nr: (DC-Volt, DC-Watt)}).
    DC-Daten gibt es nur in der Detail-Ansicht (?inv=serial). Gecacht
    (LIVEDATA_CACHE_S): die DTU ist ein ESP32 — HTTP-Polling im Regeltakt
    plus Limit-POSTs wuergt ihren Webserver und die RF-Queue ab."""
    global _livedata_cache
    if _livedata_cache and time.time() - _livedata_cache[0] < LIVEDATA_CACHE_S:
        return _livedata_cache[1]
    url = f"{OPENDTU_URL}/api/livedata/status"
    if BATT_STRINGS:
        url += f"?inv={INVERTER_SERIAL}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    inv = data["inverters"][0]
    try:
        ac = float(data["total"]["Power"]["v"])
    except (KeyError, IndexError):
        ac = float(inv["AC"]["0"]["Power"]["v"])
    dc: dict[int, tuple[float, float]] = {}
    for key, ch in inv.get("DC", {}).items():
        try:
            dc[int(key) + 1] = (float(ch["Voltage"]["v"]),
                                float(ch["Power"]["v"]))
        except (KeyError, TypeError, ValueError):
            pass
    _livedata_cache = (time.time(), (ac, dc))
    return ac, dc


def get_inverter_power() -> float:
    """Aktuelle AC-Leistung des Inverters aus OpenDTU-Livedata."""
    return get_livedata()[0]


def battery_guard(state: dict, pv_w: float,
                  dc: dict[int, tuple[float, float]], now: float) -> int:
    """Liefert das erlaubte Max-Limit. Hysterese: unter BATT_LOW_V wird
    gehalten (Cap adaptiv auf Solar-only gesenkt), ab BATT_HIGH_V wieder
    freigegeben. Waehrend des Holds hebt eine Sonnen-Probe das Cap langsam
    an; zieht der Akku wieder, senkt die Messung es sofort zurueck."""
    volts = [dc[s][0] for s in BATT_STRINGS if s in dc]
    batt_w = sum(dc[s][1] for s in BATT_STRINGS if s in dc)
    hold = state.get("batt_hold", False)
    if not volts:
        return MAX_LIMIT_W if not hold else state.get("batt_cap", MAX_LIMIT_W)
    v = min(volts)
    state["batt_v"] = v
    if not hold and v < BATT_LOW_V:
        hold = True
        state["batt_cap"] = max(MIN_LIMIT_W, int(pv_w - batt_w))
        state["batt_cap_ts"] = now
        print(f"Akku-Waechter: {v:.1f}V < {BATT_LOW_V}V — halte Limit auf "
              f"Solar-only (Cap {state['batt_cap']}W, Akku zog {batt_w:.0f}W)")
    elif hold and v >= BATT_HIGH_V:
        # Victron haengt mit Solar am selben Bus: beim Laden liegt die
        # BUS-Spannung sofort ueber der Schwelle, obwohl der Akku noch leer
        # ist. Erst freigeben, wenn sie BATT_RELEASE_S durchgehend hielt.
        if "batt_high_since" not in state:
            state["batt_high_since"] = now
        if now - state["batt_high_since"] >= BATT_RELEASE_S:
            hold = False
            state.pop("batt_cap", None)
            state.pop("batt_high_since", None)
            print(f"Akku-Waechter: {v:.1f}V >= {BATT_HIGH_V}V "
                  f"({BATT_RELEASE_S:.0f}s gehalten) — Akku-Strings "
                  "wieder freigegeben")
    elif hold:
        state.pop("batt_high_since", None)  # Spannung wieder eingebrochen
    state["batt_hold"] = hold
    if not hold:
        return MAX_LIMIT_W
    cap = state.get("batt_cap", MAX_LIMIT_W)
    since = now - state.get("batt_cap_ts", 0)
    if batt_w > BATT_MAX_DRAIN_W and since >= LATENCY_S:
        cap = max(MIN_LIMIT_W, min(cap, int(pv_w - batt_w)))
        state["batt_cap_ts"] = now
        print(f"Akku-Waechter: Akku zieht {batt_w:.0f}W — Cap -> {cap}W")
    elif since >= BATT_PROBE_S and cap < MAX_LIMIT_W:
        cap = min(MAX_LIMIT_W, cap + BATT_PROBE_W)  # Sonnen-Probe
        state["batt_cap_ts"] = now
    state["batt_cap"] = cap
    return cap


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


_mqtt_last: dict = {}
MQTT_MIN_INTERVAL_S = float(os.environ.get("MQTT_MIN_INTERVAL_S", "5"))


def _throttled(topic: str, payload: str, now: float) -> bool:
    """True = senden. Identische Payloads werden unterdrueckt, Aenderungen
    hoechstens alle MQTT_MIN_INTERVAL_S — ausser kwh/status (sofort)."""
    last = _mqtt_last.get(topic)
    if last and last[1] == payload:
        return False
    if (last and topic.rsplit("/", 1)[-1] in ("w", "limit_w", "batt_v")
            and now - last[0] < MQTT_MIN_INTERVAL_S):
        return False
    _mqtt_last[topic] = (now, payload)
    return True


def publish(reading: dict | None, status: str, limit: int | None,
            state: dict | None = None):
    c = _get_mqtt()
    if c is None:
        return
    msgs = [(f"{TOPIC}/status", status)]
    if reading:
        msgs += [(f"{TOPIC}/kwh", str(reading["kwh"])),
                 (f"{TOPIC}/w", str(reading["w"]))]
    if limit is not None:
        msgs.append((f"{TOPIC}/limit_w", str(limit)))
    if state is not None and "batt_v" in state:
        msgs += [(f"{TOPIC}/batt_v", f"{state['batt_v']:.1f}"),
                 (f"{TOPIC}/batt_hold",
                  "ON" if state.get("batt_hold") else "OFF")]
    due = retrain_due()
    msgs += [(f"{TOPIC}/retrain_due", "ON" if due else "OFF"),
             (f"{TOPIC}/retrain_reason", due or "-")]
    try:
        _now = time.time()
        for topic, payload in msgs:
            if _throttled(topic, payload, _now):
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
    if BATT_STRINGS:
        sensors["batt_v"] = {"name": "Akku-Spannung",
                             "unit_of_measurement": "V",
                             "device_class": "voltage",
                             "state_class": "measurement",
                             "icon": "mdi:battery-outline"}
        sensors["batt_hold"] = {"name": "Akku-Schutz aktiv",
                                "icon": "mdi:battery-lock"}
    msgs = [("homeassistant/binary_sensor/smartmeter_llm/retrain_due/config",
             json.dumps({"name": "OCR Retrain f\u00e4llig",
                         "unique_id": "smartmeter_llm_retrain_due",
                         "state_topic": f"{TOPIC}/retrain_due",
                         "icon": "mdi:school",
                         "device": device}), 0, True)]
    sensors["retrain_reason"] = {"name": "OCR Retrain Grund",
                                 "icon": "mdi:school-outline"}
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


# --- Regler-Telemetrie: Limit-Sends + Leistungsverlauf (JSONL) fuer die
# FOPDT-Analyse der HMS-Totzeit (scripts/analyze_latency.py). Vorlauf-Ticks
# kommen aus einem Ringpuffer, nach jedem Send wird 45s lang mitgeschrieben.
from collections import deque as _deque

_ctl_buf: "_deque[dict]" = _deque(maxlen=30)
_ctl_until = 0.0
CTL_LOG_AFTER_S = 45


def _ctl_write(rec: dict):
    if not SAVE_SAMPLES_DIR:
        return
    d = Path(SAVE_SAMPLES_DIR) / "control"
    try:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / (time.strftime("%Y%m%d") + ".jsonl"), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def ctl_tick(grid_w: int, pv_w: float, limit):
    rec = {"t": round(time.time(), 2), "ev": "tick", "grid": grid_w,
           "pv": round(pv_w, 1), "limit": limit}
    if time.time() < _ctl_until:
        _ctl_write(rec)
    else:
        _ctl_buf.append(rec)


def ctl_send(old, new, tag: str):
    global _ctl_until
    for rec in _ctl_buf:
        _ctl_write(rec)
    _ctl_buf.clear()
    _ctl_write({"t": round(time.time(), 2), "ev": "limit",
                "from": old, "to": new, "tag": tag})
    _ctl_until = time.time() + CTL_LOG_AFTER_S


def control(grid_w: int, state: dict) -> tuple[int | None, float | None]:
    """Asymmetrischer Absolut-Regler ("GIB IHM"-Politik):

    wanted = PV + Netzleistung - Ziel  — das physikalisch korrekte Limit,
    direkt aus der Messung (OCR sekuendlich, PV sekuendlich).
    - HOCH (Bezug ueber Ziel): SOFORT und ungebremst auf wanted. Kein Guard,
      kein Slew — kein Cent Netzbezug, wenn die Sonne liefern koennte.
    - Bei Wolken wird NICHT gesenkt: wanted haelt das Limit auf Bedarfsniveau,
      Sonnenrueckkehr deckt die Last ohne Anlauf.
    - RUNTER nur bei echter Ueber-Einspeisung (w < Ziel - Deadband), und nur
      mit Totzeit-Guard (LATENCY_S), damit stale Messwerte keine
      Abwaertsspirale treten.
    """
    if not INVERTER_SERIAL or INVERTER_SERIAL == "CHANGE_ME":
        return None, None
    try:
        pv_w, dc = get_livedata()
    except Exception as e:
        print(f"OpenDTU nicht erreichbar: {e}", file=sys.stderr)
        return None, None
    now = time.time()
    max_limit = MAX_LIMIT_W
    if BATT_STRINGS:
        max_limit = battery_guard(state, pv_w, dc, now)
    horizon = PENDING_THETA_S + 4 * PENDING_TAU_S
    pend = [(ts, d) for ts, d in state.get("pending", [])
            if now - ts < horizon]
    state["pending"] = pend
    pending = sum(d * pending_weight(now - ts) for ts, d in pend)
    error_raw = grid_w - TARGET_GRID_W  # >0: zu viel Bezug
    # wanted bleibt absolut aus Roh-Messwerten (staleness-invariant);
    # ENTSCHIEDEN wird auf dem kompensierten Fehler
    error = error_raw - int(round(pending))
    wanted = int(round(max(MIN_LIMIT_W, min(max_limit, pv_w + error_raw))))
    current = state.get("limit_w")
    ctl_tick(grid_w, pv_w, current)

    def send(value: int, tag: str):
        if now - state.get("limit_sent_ts", 0) < 2.0:
            return None  # DTU-RF-Queue schonen — naechster Zyklus traegt nach
        try:
            set_limit(value)
            state["limit_sent_ts"] = now
            if current is not None:
                state.setdefault("pending", []).append((now, value - current))
            ctl_send(current, value, tag)
            print(f"Regler: Limit {current}->{value} [{tag}] "
                  f"(e={error:+.0f}, pv={pv_w:.0f})")
            return value
        except Exception as e:
            print(f"Limit setzen fehlgeschlagen: {e}", file=sys.stderr)
            return None

    if current is None:
        return send(wanted, "init"), pv_w
    # MPPT-Stuck-Kick (siehe oben): Eskalationstreppe reisst den Tracker
    # los; der loesende Schritt wird geloggt (Schwellen-Vermessung), der
    # normale runter-Pfad holt das Limit danach von selbst zurueck
    k = state.get("kick")
    if k:
        if pv_w - k["pv0"] >= KICK_UNSTUCK_W:
            delta = current - k["base"]
            print(f"MPPT-Kick GELOEST: +{delta}W (Stufe {k['step']}) — "
                  f"pv {k['pv0']:.0f} -> {pv_w:.0f}W")
            _ctl_write({"t": round(now, 2), "ev": "kick_result", "ok": True,
                        "base": k["base"], "pv0": k["pv0"], "delta": delta,
                        "step": k["step"], "pv": round(pv_w, 1)})
            state.pop("kick")
            state["kick_ts"] = now
        elif now - k["ts"] >= KICK_STEP_HOLD_S:
            if k["step"] >= len(KICK_STEPS_W) or current >= max_limit:
                print(f"MPPT-Kick erfolglos (Quelle begrenzt?) — "
                      f"pv {pv_w:.0f}W bei Limit {current}W")
                _ctl_write({"t": round(now, 2), "ev": "kick_result",
                            "ok": False, "base": k["base"], "pv0": k["pv0"],
                            "delta": current - k["base"], "pv": round(pv_w, 1)})
                state.pop("kick")
                state["kick_ts"] = now
            else:
                target = int(min(max_limit, k["base"] + KICK_STEPS_W[k["step"]]))
                k["step"] += 1
                k["ts"] = now
                if target > current:
                    return send(target, f"kick{k['step']}") or current, pv_w
        else:
            return current, pv_w  # Stufe wirken lassen
    elif (error > DEADBAND_W and current - pv_w > STUCK_GAP_W
          and max_limit - current >= KICK_STEPS_W[0]):
        # nur wenn Kick-Spielraum existiert — Limit am Anschlag heisst
        # quellenbegrenzt (Wolke/Akku-Cap), nicht verklemmt
        if "stuck_since" not in state:
            state["stuck_since"] = now
            state["stuck_pv0"] = pv_w
        elif (now - state["stuck_since"] > STUCK_S
              and pv_w - state.get("stuck_pv0", 0) < 25
              and now - state.get("kick_ts", 0) > KICK_COOLDOWN_S):
            state.pop("stuck_since", None)
            print(f"MPPT-Kick: pv {pv_w:.0f}W klemmt unter Limit {current}W "
                  f"— Eskalation startet (+{KICK_STEPS_W[0]}W)")
            state["kick"] = {"base": current, "pv0": pv_w, "step": 1,
                             "ts": now}
            target = int(min(max_limit, current + KICK_STEPS_W[0]))
            return send(target, "kick1") or current, pv_w
    else:
        state.pop("stuck_since", None)
    # Akku-Hold: Limit ueber dem Cap SOFORT senken — die normale
    # runter-Bedingung greift nicht, solange der Akku das Netz auf Ziel haelt
    if current > max_limit and now - state.get("limit_sent_ts", 0) >= LATENCY_S:
        return send(max_limit, "akku-schutz") or current, pv_w
    if error > DEADBAND_W and wanted > current:
        if wanted - current < MIN_STEP_W:
            return current, pv_w  # Mikro-Trim: Funk-Spam ohne Wirkung
        return send(wanted, "hoch") or current, pv_w
    if error < -DEADBAND_W and wanted < current:
        if current - wanted < MIN_STEP_W:
            return current, pv_w
        if now - state.get("limit_sent_ts", 0) < LATENCY_S:
            return current, pv_w  # letzte Korrektur erst wirken lassen
        return send(wanted, "runter") or current, pv_w
    return current, pv_w


# Auto-Training (Stunde 0-23, -1 = aus). Auf dem NUC aus lassen —
# der Sync liefert nur Evidence; trainiert wird nach Label-Audit.
AUTO_TRAIN_HOUR = int(os.environ.get(
    "AUTO_TRAIN_HOUR", os.environ.get("RETRAIN_HOUR", "-1")))
_model_mtime: float | None = None


def maybe_reload_model():
    """Hot-Reload, wenn model.npz sich geaendert hat — z.B. durch den
    Feedback-Sync (eigenes Retraining oder git pull von anderer Maschine)."""
    global _local_reader, _model_mtime
    if _local_reader is None:
        return
    try:
        from local_reader import MODEL_FILE, LocalReader
        mt = MODEL_FILE.stat().st_mtime
    except OSError:
        return
    if _model_mtime is None:
        _model_mtime = mt
        return
    if mt != _model_mtime:
        try:
            _local_reader = LocalReader()
            _model_mtime = mt
            print(f"OCR-Modell neu geladen ({MODEL_FILE})")
        except Exception as e:
            print(f"Modell-Reload fehlgeschlagen: {e}", file=sys.stderr)


def maybe_retrain(state: dict):
    """Naechtliches Auto-Retraining: Gemini-bestaetigte Disagreements werden
    Trainingsdaten, Modell wird neu gebaut und im laufenden Betrieb geladen.
    Kein manueller Eingriff mehr noetig (Zaehler-Rollover, Lichtwechsel...)."""
    global _local_reader
    if AUTO_TRAIN_HOUR < 0 or _local_reader is None or not SAVE_SAMPLES_DIR:
        return  # ohne Sample-Sammlung gibt es nichts zu trainieren
    today = time.strftime("%Y-%m-%d")
    if state.get("retrain_day") == today or int(time.strftime("%H")) != AUTO_TRAIN_HOUR:
        return
    state["retrain_day"] = today
    try:
        root = Path(SAVE_SAMPLES_DIR or "samples")
        dst = root / "auto"
        dst.mkdir(parents=True, exist_ok=True)
        ref_kwh = state.get("kwh", 0)
        n = 0
        for jf in sorted((root / "disagreements").glob("*.json")):
            if (dst / f"{jf.stem}.json").exists():
                continue
            d = json.loads(jf.read_text())
            gem = d.get("gemini")
            if not gem or not isinstance(gem.get("kwh"), int):
                continue
            if abs(gem["kwh"] - ref_kwh) > 50 or abs(gem.get("w", 0)) > 20000:
                continue
            if 888888 in (gem["kwh"], gem.get("w")):
                continue
            (dst / f"{jf.stem}.jpg").write_bytes(
                jf.with_suffix(".jpg").read_bytes())
            (dst / f"{jf.stem}.json").write_text(json.dumps(gem))
            n += 1
        import subprocess
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "ocr" / "train.py"),
             str(root)],
            capture_output=True, text=True, timeout=1800,
        )
        summary = [ln for ln in r.stdout.splitlines()
                   if "Accuracy" in ln or "End-to-End" in ln]
        if r.returncode == 0:
            from local_reader import LocalReader
            _local_reader = LocalReader()  # neues model.npz laden
            print(f"Auto-Retraining ok (+{n} Disagreements): "
                  f"{' | '.join(summary)}")
        else:
            print(f"Auto-Retraining fehlgeschlagen: {r.stderr[-200:]}",
                  file=sys.stderr)
    except Exception as e:
        print(f"Auto-Retraining Fehler: {e}", file=sys.stderr)


def main(once: bool = False):
    import atexit
    import signal

    def _bye(*_):  # LED nicht brennen lassen (continuous-Modus)
        if _cam is not None:
            _cam.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _bye)
    atexit.register(lambda: _cam is not None and _cam.shutdown())

    state = {}
    if STATE_FILE.exists():
        try:
            state = {"kwh": json.loads(STATE_FILE.read_text()).get("kwh")}
        except (ValueError, OSError):
            state = {}
    publish_discovery()
    w_hist: list[int] = []  # Median-3: einzelner Ausreisser-Frame regelt nicht
    last_written_kwh = state.get("kwh")
    while True:
        limit = None
        try:
            state["cycle"] = state.get("cycle", 0) + 1
            reading, source = read_meter(state["cycle"])
            reason = plausible(reading, state)
            if (reason and state.get("kwh") is not None
                    and ("rückläufig" in reason or "kWh-Sprung" in reason)):
                seg_kwh = seg_confirm(state["kwh"], state["kwh"] + 2, state)
                if seg_kwh is not None:
                    print(f"Seg-Schiedsrichter: kWh {reading['kwh']} "
                          f"verworfen, Segment-Dekoder bestaetigt {seg_kwh}")
                    reading = {**reading, "kwh": seg_kwh}
                    reason = None
                    source += " (seg)"
            if reason and ("rückläufig" in reason or "Sprung" in reason):
                if rebaseline(reading, state):
                    reason = None
                    source += " (re-baseline)"
            if reason:
                raise ValueError(f"verworfen: {reason}")
            # kWh-ERHOEHUNGEN erst nach 2 uebereinstimmenden Lesungen
            # uebernehmen: eine einzelne Fehl-Lesung an der Toleranzgrenze
            # (1->3: 35851->35853) vergiftete sonst den Stand und blockte
            # danach alles als "rueckläufig" (21.07.: 50min Failsafe)
            if (state.get("kwh") is not None and reading["kwh"] > state["kwh"]
                    and "re-baseline" not in source and "(seg)" not in source):
                if state.get("kwh_pend") == reading["kwh"]:
                    state["kwh_pend_n"] = state.get("kwh_pend_n", 1) + 1
                else:
                    state["kwh_pend"], state["kwh_pend_n"] = reading["kwh"], 1
                if state["kwh_pend_n"] < 2:
                    reading = {**reading, "kwh": state["kwh"]}
                else:
                    state.pop("kwh_pend", None)
                    state.pop("kwh_pend_n", None)
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
            publish(reading, "ok", limit, state)
            pv = f"{pv_w:.0f}" if pv_w is not None else "?"
            print(f"kwh={reading['kwh']} w={reading['w']:+d} pv={pv}"
                  f" limit={limit} [{source}]")
        except Exception as e:
            state["failures"] = state.get("failures", 0) + 1
            if _event_worth_saving(str(e)):
                save_event(SAVE_SAMPLES_DIR, _last_snapshot,
                           "rejected_reading", error=str(e),
                           failures=state["failures"],
                           accepted_kwh=state.get("kwh"))
            print(f"Fehler ({state['failures']}x): {e}", file=sys.stderr)
            if state["failures"] >= FAILSAFE_AFTER:
                # Failsafe: Inverter drosseln statt blind weiter einspeisen
                try:
                    set_limit(FAILSAFE_LIMIT_W)
                    state["limit_w"] = FAILSAFE_LIMIT_W
                    if state["failures"] == FAILSAFE_AFTER:  # nur Eintritt
                        retrain_mark("failsafe")
                    publish(None, "failsafe", FAILSAFE_LIMIT_W)
                except Exception as e2:
                    print(f"Failsafe fehlgeschlagen: {e2}", file=sys.stderr)
                    publish(None, "error", None)
            elif state["failures"] >= 3:
                # Einzelne verworfene Frames (Segmenttest-Rotation) sind
                # normal — erst anhaltende Fehler als "retry" melden
                publish(None, "retry", None)
        if state.get("kwh") != last_written_kwh:
            # Nur das kWh-Feld persistieren (einziger Wert, der einen
            # Neustart ueberleben muss) — wenige Winz-Writes pro Tag
            STATE_FILE.write_text(json.dumps({"kwh": state.get("kwh")}))
            last_written_kwh = state.get("kwh")
        maybe_retrain(state)
        if state["cycle"] % 100 == 0:  # ~alle 1-2min nach neuem Modell schauen
            maybe_reload_model()
        if once:
            break
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main(once="--once" in sys.argv)
