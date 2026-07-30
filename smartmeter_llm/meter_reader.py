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
import struct
import sys
import threading
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
    'die letzte Ziffer weg. Rechts der 6 Ziffern kann eine NACHKOMMASTELLE '
    'stehen (z.B. 035891.4) — haenge sie NIEMALS an, gib ausschliesslich '
    'die 6 Vorkomma-Ziffern als Zahl an (035891.4 -> 35891). '
    'Zeile 2 = aktuelle Leistung in W (1-5 Ziffern), '
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
# Entprellung: so lange muss die Spannung unter BATT_LOW_V liegen, bevor
# abgeschaltet wird (Lastsprung != leerer Akku)
BATT_TRIP_S = float(os.environ.get("BATT_TRIP_S", "15"))
# Freigabe-Schwelle liegt knapp UEBER der Ausloese-Schwelle — frueher war es
# BATT_HIGH_V (Zielspannung "voll"), was den Schutz praktisch nie loeste
BATT_RECOVER_V = float(os.environ.get("BATT_RECOVER_V", "1.5"))
# Freigabe erst nach durchgehend gehaltener Spannung: die Victron-LADE-
# Spannung liegt sonst sofort ueber der Schwelle, obwohl der Akku leer ist
BATT_RELEASE_S = float(os.environ.get("BATT_RELEASE_S", "300"))
# --- Zweite Quelle: Deye-Balkonwechselrichter (SUN600G3) lokal auslesen.
# Der Solarman-Logger liefert seine Werte ohne Cloud auf /status.html als
# JS-Variablen (webdata_now_p usw.). Rein lesend. Leer = aus.
# WICHTIG — GEMESSEN am 28.07. (57 Proben im 5s-Takt ueber 4:44 min):
# Der Logger fragt den Wechselrichter intern nur alle ~5 MINUTEN ab und
# liefert dazwischen denselben Cache-Wert. HTTP-Statusseite und Modbus
# (Solarman V5 auf Port 8899) lesen exakt dieselbe Zahl — schneller
# pollen bringt keine neue Information, nur mehr Fehlversuche auf einer
# WLAN-Strecke, die ohnehin ~10% Aussetzer hat. Der Wert ist damit gut
# fuers Monitoring/Energie-Dashboard, aber NIEMALS echtzeitfaehig genug
# fuer den 0,5s-Regelkreis — der sieht die Deye-Leistung ohnehin sofort
# in der gemessenen Netzleistung.
DEYE_HOST = os.environ.get("DEYE_HOST", "")
DEYE_USER = os.environ.get("DEYE_USER", "admin")
DEYE_PASS = os.environ.get("DEYE_PASS", "admin")
DEYE_POLL_S = float(os.environ.get("DEYE_POLL_S", "60"))
# Seriennummer des WLAN-Loggers (steht auf dem Stick und in der Weboberflaeche).
# Gesetzt -> Modbus/Solarman V5 wird bevorzugt, sonst nur die HTML-Statusseite.
# Steht der Logger auf yz_tmode=throughput, ist NUR noch der Modbus-Pfad
# moeglich (und dann echtzeitfaehig — DEYE_POLL_S darf runter auf 5).
DEYE_LOGGER_SN = int(os.environ.get("DEYE_LOGGER_SN", "0") or 0)
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
# Netz-Sollwert. Mit Akku wird er ZUSTANDSABHAENGIG: ist der Speicher voll,
# darf ruhig etwas ins Netz laufen (der Ueberschuss waere sonst eh
# abgeregelt) — ist er leer, wollen wir jede Wattstunde selbst behalten und
# nehmen lieber ein paar Watt Netzbezug in Kauf, statt den Akku zu
# verheizen. Interpoliert linear ueber die Akku-Spannung zwischen
# BATT_LOW_V (leer -> TARGET_GRID_W) und BATT_HIGH_V (voll ->
# TARGET_GRID_FULL_W). Ohne konfigurierten Akku gilt TARGET_GRID_W.
TARGET_GRID_W = int(os.environ.get("TARGET_GRID_W", "20"))
TARGET_GRID_FULL_W = int(os.environ.get("TARGET_GRID_FULL_W", "-50"))


def target_grid(state: dict) -> int:
    v = state.get("batt_v")
    if not BATT_STRINGS or v is None or BATT_HIGH_V <= BATT_LOW_V:
        return TARGET_GRID_W
    f = max(0.0, min(1.0, (v - BATT_LOW_V) / (BATT_HIGH_V - BATT_LOW_V)))
    return int(round(TARGET_GRID_W + f * (TARGET_GRID_FULL_W - TARGET_GRID_W)))
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
# Max. kWh-Zuwachs pro Lesung — physikalisch 1 (Zaehler zaehlt ganze kWh)
MAX_KWH_STEP = 1
# DIE MONOTONIE-INVARIANTE: Der Zaehler laeuft physikalisch NIE rueckwaerts.
# Ein zu HOHER Stand kann nur durch akzeptierte +1-Schritte entstehen (jeder
# doppelt bestaetigt), also ist er hoechstens um +1 zu hoch — mehr
# als KWH_HEAL_MAX nach unten zu "heilen" ist in JEDEM Fall ein Lesefehler,
# egal wie viele Zeugen das Bild bestaetigen (28.07.: Morgenschatten loescht
# Segment B der 9, kNN UND Segment-Dekoder lasen uebereinstimmend 35850
# statt 35890 — dieselbe Optik ist kein unabhaengiger Zeuge). Senkungen
# innerhalb von KWH_HEAL_MAX brauchen zwingend Gemini als bild-fremden
# Zeugen; groessere Senkungen sind verboten. Punkt.
# Warum 1: ein Aufwaertsschritt braucht ZWEI konsistente Lesungen von exakt
# Stand+1 — eine Geister-Fehllesung vergiftet also hoechstens +1. Fuer +2
# muesste sich danach ein zweiter exakter Doppel-Fehler anschliessen,
# waehrend das System bereits Alarm schlaegt (nie beobachtet). Und greift
# die -1-Heilung je faelschlich, steht der echte Zaehler sofort +1 ueber
# dem Stand und der normale +1-Pfad korrigiert binnen Minuten.
KWH_HEAL_MAX = int(os.environ.get("KWH_HEAL_MAX", "1"))
# STRUKTUR-DECKEL: Das Display hat 6 Vorkomma-Stellen MIT fuehrender Null
# (035891) — ein Zaehlerstand > 99999 ist strukturell unmoeglich, egal wie
# viele Leser ihn bestaetigen. 28.07. ~09:40: Gemini las die Nachkommastelle
# mit (35891.4 -> "358914") und bestaetigte im Re-Baseline seinen eigenen
# Fehler -> Stand sprang auf 358914 (+323023 kWh). Nie wieder.
KWH_ABS_MAX = int(os.environ.get("KWH_ABS_MAX", "99999"))
# PHYSIK-DECKEL fuer Aufwaerts-Heilungen: schneller als der HAUSANSCHLUSS
# (3x35A ~ 24 kW) kann der Zaehler nicht steigen — DAS ist die physische
# Grenze, nicht der historische Schnitt. Ein frueherer Wert von 5 (aus
# p99 = 2,2 kWh/h abgeleitet) blockierte legitime Lastspitzen: eine
# 11-kW-Wallbox-Nachtladung fror die Regelung 5,9 h im Failsafe ein
# (Befund der 2. Angriffsrunde). Lehre: Deckel duerfen nur Physik
# kodieren, nie Gewohnheit. 358914 braeuchte auch mit 25 noch 538 Tage.
KWH_MAX_RATE_KWH_H = float(os.environ.get("KWH_MAX_RATE_KWH_H", "25.0"))
# WIR HABEN ZEIT: Der Zaehler tickt ~1 kWh pro 20-30 min. Ein Re-Baseline
# (= Korrektur des gespeicherten Stands!) muss nicht in Sekunden fallen —
# der Kandidat muss mindestens diese Spanne lang konsistent gelesen werden,
# BEVOR ueberhaupt ein Zeuge gefragt wird. Einzelne Fehl-Frames und kurze
# Stoerungen (Schattenwanderung, Reflexe) ueberleben keine 3 Minuten.
REBASE_MIN_SPAN_S = float(os.environ.get("REBASE_MIN_SPAN_S", "180"))
REBASE_MIN_COUNT = 4
# KUMULATIVES PHYSIK-FENSTER: der Einzel-Deckel allein liess sich ratschen
# (+1 je Re-Baseline alle 3 min = 20 kWh/h). Deshalb merkt sich kwh_hist
# die akzeptierten Staende der letzten 6 h, und JEDER Anstieg muss auch
# gegen jeden dieser Punkte unter Rate x Zeit + 1 bleiben.
KWH_RATE_WINDOW_S = float(os.environ.get("KWH_RATE_WINDOW_S", str(6 * 3600)))
# Blindflug-Deckel: aeltere Zeitstempel (RTC-Sprung, Uhr ohne NTP) duerfen
# den Physik-Deckel nicht ins Unendliche oeffnen.
KWH_ELAPSED_MAX_H = float(os.environ.get("KWH_ELAPSED_MAX_H", "72"))
# LETZTER AUSWEG bei totem Gemini: ist der Stand verloren, der Boden
# vergiftet und Gemini seit so vielen Stunden DURCHGEHEND ausgefallen
# (nie widersprochen!), darf eine 4x ueber >=10 min konsistente lokale
# Lesung mit deutlicher Segment-Bestaetigung die Basis setzen. Bewusst
# laenger als die laengste beobachtete Schattenphase (~2 h am Morgen).
GEMINI_DEAD_GRACE_H = float(os.environ.get("GEMINI_DEAD_GRACE_H", "6"))
# Der Schiedsrichter darf RATEN VERWEIGERN: der Segment-Dekoder liefert pro
# Zelle einen Log-Likelihood-Abstand zum zweitbesten Muster. Bei Ghost-
# Fehllesungen (Phantom-Segmente in der Schattenzone) faellt der auf 0.03-0.09,
# bei korrekten Lesungen liegt er 3-20x hoeher. Auf 403 gelabelten Frames
# gemessen: Schwelle 0.8 hebt die Treffsicherheit von 76% auf 95% bei noch
# 60% Abdeckung. Schweigen ist fuer eine Zweitmeinung billiger als Raten.
SEG_MIN_CONF = float(os.environ.get("SEG_MIN_CONF", "0.8"))
# Der Schiedsrichter entscheidet nicht per offenem Lesen, sondern als
# HYPOTHESENTEST zwischen den beiden einzig moeglichen Kandidaten (Stand,
# Stand+1). Gemessen an 1294 Frames: offenes Lesen ist an der rechten
# Schattenzone nicht sicher zu bekommen (Slot 5 braeuchte conf>=3.9, das
# schaffen 11% der Frames). Der Zweiwege-Test dagegen irrt in der
# GEFAEHRLICHEN Richtung (faelschlich "+1") nur bei 0.4% der Frames, wenn
# man eine Log-Likelihood-Marge von 6 verlangt — mit den zwei geforderten
# konsistenten Lesungen bleibt ein Restrisiko von ~1:60000.
SEG_UP_MARGIN = float(os.environ.get("SEG_UP_MARGIN", "6"))
# Wie oft der unabhaengige Segment-Dekoder den akzeptierten Stand prueft
SEG_WATCH_EVERY = int(os.environ.get("SEG_WATCH_EVERY", "200"))
# Ansteuerbare Untergrenze. Gemessen an 929 Limit-Kommandos: der HMS FOLGT
# einem Limit unter ~500W nur unzuverlaessig (250-300W: 25%, 350-400W: 67%,
# 450-500W: 90%, 500-600W: 99.7%). Er kann niedrige Leistung durchaus HALTEN
# (stabile Plateaus bei 157W, 320W, 424W ueber Minuten) — er findet per
# Kommando nur nicht dorthin: der MPPT verliert den Arbeitspunkt und faellt
# in einen Attraktor bei ~157W. Folge nachts (Last ~390W): der Regler jagt
# ein Ziel unterhalb der Grenze, wirft den Inverter dabei staendig aus dem
# Tritt (1667 Limitwechsel in 5,2h) und das Netz zahlt.
# Fix: Ziele unterhalb des Floors werden NICHT angesteuert — stattdessen
# bleibt das Limit auf dem Floor stehen und der Inverter in Ruhe. Preis ist
# etwas Ueberschuss-Einspeisung; bei Nachtlast ~390W und Floor 430W sind das
# ~40W. Simulation ueber die echte Lastkurve: Netzbezug 1,65 -> 0,32 kWh
# pro Nacht. 0 = aus (dann regelt er wie bisher bis MIN_LIMIT_W runter).
SUSTAIN_FLOOR_W = int(os.environ.get("SUSTAIN_FLOOR_W", "430"))
# Glaettungsfenster fuer die Schlafen/Halten-Entscheidung am Floor
FLOOR_SMOOTH_S = float(os.environ.get("FLOOR_SMOOTH_S", "12"))
# MPPT-Stuck-Kick: der HMS verklemmt sich an der Batterie gelegentlich weit
# unter dem Limit (z.B. 178W bei Limit 420) und reagiert auf kleine Schritte
# kaum — ein grosser Limit-Sprung zwingt den Tracker zum Neu-Akquirieren,
# danach laeuft er auch auf niedrigeren Limits normal. Detektion: Bezug ueber
# Deadband + Limit deutlich ueber Ist + keine Bewegung ueber STUCK_S.
STUCK_S = float(os.environ.get("STUCK_S", "25"))
STUCK_GAP_W = int(os.environ.get("STUCK_GAP_W", "150"))
# Schwankungsbreite, unter der die Leistung als "steht" gilt. Im Attraktor
# liegt sie bei 0,2W, beim echten Nachfuehren bei zig Watt.
STUCK_FLAT_W = float(os.environ.get("STUCK_FLAT_W", "8"))
KICK_COOLDOWN_S = float(os.environ.get("KICK_COOLDOWN_S", "180"))
# Eskalationstreppe statt Verdopplung: Schwelle, ab der der Tracker sich
# loest, ist unbekannt — wir tasten uns hoch und LOGGEN den loesenden
# Schritt (ev=kick_result), um die HMS-Schwelle zu vermessen.
# Aufwach-Sprung, datenbasiert (160 kick_result-Events, 111 echte
# Aufwacher): der Inverter schlaeft bei ~157W median und die noetige
# Sprunghoehe haengt weder am Basis-Limit noch an der Schlaftiefe. +100
# weckt nur 48%, +400 aber 94% beim ERSTEN Versuch — also gross starten
# statt langsam eskalieren (spart ~20-30s pro Aufwachen). +800 als
# Fallback fuer die restlichen 6%. Der kurze Puls (~+380W) ist mit Akku
# egal; der runter-Pfad holt ihn in Sekunden zurueck.
KICK_STEPS_W = (400, 800)
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



def seg_decide(*candidates: int) -> tuple[int | None, float]:
    """Hypothesentest des Segment-Dekoders zwischen mehreren kWh-Kandidaten.

    Unabhaengig vom kNN (geometrische Segment-Abtastung) und ohne Cloud.
    Liefert (None, 0) wenn KEINER der Kandidaten zum Bild passt — genau das
    ist die Selbstkontrolle, die am 26.07. fehlte."""
    global _seg_reader
    if _last_snapshot is None or _local_reader is None:
        return None, 0.0
    try:
        import cv2
        import numpy as np
        if _seg_reader is None:
            from seg_decoder import SegReader
            _seg_reader = SegReader(anchor_ref=_local_reader.ex._anchor_ref)
        gray = cv2.imdecode(np.frombuffer(_last_snapshot, np.uint8),
                            cv2.IMREAD_GRAYSCALE)
        return _seg_reader.score_candidates(gray, list(candidates))
    except Exception as e:
        print(f"Seg-Hypothesentest: {e}", file=sys.stderr)
        return None, 0.0


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
        # 1) Offenes Lesen NUR als Veto: liest der Dekoder selbstbewusst
        #    etwas ausserhalb des Erwartungsfensters, ist vermutlich der
        #    gespeicherte Stand veraltet — dann schweigen und die
        #    Re-Baseline (mit Gemini) uebernehmen lassen.
        labels, confs, _ = _seg_reader.read_cells(gray)
        kwh_s = "".join(labels[:6])
        if kwh_s.isdigit() and set(kwh_s) == {"8"}:
            print("Seg-Schiedsrichter: LCD-Segmenttest — schweigt",
                  file=sys.stderr)
            return None
        if (kwh_s.isdigit() and min(confs[:6]) >= SEG_MIN_CONF
                and not (expected_lo <= int(kwh_s) <= expected_hi)):
            print(f"Seg-Schiedsrichter: liest {kwh_s} ausserhalb "
                  f"[{expected_lo},{expected_hi}] — Stand evtl. veraltet, "
                  f"schweigt", file=sys.stderr)
            return None
        # 2) Entscheidung als Zweiwege-Hypothesentest (s.o.)
        cands = sorted({expected_lo, expected_hi})
        best, margin = _seg_reader.score_candidates(gray, cands)
        if best is None:
            return None
        if best > expected_lo and margin < SEG_UP_MARGIN:
            print(f"Seg-Schiedsrichter: Zuwachs auf {best} nur mit Marge "
                  f"{margin:.1f} < {SEG_UP_MARGIN} — schweigt", file=sys.stderr)
            return None
        kwh = best
        # "Kein Zuwachs" darf sofort bestaetigt werden — das ist die
        # konservative Aussage (Stand bleibt, kann nichts vergiften).
        # Ein ZUWACHS (+1) braucht zwei konsistente Lesungen: eine einzelne
        # Ghost-Fehllesung (Phantom-Segmente in der Schattenzone) darf den
        # Stand nie hochziehen — genau das passierte am 24.07. um 00:04.
        now = time.time()
        if state is not None and kwh > expected_lo:
            cand, cand_ts, cand_n = state.get("seg_cand", (0, 0.0, 0))
            if now - cand_ts > 1800 or cand != kwh:
                cand, cand_n = kwh, 0
            cand_n += 1
            state["seg_cand"] = (cand, now, cand_n)
            if cand_n < 2:
                return None
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
        if local is not None and conf >= OCR_MIN_CONF:
            # Bei Abweichung gewinnt die KONFIDENTE lokale Lesung. Am 28.07.
            # ueberschrieb hier Geminis Nachkommastellen-Fehler (358914) im
            # Kreuz-Check die korrekte lokale 35891 — und bestaetigte sich
            # spaeter im Re-Baseline selbst. Gemini bleibt Zeuge und
            # Fallback, aber nie Ueberstimmer einer konfidenten Lesung.
            return local, f"local c={conf:.2f} (cross-check)"
        return gem, "gemini" + (" (cross-check)" if cross_check else "")
    return gemini_read(img), "gemini"


def witness_match(gem_kwh: int, kwh: int) -> bool:
    """Zaehlt eine Gemini-Aussage als EXAKTE Bestaetigung des Kandidaten?

    Frueher stand hier eine Normalisierung (kwh//10) direkt in
    gemini_read — die wusch aber JEDEN 6-stelligen Geistermuell in
    gueltige Staende (999999 -> 99999, 585870 -> 58587) und entzog dem
    Struktur-Deckel genau die Faelle, fuer die er gebaut wurde. Deshalb:
    Gemini-Lesungen bleiben ROH (der Struktur-Deckel verwirft sie als
    Messung), und nur hier, beim Zeugen-Vergleich, wird die exakte
    Nachkomma-Signatur anerkannt: 35891.4 -> "358914" bestaetigt den
    Kandidaten 35891, weil gem == kandidat*10 + zehntel. Jede andere
    Beziehung ist keine Bestaetigung."""
    if gem_kwh == kwh:
        return True
    if (gem_kwh > KWH_ABS_MAX and set(str(gem_kwh)) != {"8"}
            and gem_kwh // 10 == kwh):
        print(f"Gemini-Zeuge per Nachkomma-Signatur: {gem_kwh} "
              f"bestaetigt {kwh}", file=sys.stderr)
        return True
    return False


_gemini_err_since: float | None = None
_gemini_ok_ts: float | None = None
_gemini_err_n: int = 0


def gemini_read(img: bytes) -> dict:
    """Bild von Gemini lesen lassen; protokolliert Dauer-Ausfaelle.
    Wirft Exception bei Fehler. WICHTIG: ein WIDERSPRUCH (andere Zahl)
    ist kein Ausfall — nur echte Fehler zaehlen fuer den Notausweg.
    Der Fehler-ZAEHLER schuetzt die 6h-Uhr vor Uhrenspruengen: ein
    NTP-Vorwaertssprung kann die Uhr kuenstlich altern lassen, aber
    keine 20 realen Fehlversuche herbeizaubern (Runde 5, #7)."""
    global _gemini_err_since, _gemini_ok_ts, _gemini_err_n
    try:
        r = _gemini_read_raw(img)
        _gemini_err_since = None
        _gemini_err_n = 0
        _gemini_ok_ts = time.time()
        return r
    except Exception:
        if _gemini_err_since is None:
            _gemini_err_since = time.time()
        _gemini_err_n += 1
        raise


def _gemini_read_raw(img: bytes) -> dict:
    """Bild von Gemini lesen lassen. Wirft Exception bei Fehler."""
    body = {
        "contents": [{
            "parts": [
                {"text": GEMINI_PROMPT},
                {"inline_data": {"mime_type": "image/jpeg",
                                 "data": base64.b64encode(img).decode()}},
            ]
        }],
        # KEIN thinkingConfig: die 3.x-Modelle lehnen thinkingBudget=0 mit
        # HTTP 400 ab. Das legte am 26.07. den unabhaengigen Zeugen still —
        # ohne Gemini konnte niemand dem kNN widersprechen, und der falsche
        # Zaehlerstand hielt sich stundenlang.
        "generationConfig": {"temperature": 0},
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
        if r.status_code == 400:
            # Ungueltiges Argument: einmal ohne generationConfig versuchen,
            # bevor rotiert wird (Modelle unterscheiden sich darin)
            r2 = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"Content-Type": "application/json", "X-goog-api-key": key},
                json={"contents": body["contents"]}, timeout=30)
            if r2.status_code == 200:
                print(f"{model}: 400 mit generationConfig — ohne akzeptiert",
                      file=sys.stderr)
                r = r2
        if r.status_code in (400, 404, 429, 503):
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
    """Gibt Fehlergrund zurueck oder None wenn die Lesung plausibel ist.

    WICHTIG — die Pruefungen fuer kWh und W sind streng getrennt. Frueher
    liefen sie in einer linearen Kette mit fruehen returns, und der
    W-Heilpfad gab bei Erfolg direkt None zurueck. Damit wurde die
    kWh-Pruefung UEBERSPRUNGEN: am 26.07. flatterte W wegen einer 3->9-
    Fehllesung (+6000 W), die W-Re-Baseline griff, und im selben Atemzug
    rutschte ein um 20 kWh zu niedriger Zaehlerstand ungeprueft durch
    (35881 -> 35861 -> 35801). Ein Heilpfad darf immer nur seinen EIGENEN
    Kanal freigeben."""
    kwh, w = reading["kwh"], reading["w"]
    if kwh == 888888 or abs(w) in (88888, 888888):
        return "LCD-Segmenttest (alles 8er)"
    if kwh > KWH_ABS_MAX:
        # Struktur-Deckel: 6 Stellen mit fuehrender Null -> mehr als 5
        # signifikante Ziffern KANN das Display nicht zeigen. Faengt die
        # ganze Fehlerklasse "Nachkommastelle/Geisterziffer angehaengt"
        # (358914, 585870, 880080), bevor sie ueberhaupt Kandidat wird.
        return (f"kWh {kwh} > {KWH_ABS_MAX} strukturell unmoeglich "
                f"(Display: 6 Stellen mit fuehrender Null)")
    if kwh <= 0:
        return "kwh<=0 — LCD vermutlich dunkel/unlesbar"
    if abs(w) > 20000:
        return f"unplausible Leistung {w} W"
    return _plausible_kwh(kwh, state) or _plausible_w(w, state)


def _plausible_kwh(kwh: int, state: dict) -> str | None:
    if state.get("kwh") is not None:
        if kwh < state["kwh"]:
            return f"kWh rückläufig ({state['kwh']} -> {kwh})"
        # Bei ~1,4s-Zyklus kann der Zaehler NIE um mehr als 1 steigen. Die
        # alte Toleranz +2 liess genau die Ghost-Fehllesung durch, die der
        # Segment-Dekoder in der Schattenzone produziert (letzte Ziffer
        # 1 -> 3 durch Phantom-Segmente): 24.07. 00:04 wurde 35873
        # akzeptiert, obwohl kNN UND Gemini 35871 lasen -> 2h Ablehnungen.
        if kwh > state["kwh"] + MAX_KWH_STEP:
            return f"kWh-Sprung ({state['kwh']} -> {kwh})"
    return None


def _plausible_w(w: int, state: dict) -> str | None:
    if state.get("w") is not None and abs(w - state["w"]) <= MAX_JUMP_W:
        state.pop("wjump", None)   # sauberer Wert -> Heil-Zaehler zuruecksetzen
    if state.get("w") is not None and abs(w - state["w"]) > MAX_JUMP_W:
        # Heilpfad: 4 konsistente Lesungen auf neuem Niveau heissen, dass
        # der GESPEICHERTE Stand vergiftet war (23.07.: Geister-8443 beim
        # Erststart -> jede echte Lesung "Sprung >5000" -> Deadlock).
        cand, n = state.get("wjump", (None, 0))
        if cand is not None and abs(w - cand) <= max(100, abs(cand) // 5):
            n += 1
            if n >= 4:
                # Konsistenz allein reicht NICHT: ein systematischer
                # Lesefehler ist per Definition konsistent (26.07.: die
                # fuehrende 3 wurde konstant als 9 gelesen, 3075 -> 9075).
                # Deshalb muss ein unabhaengiger Zeuge zustimmen.
                if not w_second_opinion(w):
                    state["wjump"] = (cand, 0)
                    return (f"Sprung {w - state['w']:+d} W — zweite Meinung "
                            f"widerspricht")
                state.pop("wjump", None)
                print(f"W-Re-Baseline: {state['w']} W war vergiftet, "
                      f"4x konsistent ~{w} W + bestaetigt -> uebernehme")
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
    return None

def w_second_opinion(w: int) -> bool:
    """Unabhaengige Bestaetigung eines neuen W-Niveaus (+-20%).

    Erst Gemini (bester Zeuge), sonst der Segment-Dekoder. Ohne das wuerde
    eine systematische Fehllesung sich selbst bestaetigen."""
    global _last_gemini_call
    if _last_snapshot is not None and time.time() - _last_gemini_call >= GEMINI_COOLDOWN_S:
        try:
            _last_gemini_call = time.time()
            gem = gemini_read(_last_snapshot)
            ok = abs(gem["w"] - w) <= max(50, abs(w) // 5)
            print(f"W-Zweitmeinung Gemini: {gem['w']} W vs {w} W -> "
                  f"{'bestaetigt' if ok else 'WIDERSPRUCH'}", file=sys.stderr)
            return ok
        except Exception as e:
            print(f"W-Zweitmeinung Gemini nicht verfuegbar: {e}", file=sys.stderr)
    try:
        import cv2
        import numpy as np
        global _seg_reader
        if _seg_reader is None and _local_reader is not None:
            from seg_decoder import SegReader
            _seg_reader = SegReader(anchor_ref=_local_reader.ex._anchor_ref)
        if _seg_reader is None or _last_snapshot is None:
            return False
        gray = cv2.imdecode(np.frombuffer(_last_snapshot, np.uint8),
                            cv2.IMREAD_GRAYSCALE)
        labels, confs, _ = _seg_reader.read_cells(gray)
        ws = "".join(labels[6:]).replace("_", "")
        if not ws.lstrip("-").isdigit() or min(confs[6:]) < SEG_MIN_CONF:
            return False
        ok = abs(int(ws) - w) <= max(50, abs(w) // 5)
        print(f"W-Zweitmeinung Segment: {ws} W vs {w} W -> "
              f"{'bestaetigt' if ok else 'WIDERSPRUCH'}", file=sys.stderr)
        return ok
    except Exception:
        return False


KWH_DOWN_MAX_24H = int(os.environ.get("KWH_DOWN_MAX_24H", "3"))


def _source_ok(source: str) -> bool:
    """Zeugen-Trennung gilt nur, wenn es ueberhaupt ZWEI Quellen gibt:
    im reinen Gemini-Betrieb (kein lokales OCR geladen) ist Gemini
    zwangslaeufig Kandidat UND Zeuge — eine harte Trennung fror dort
    jede Luecke >= 2 kWh permanent ein (Runde 5, #9)."""
    return source.startswith("local") or _local_reader is None


def down_budget_ok(state: dict, amount: int) -> bool:
    """SENKUNGS-BUDGET: hoechstens KWH_DOWN_MAX_24H kWh Heilung nach
    unten je 24 h. Jede einzelne -1 braucht zwar einen exakten Zeugen,
    aber ein systematisch mitluegender Zeuge konnte -1 je 3-min-Runde
    ratschen (-30 kWh in 1,75 h, Runde 5 #3). Ein echter Zaehler laeuft
    nie rueckwaerts — Heilungen sind Fehlerkorrekturen um +-1, drei am
    Tag sind grosszuegig."""
    now = time.time()
    dh = state.setdefault("down_hist", [])
    dh[:] = [e for e in dh if now - e[0] < 24 * 3600]
    return sum(e[1] for e in dh) + amount <= KWH_DOWN_MAX_24H


def down_note(state: dict, amount: int) -> None:
    state.setdefault("down_hist", []).append([time.time(), amount])


def kwh_rate_ok(state: dict, new_kwh: int) -> bool:
    """KUMULATIVES Physik-Fenster: new_kwh muss gegen JEDEN akzeptierten
    Stand der letzten 6 h unter Rate x Zeit + 1 bleiben. Der Einzel-Deckel
    in rebaseline liess sich sonst ratschen (+1 alle 3 min = 20 kWh/h)."""
    now = time.time()
    hist = state.setdefault("kwh_hist", [])
    while hist and now - hist[0][0] > KWH_RATE_WINDOW_S:
        hist.pop(0)
    # Eintraege AUS DER ZUKUNFT (NTP-Rueckwaertskorrektur) fliegen raus:
    # sie verfielen nie und froren den Kanal auf +1 ein, bis die Uhr sie
    # eingeholt hatte (Runde 4, #4).
    hist[:] = [e for e in hist if e[0] <= now + 60]
    for ts0, k0 in hist:
        # dt-Klemme: Eintraege "aus der Zukunft" (Uhrensprung rueckwaerts)
        # zaehlen als gleichzeitig -> strengster Fall, loest sich mit der
        # Zeit von selbst. NIE in die andere Richtung klemmen — eine
        # Lade-Klemme auf 'jetzt' riss bei nachgehender Boot-Uhr nach dem
        # NTP-Sprung den Deckel auf (Befund der 2. Angriffsrunde).
        if new_kwh - k0 > KWH_MAX_RATE_KWH_H * max(0.0, now - ts0) / 3600 + 1:
            return False
    return True


def kwh_hist_push(state: dict, kwh: int) -> None:
    hist = state.setdefault("kwh_hist", [])
    # Auch bei UNVERAENDERTEM Wert alle 5 min einen Anker setzen: auf
    # einem Zaehler-Plateau (Nulleinspeisung am Sonnentag) alterte sonst
    # der einzige Eintrag aus dem Fenster heraus und der letzte
    # Persist-Zeitpunkt wurde zum Physik-Anker (Runde 4: Neustart-Sturm
    # + Plateau -> +100 kWh als EIN Sprung; Runde 5 #2: 20-min-Anker
    # gab noch ~+8 kWh Plateau-Kredit — 5 min druecken das auf ~+2).
    if (not hist or hist[-1][1] != kwh
            or time.time() - hist[-1][0] > 300):
        hist.append([time.time(), kwh])
        del hist[:-100]


def local_escape(reading: dict, state: dict, source: str) -> bool:
    """LETZTER Ausweg aus einem vergifteten Boden (Stand verloren, Basis-
    Fenster passt nicht, Gemini als Zeuge nicht verfuegbar): NUR wenn
    Gemini seit >= GEMINI_DEAD_GRACE_H durchgehend AUSGEFALLEN ist (ein
    Widerspruch zaehlt nicht als Ausfall!), das lokale kNN den Kandidaten
    4x ueber >= 10 min konsistent liest UND der Segment-Dekoder ihn mit
    deutlicher Marge stuetzt. Ohne diesen Pfad stand das System im
    Deadlock-Szenario 72 h simuliert im Failsafe, obwohl beide lokalen
    Verfahren sich einig waren. Die Grace-Zeit liegt bewusst ueber der
    laengsten beobachteten Schattenphase (~2 h) — waehrend des Morgen-
    schattens antwortet Gemini ausserdem (Widerspruch), was den Pfad
    sofort schliesst."""
    kwh = reading["kwh"]
    now = time.time()
    if kwh > KWH_ABS_MAX or kwh <= 0 or not source.startswith("local"):
        return False
    # Der Notausweg ist KEINE Generalvollmacht (2. Angriffsrunde: er war
    # eine zeugenlose Tuer in beide Richtungen, -800 und +4000 kWh):
    #  - nie unter den letzten ECHTEN Stand (kwh_lost) minus Heilung —
    #    nur ein rein abgeleiteter Boden (Korruptur-Heilung, kwh_lost
    #    fehlt) darf unterschritten werden, dafuer existiert der Pfad;
    #  - nach oben gilt die Physik genauso wie ueberall.
    lost = state.get("kwh_lost")
    if lost is not None and kwh < lost - KWH_HEAL_MAX:
        return False
    anker = lost if lost is not None else state.get("kwh_floor")
    # Blindflug-Anker: die aeltere der beiden Uhren. el_h nur ab der
    # Watchdog-FREIGABE zu rechnen vergass einen echten 72h-Ausfall
    # davor — die Heilung hing dann Stunden (Runde 4, #10).
    ts0 = min(state.get("kwh_floor_ts", now), state.get("kwh_ts", now))
    el_h = min(KWH_ELAPSED_MAX_H, max(0.0, now - ts0) / 3600)
    if anker is not None and kwh > anker + max(
            MAX_KWH_STEP, KWH_MAX_RATE_KWH_H * el_h):
        return False
    # "Durchgehend tot" heisst: seit >= Grace KEIN einziger Gemini-ERFOLG.
    # Eine sporadisch durchkommende Antwort ist ein echter Zeuge und
    # gehoert in den Re-Baseline — der Notausweg bleibt dann zu.
    if (_gemini_err_since is None
            or now - _gemini_err_since < GEMINI_DEAD_GRACE_H * 3600
            or _gemini_err_n < 20
            or (_gemini_ok_ts is not None
                and now - _gemini_ok_ts < GEMINI_DEAD_GRACE_H * 3600)):
        return False
    # Zaehler VERFALLEN: 4 Geisterframes ueber Stunden verstreut sind
    # keine Konsistenz — laenger als 30 min ohne Wiederholung -> von vorn.
    esc = state.setdefault("esc_counts", {})
    n, first, last = esc.get(kwh, (0, now, now))
    if now - last > 1800:
        n, first = 0, now
    esc[kwh] = (n + 1, first, now)
    if len(esc) > 20:
        state["esc_counts"] = esc = {kwh: esc[kwh]}
    n, first, last = esc[kwh]
    if n < REBASE_MIN_COUNT or now - first < 600:
        return False
    best, margin = seg_decide(kwh, kwh + 1)
    # 0.8 = die kalibrierte REJECT_MARGIN des Dekoders (99,4 % Ghost-
    # Abweisung). Die alte harte 1.0 war willkuerlich: eine schwache,
    # aber RICHTIGE Segment-Stuetze blockierte die Heilung fuer immer
    # (Runde 4, #3: Marge 0,9 -> nie geheilt, auch nach 48 h nicht).
    if best != kwh or margin < 0.8:
        return False
    print(f"NOTAUSWEG: Gemini seit "
          f"{(time.time() - _gemini_err_since) / 3600:.1f}h tot, lokal 4x "
          f"konsistent {kwh} + Segment-Marge {margin:.2f} -> Basis gesetzt",
          file=sys.stderr)
    if SAVE_SAMPLES_DIR:
        save_event(SAVE_SAMPLES_DIR, _last_snapshot, "local_escape",
                   kwh=kwh, floor=state.get("kwh_floor"))
    state["esc_counts"] = {}
    return True


def rebaseline(reading: dict, state: dict, source: str = "local") -> bool:
    """Kommt dieselbe 'unplausible' kWh-Lesung mehrfach in Folge, wird sie
    per Gemini verifiziert und bei Bestaetigung als neuer Stand akzeptiert.
    Verhindert, dass eine einmal akzeptierte Fehl-Lesung alles blockiert."""
    global _last_gemini_call
    kwh = reading["kwh"]
    if kwh > KWH_ABS_MAX or kwh <= 0:
        return False   # strukturell unmoeglich — zaehlt nicht mal als Kandidat
    if not _source_ok(source):
        # ZEUGEN-TRENNUNG: Kandidat und Bestaetiger muessen verschiedene
        # Quellen sein. Am 28.07. wurde Geminis Fehllesung (358914) zum
        # Kandidaten, und Gemini "bestaetigte" dann sich selbst. Nur das
        # lokale OCR darf Kandidaten liefern; Gemini bleibt reiner Zeuge.
        return False
    # Zaehler JE KANDIDAT mit Erst-Zeitstempel: eingestreute Dunkel-Fehl-
    # Lesungen (500, 3570...) duerfen den Konsens nicht zuruecksetzen, und
    # der Kandidat muss ueber REBASE_MIN_SPAN_S konsistent bleiben — ein
    # Re-Baseline korrigiert den STAND, das darf Minuten dauern.
    counts = state.setdefault("rb_counts", {})
    now = time.time()
    ent = counts.get(kwh, (0, now, now))
    n, first, last = ent if len(ent) >= 3 else (ent[0], ent[1], ent[1])
    if now - last > 900:
        # KONSISTENZ verfaellt: 4 Geisterframes ueber Tage verstreut sind
        # kein Konsens (Runde 4, #7 — dieselbe Regel wie bei esc_counts).
        n, first = 0, now
    counts[kwh] = (n + 1, first, now)
    if len(counts) > 20:
        state["rb_counts"] = counts = {kwh: counts[kwh]}
    n, first, last = counts[kwh]
    if n < REBASE_MIN_COUNT or now - first < REBASE_MIN_SPAN_S:
        return False
    # Lokaler Heilpfad: bestaetigt der Segment-Dekoder unabhaengig dieselbe
    # kWh, sind sich zwei voellig verschiedene Leseverfahren einig — das
    # reicht und braucht keine Cloud. Ohne diesen Pfad haing das System am
    # 26.07. fest, weil ein vergifteter Stand (35801 statt 35881) vom kNN
    # konsistent gelesen wurde und Gemini gleichzeitig ausfiel.
    alt = state.get("kwh")
    if alt is None:
        # AUFWAERTS-ANKER AUCH OHNE STAND (Runde 5, #0/#5): der ankerlose
        # Pfad hatte nur die kwh_lost-Schranke nach UNTEN — nach >= 6 h
        # ohne akzeptierte Lesung (Fenster leer) setzten 2 Gemini-
        # Bestaetigungen jeden Stand bis 99999 (+64108 im Repro). Der
        # Anker ist derselbe wie im Notausweg: letzter echter Stand bzw.
        # Boden plus Physik seit der aelteren Uhr.
        anker = state.get("kwh_lost")
        if anker is None:
            anker = state.get("kwh_floor")
        if anker is not None:
            ts0 = min(state.get("kwh_floor_ts", time.time()),
                      state.get("kwh_ts", time.time()))
            el_h = min(KWH_ELAPSED_MAX_H, max(0.0, time.time() - ts0) / 3600)
            cap = anker + max(MAX_KWH_STEP, KWH_MAX_RATE_KWH_H * el_h)
            if kwh > cap:
                print(f"Re-Baseline VERBOTEN: Basis {kwh} laege "
                      f"{kwh - anker} kWh ueber Anker {anker} — "
                      f"physikalisch unmoeglich (Deckel {cap:.0f})",
                      file=sys.stderr)
                state["rb_counts"] = {}
                if SAVE_SAMPLES_DIR and time.time() - state.get(
                        "rate_veto_ts", 0) > 60:
                    state["rate_veto_ts"] = time.time()
                    save_event(SAVE_SAMPLES_DIR, _last_snapshot,
                               "rate_veto", stored=anker, rejected=kwh)
                return False
    if alt is not None and kwh < alt - KWH_HEAL_MAX:
        # Monotonie-Invariante: so weit runter geht NIE. Kein Zeuge der Welt
        # macht aus einem Zaehler ein Geraet, das rueckwaerts laeuft.
        print(f"Re-Baseline VERBOTEN: {kwh} laege {alt - kwh} kWh unter dem "
              f"Stand {alt} (max. Heilung {KWH_HEAL_MAX}) — Lesefehler",
              file=sys.stderr)
        state["rb_counts"] = {}
        if SAVE_SAMPLES_DIR and time.time() - state.get("mono_veto_ts", 0) > 60:
            state["mono_veto_ts"] = time.time()
            save_event(SAVE_SAMPLES_DIR, _last_snapshot, "monotonic_veto",
                       stored=alt, rejected=kwh)
        return False
    if alt is not None and kwh < alt:
        # Kleine Senkung (<= KWH_HEAL_MAX): NUR mit Gemini als bild-fremdem
        # Zeugen. Der Segment-Dekoder reicht hier nicht — er sieht dieselbe
        # Optik wie das kNN und irrt im Schatten identisch.
        if not down_budget_ok(state, alt - kwh):
            print(f"Re-Baseline-Senkung abgelehnt: 24h-Senkungs-Budget "
                  f"({KWH_DOWN_MAX_24H} kWh) erschoepft", file=sys.stderr)
            return False
        if time.time() - _last_gemini_call < GEMINI_COOLDOWN_S:
            return False
        _last_gemini_call = time.time()
        try:
            gem = gemini_read(get_snapshot())
        except Exception as e:
            # Zeugen-FEHLER loescht den Konsens NICHT (flakiger Gemini
            # erzwang sonst alle 3 min eine neue 4x-Sammelrunde und
            # heilte nie) — der naechste freie Slot fragt sofort erneut.
            print(f"Re-Baseline-Senkung: Gemini nicht verfuegbar ({e}) — "
                  f"Stand bleibt", file=sys.stderr)
            return False
        state["rb_counts"] = {}
        if witness_match(gem["kwh"], kwh):
            print(f"Re-Baseline (Senkung um {alt - kwh}): Gemini "
                  f"bestaetigt {kwh} -> akzeptiert")
            return True
        print(f"Re-Baseline-Senkung abgelehnt: Gemini liest "
              f"{gem['kwh']}, nicht {kwh}", file=sys.stderr)
        return False
    if alt is not None and kwh > alt:
        # PHYSIK-DECKEL: Aufwaerts war bis 1.7.34 unbegrenzt ("Ausfall-
        # Heilung") — genau da kam 358914 durch. Der Zaehler kann seit der
        # letzten akzeptierten Lesung hoechstens Zeit x Hausanschluss
        # gestiegen sein. Echte Ausfall-Heilung skaliert mit — ein
        # 10h-Ausfall erlaubt +52 kWh, ein Geistersprung nie.
        elapsed_h = min(KWH_ELAPSED_MAX_H, max(
            0.0, time.time() - state.get("kwh_ts", time.time())) / 3600)
        # KEIN konstanter Schlupf (+2 war ein Konstruktionsfehler: er
        # dominierte den Deckel im Normalbetrieb und erlaubte eine
        # +3-Ratsche alle 3 min = 60 kWh/h). Im laufenden Betrieb ist
        # elapsed winzig -> Deckel = alt+1; nur echter Blindflug oeffnet ihn.
        cap = alt + max(MAX_KWH_STEP, KWH_MAX_RATE_KWH_H * elapsed_h)
        if kwh > cap:
            print(f"Re-Baseline VERBOTEN: {kwh} laege {kwh - alt} kWh ueber "
                  f"Stand {alt} — physikalisch unmoeglich in {elapsed_h:.1f}h "
                  f"(Deckel {cap:.0f})", file=sys.stderr)
            state["rb_counts"] = {}
            if SAVE_SAMPLES_DIR and time.time() - state.get("rate_veto_ts", 0) > 60:
                state["rate_veto_ts"] = time.time()
                save_event(SAVE_SAMPLES_DIR, _last_snapshot, "rate_veto",
                           stored=alt, rejected=kwh, elapsed_h=round(elapsed_h, 2))
            return False
    if alt is not None and alt != kwh and kwh <= alt + MAX_KWH_STEP:
        # Zwei getrennte Fragen ans Bild, beide muessen JA sagen:
        #   1. Wird der alte Stand widerlegt? (Hypothesentest liefert None)
        #   2. Wird der neue gestuetzt?
        # Der blosse Vergleich alt-gegen-neu reicht nicht — die Marge
        # zwischen zwei Kandidaten sagt nichts darueber, ob ueberhaupt
        # einer davon zum Bild passt. NUR fuer +1: groessere Sprunge
        # brauchen IMMER den bild-fremden Zeugen (Gemini) — der Segment-
        # Dekoder teilt die Optik des kNN und irrt mit ihm gemeinsam.
        alt_ok, _ = seg_decide(alt, alt + 1)
        neu, _ = seg_decide(kwh, kwh + 1)
        if alt_ok is None and neu == kwh:
            state["rb_counts"] = {}
            print(f"Re-Baseline: Segment-Dekoder widerlegt {alt} und "
                  f"bestaetigt {kwh} -> akzeptiert (ohne Cloud)")
            return True
    # Gemini-Cooldown gilt auch hier — aber der Zaehler bleibt stehen,
    # damit der naechste freie Slot sofort verifiziert
    if time.time() - _last_gemini_call < GEMINI_COOLDOWN_S:
        return False
    _last_gemini_call = time.time()
    # EXAKTER Match noetig (frueher +-2). Ohne Anker (Stand verloren,
    # alt=None) braucht es ZWEI exakte Bestaetigungen auf frischen
    # Snapshots — die duerfen aber UEBER ZYKLEN VERTEILT eintreffen
    # (30-min-Fenster): ein sporadisch erreichbarer Gemini (Quota) schaffte
    # sonst nie 2 Treffer am Stueck und heilte NIE — schlechter als ein
    # ganz toter. Zeugen-FEHLER lassen Konsens und Teil-Bestaetigung
    # stehen; nur ein WIDERSPRUCH setzt beides zurueck.
    # Doppel-Zeuge auch fuer RIESEN-Spruenge mit Anker (> 1 h Physik,
    # 25 kWh): der 72h-Blindflug-Deckel erlaubt bis +1800 in einem
    # Schritt — so ein Schritt braucht zwei exakte Bestaetigungen.
    need = 2 if (alt is None or kwh > alt + KWH_MAX_RATE_KWH_H) else 1
    ck, got, cts = state.get("rb_confirm", (None, 0, 0.0))
    # 6h-Frische statt 30-min-Klippe: an einem Quota-Tag kommen Gemini-
    # Erfolge > 30 min auseinander — got fiel dann ewig auf 0 zurueck
    # und ein SPORADISCHER Gemini heilte nie (Runde 4, #6). Der Kandidat
    # selbst muss dank Zaehler-Verfall trotzdem DURCHGEHEND gelesen sein.
    if ck != kwh or time.time() - cts > 6 * 3600:
        got = 0
    try:
        gem = gemini_read(get_snapshot())
    except Exception as e:
        print(f"Re-Baseline: Gemini nicht verfuegbar ({e}) — Konsens "
              f"bleibt stehen", file=sys.stderr)
        return False
    if not witness_match(gem["kwh"], kwh):
        print(f"Re-Baseline abgelehnt: Gemini liest {gem['kwh']}, "
              f"nicht {kwh}", file=sys.stderr)
        state["rb_counts"] = {}
        state.pop("rb_confirm", None)
        return False
    got += 1
    if got >= need:
        state["rb_counts"] = {}
        state.pop("rb_confirm", None)
        print(f"Re-Baseline: Gemini bestätigt kWh={gem['kwh']} "
              f"{got}x (alter Stand {state.get('kwh')}) -> akzeptiert")
        return True
    state["rb_confirm"] = (kwh, got, time.time())
    print(f"Re-Baseline: Bestaetigung {got}/{need} fuer {kwh} — "
          f"weitere folgt", file=sys.stderr)
    return False


def guard_kwh(reading: dict, source: str, state: dict) -> tuple[dict, str]:
    """ALLE kWh-Tore in einer Funktion — vom Hauptloop und von
    tests/test_kwh_gates.py identisch durchlaufen. Wirft ValueError, wenn
    die Lesung verworfen wird; sonst kommt die (ggf. korrigierte) Lesung
    plus Quelle zurueck. Torfolge: Plausibilitaet (Struktur-Deckel,
    Monotonie, Sprung) -> Seg-Schiedsrichter -> Re-Baseline (Zeugen-
    Trennung, Zeitspanne, Physik-Deckel) -> +1-Doppelbestaetigung ->
    Basis-Fenster nach Stand-Verlust -> Monotonie-Notbremse."""
    prev_kwh = state.get("kwh")
    prev_lost = state.get("kwh_lost")
    reason = plausible(reading, state)
    if (reason and state.get("kwh") is not None
            and ("rückläufig" in reason or "kWh-Sprung" in reason)):
        seg_kwh = seg_confirm(state["kwh"],
                              state["kwh"] + MAX_KWH_STEP, state)
        if seg_kwh is not None and seg_kwh <= KWH_ABS_MAX:
            # seg_kwh <= Deckel: bei Stand 99999 darf der Schiedsrichter
            # kein 100000 hineinreichen (einziger Pfad, der den Struktur-
            # Deckel umgehen konnte). Und der ersetzte Wert entbindet
            # NICHT von der W-Pruefung — plausible() hatte wegen des
            # kWh-Kurzschlusses den W-Kanal noch nie gesehen.
            print(f"Seg-Schiedsrichter: kWh {reading['kwh']} "
                  f"verworfen, Segment-Dekoder bestaetigt {seg_kwh}")
            reading = {**reading, "kwh": seg_kwh}
            reason = _plausible_w(reading["w"], state)
            source += " (seg)"
    if (reason and "kWh-Sprung" in reason
            and not kwh_rate_ok(state, reading["kwh"])):
        # Physik-Fenster VOR dem Re-Baseline: sonst wird ein Kandidat
        # erst teuer per Gemini bestaetigt und dann doch vom Fenster
        # verworfen — verbrannte Quota und geleerte Konsens-Zaehler.
        raise ValueError(f"verworfen: Physik-Fenster verletzt "
                         f"({reading['kwh']} zu schnell gestiegen)")
    if reason and ("rückläufig" in reason or "kWh-Sprung" in reason):
        # NUR kWh-Gruende duerfen den kWh-Re-Baseline anstossen. Der blosse
        # Teilstring "Sprung" matchte auch die W-Kanal-Gruende ("Sprung
        # +8675 W > 5000 W") — dann bestaetigte Gemini den (korrekten,
        # unveraenderten) kWh-Stand und gab damit den GEISTER-W frei:
        # exakt die Kanaltrennungs-Luecke vom 26.07., andere Richtung.
        if rebaseline(reading, state, source):
            # Auch hier: der kWh-Kurzschluss in plausible() hat den
            # W-Kanal dieses Frames nie geprueft — nachholen.
            reason = _plausible_w(reading["w"], state)
            source += " (re-baseline)"
    if reason:
        raise ValueError(f"verworfen: {reason}")
    # kWh-ERHOEHUNGEN erst nach 2 uebereinstimmenden Lesungen
    # uebernehmen: eine einzelne Fehl-Lesung an der Toleranzgrenze
    # (1->3: 35851->35853) vergiftete sonst den Stand und blockte
    # danach alles als "rueckläufig" (21.07.: 50min Failsafe)
    if (state.get("kwh") is not None and reading["kwh"] > state["kwh"]
            and "re-baseline" not in source and "(seg)" not in source):
        # Bestaetigung VERFAELLT nach 10 min: zwei gleiche Geisterframes
        # im Abstand von Stunden sind keine Doppelbestaetigung (Runde 4).
        if time.time() - state.get("kwh_pend_ts", 0) > 600:
            state.pop("kwh_pend", None)
            state.pop("kwh_pend_n", None)
        if state.get("kwh_pend") == reading["kwh"]:
            state["kwh_pend_n"] = state.get("kwh_pend_n", 1) + 1
        else:
            state["kwh_pend"], state["kwh_pend_n"] = reading["kwh"], 1
        state["kwh_pend_ts"] = time.time()
        if state["kwh_pend_n"] < 2:
            reading = {**reading, "kwh": state["kwh"]}
        else:
            state.pop("kwh_pend", None)
            state.pop("kwh_pend_n", None)
    if not kwh_rate_ok(state, reading["kwh"]):
        # Kumulatives 6h-Fenster — VOR allen Pfaden mit Seiteneffekten:
        # es stand frueher als letztes Tor, und ein von ihm verworfener
        # Frame hatte dann bereits Boden/kwh_lost gepoppt, rb_counts
        # geleert und Gemini-Aufrufe verbrannt (2. Angriffsrunde: danach
        # war der Stand zeugenlos frei setzbar). Ein Veto darf den
        # Zustand nicht anfassen.
        raise ValueError(f"verworfen: Physik-Fenster verletzt "
                         f"({reading['kwh']} zu schnell gestiegen)")
    global _last_gemini_call
    floor = state.get("kwh_floor")
    if state.get("kwh") is None and floor is not None:
        # BASIS-FENSTER nach Stand-Verlust (Watchdog / korrupter State):
        # die neue Basis muss zwischen Monotonie-Boden und Physik-Deckel
        # liegen. Ausserhalb bleibt der volle Re-Baseline-Weg (4x
        # konsistent ueber 3 Minuten + Gemini doppelt-exakt) — bzw. bei
        # lange totem Gemini der enge Notausweg. KEIN konstanter Schlupf:
        # frisches Fenster ist [alter Stand - 1, alter Stand + 1].
        # Blindflug-Anker = die AELTERE Uhr: nur ab der Watchdog-Freigabe
        # zu rechnen vergass einen echten Ausfall davor (Runde 4, #10 —
        # 72h-Ausfall + Quota heilte erst nach 4,2 h statt Minuten).
        ts0 = min(state.get("kwh_floor_ts", time.time()),
                  state.get("kwh_ts", time.time()))
        el_h = min(KWH_ELAPSED_MAX_H, max(0.0, time.time() - ts0) / 3600)
        cap = floor + KWH_HEAL_MAX + max(MAX_KWH_STEP,
                                         KWH_MAX_RATE_KWH_H * el_h)
        lost = state.get("kwh_lost")
        if (lost is not None
                and reading["kwh"] < lost - KWH_HEAL_MAX):
            # DIE MONOTONIE-SCHRANKE GILT FUER BEIDE PFADE: der Notausweg
            # hatte sie (1.7.37), der Schwester-Pfad rebaseline nicht —
            # mit alt=None fielen dort alle Monotonie-Zweige weg, und
            # zwei Gemini-Bestaetigungen desselben optischen Fehlers
            # (letzte Ziffer verloren: 35891 -> 3589) senkten den Stand
            # um -32302 (Runde 4, #1). Unter den letzten echten Stand
            # minus Heilung geht es NIE, mit keinem Zeugen der Welt.
            state["rb_counts"] = {}
            if SAVE_SAMPLES_DIR and time.time() - state.get(
                    "mono_veto_ts", 0) > 60:
                state["mono_veto_ts"] = time.time()
                save_event(SAVE_SAMPLES_DIR, _last_snapshot,
                           "monotonic_veto", stored=lost,
                           rejected=reading["kwh"])
            raise ValueError(f"verworfen: Basis {reading['kwh']} unter "
                             f"letztem Stand {lost} — Monotonie")
        if not (floor <= reading["kwh"] <= cap):
            if (rebaseline(reading, state, source)
                    or local_escape(reading, state, source)):
                source += " (re-baseline)"
                state.pop("kwh_floor", None)   # Boden war falsch — weg,
                state.pop("kwh_floor_ts", None)  # auch wenn Basis darunter
                state.pop("kwh_lost", None)
            else:
                raise ValueError(f"verworfen: Basis {reading['kwh']} "
                                 f"ausserhalb [{floor}, {cap:.0f}]")
        elif (state.get("kwh_lost") is not None
                and reading["kwh"] < state["kwh_lost"]):
            # Unter dem letzten ECHTEN Stand (kwh_lost, vom Watchdog beim
            # Freigeben gemerkt): das ist eine SENKUNG und braucht wie
            # ueberall den bild-fremden Zeugen. Der Watchdog-Umweg
            # (Freigabe -> Basis) war sonst eine -1-Tuer ohne Gemini —
            # I1 gilt auch hier. Nach einer Korruptur-Heilung gibt es
            # keinen echten alten Stand (kwh_lost fehlt) — dort ist der
            # Boden selbst die beste Schaetzung und Lesungen darauf sind
            # keine Senkung.
            if not _source_ok(source):
                # Runde 5, #1: dieser Zweig war der einzige Heilpfad ohne
                # Zeugen-Trennung — Gemini als Kandidat, von Gemini
                # bestaetigt (-1 je Watchdog-Freigabe, -40 kWh in 10 h).
                raise ValueError(f"Basis {reading['kwh']}: Kandidat muss "
                                 f"vom lokalen OCR kommen")
            if not down_budget_ok(state,
                                  state["kwh_lost"] - reading["kwh"]):
                raise ValueError(f"Basis {reading['kwh']}: 24h-Senkungs-"
                                 f"Budget erschoepft")
            if time.time() - _last_gemini_call < GEMINI_COOLDOWN_S:
                raise ValueError(f"Basis {reading['kwh']} unter altem "
                                 f"Stand — warte auf Gemini-Zeugen")
            _last_gemini_call = time.time()
            try:
                gem = gemini_read(get_snapshot())
            except Exception as e:
                # Gemini tot: der Notausweg (>= 6h ohne Erfolg, eng
                # begrenzt) darf auch diese -1 freigeben — sonst haengt
                # eine legitime Basis bei totem Gemini fuer immer.
                if not local_escape(reading, state, source):
                    raise ValueError(f"Basis-Senkung {reading['kwh']}: "
                                     f"Gemini nicht verfuegbar ({e})")
                gem = None
            if gem is not None and not witness_match(gem["kwh"],
                                                     reading["kwh"]):
                raise ValueError(f"Basis-Senkung {reading['kwh']}: Gemini "
                                 f"liest {gem['kwh']} — verworfen")
        elif reading["kwh"] > floor + KWH_HEAL_MAX + KWH_MAX_RATE_KWH_H:
            # GROSSE Aufwaerts-Basis: nach langem Blindflug ist das
            # Fenster bis zu 1800 kWh breit — zwei zeugenlose Frames
            # reichen dafuer nicht (Runde 5, #4). Ueber 1 h Physik
            # (25 kWh) ueberm Boden: Kandidat lokal, dazu Gemini exakt —
            # oder, wenn Gemini tot ist (Quota-Tag nach Ausfall), der
            # Segment-Dekoder mit kalibrierter Marge plus 4 Lesungen
            # ueber >= 5 Minuten statt der ueblichen 2.
            if not _source_ok(source):
                raise ValueError(f"Basis {reading['kwh']}: Kandidat muss "
                                 f"vom lokalen OCR kommen")
            witnessed = False
            if time.time() - _last_gemini_call >= GEMINI_COOLDOWN_S:
                _last_gemini_call = time.time()
                try:
                    gem = gemini_read(get_snapshot())
                    if not witness_match(gem["kwh"], reading["kwh"]):
                        raise ValueError(
                            f"Basis {reading['kwh']}: Gemini liest "
                            f"{gem['kwh']} — verworfen")
                    witnessed = True
                except ValueError:
                    raise
                except Exception:
                    pass            # Gemini tot -> lokaler Ersatz unten
            if not witnessed:
                best, margin = seg_decide(reading["kwh"],
                                          reading["kwh"] + 1)
                if best != reading["kwh"] or margin < 0.8:
                    raise ValueError(f"Basis {reading['kwh']} weit ueber "
                                     f"Boden — kein Zeuge verfuegbar")
                state["base_strict"] = True
    if state.get("kwh") is None and "re-baseline" not in source:
        # Frische Basis braucht uebereinstimmende Lesungen JE KANDIDAT
        # (der alte Direktvergleich blockierte bei strenger Alternation
        # 35891/35892 fuer immer). Mit Boden: 2 Lesungen im gedeckelten
        # Fenster. OHNE Boden (echter Erststart, kein Anker): 4 Lesungen
        # ueber >= 60 s — ein einzelner Geister-Frame setzte sonst den
        # Anker (23.07.: 8443 statt 443).
        pend = state.setdefault("base_pend", {})
        n, first = pend.get(reading["kwh"], (0, time.time()))
        pend[reading["kwh"]] = (n + 1, first)
        if len(pend) > 20:
            state["base_pend"] = pend = {reading["kwh"]: pend[reading["kwh"]]}
        n, first = pend[reading["kwh"]]
        if state.pop("base_strict", False):
            need_n, need_s = 4, 300.0     # breite Basis ohne Gemini
        elif floor is not None:
            need_n, need_s = 2, 0.0
        else:
            need_n, need_s = 4, 60.0      # Kaltstart ohne Anker
        if n < need_n or time.time() - first < need_s:
            raise ValueError(f"Basis {reading['kwh']} braucht "
                             f"Bestaetigung ({n}/{need_n})")
        state.pop("base_pend", None)
    if (state.get("kwh") is not None
            and reading["kwh"] < state["kwh"] - KWH_HEAL_MAX):
        # Darf hier nie ankommen — Notbremse, falls je wieder ein
        # Heilpfad an der Plausibilitaet vorbeifuehrt
        raise ValueError(f"verworfen: Monotonie-Notbremse "
                         f"({state['kwh']} -> {reading['kwh']})")
    ref = prev_kwh if prev_kwh is not None else prev_lost
    if ref is not None and reading["kwh"] < ref:
        down_note(state, ref - reading["kwh"])
    kwh_hist_push(state, reading["kwh"])
    if reading["kwh"] >= (state.get("kwh_floor") or 0):
        state.pop("kwh_floor", None)
        state.pop("kwh_floor_ts", None)
        state.pop("kwh_lost", None)
    # Abgestandene Konsens-Reste aufraeumen — sonst hielten sie das
    # "pending"-Flag im Persist-Takt fuer immer auf 30 s.
    for key in ("rb_counts", "base_pend", "esc_counts"):
        c = state.get(key)
        if c:
            fresh = {k: v for k, v in c.items()
                     if time.time() - v[-1] < 3600}
            if fresh:
                state[key] = fresh
            else:
                state.pop(key, None)
    rbc = state.get("rb_confirm")
    if rbc and time.time() - rbc[2] > 6 * 3600:
        state.pop("rb_confirm", None)
    return reading, source


KWH_VETO_MARKERS = ("kWh", "Basis", "Monotonie", "Physik-Fenster")


def w_salvage(err: str, reading, state: dict) -> bool:
    """Ist bei einem kWh-Veto der W-Kanal dieses Frames trotzdem gesund?
    Dann regelt der Hauptloop mit dem W-Wert weiter, statt in den
    Failsafe zu fallen — ein blockierter ZAEHLERSTAND ist kein blinder
    Zaehler. (2. Angriffsrunde: Wallbox-Lastspitze -> kWh-Tor zu ->
    5,9 h Failsafe waehrend der hoechsten Hauslast — die teuerste Zeit,
    nicht zu regeln.) Nur fuer kWh-TYPISCHE Gruende: bei Frame-Muell
    (Segmenttest, dunkles LCD) oder W-Fehlern gibt es nichts zu retten."""
    if not isinstance(reading, dict):
        return False
    if not any(m in err for m in KWH_VETO_MARKERS):
        return False
    try:
        return _plausible_w(reading["w"], state) is None
    except Exception:
        return False


def save_state(state: dict) -> None:
    """ATOMAR (tmp + fsync + os.replace): ein SIGTERM mitten im Write
    hinterliess sonst leeres JSON — und mit ihm verschwanden Boden,
    kwh_lost und das 6h-Fenster stillschweigend (2. Angriffsrunde).
    Persistiert auch die KONSENS-ZAEHLER: Neustarts haeufiger als
    REBASE_MIN_SPAN_S (Supervisor-Watchdog-Schleife alle < 3 min)
    setzten sonst die 180s-Uhr ewig zurueck — jede Ausfall-Heilung war
    strukturell unmoeglich, Dauer-Failsafe."""
    data = {
        "kwh": state.get("kwh"), "ts": state.get("kwh_ts"),
        "floor": state.get("kwh_floor"),
        "floor_ts": state.get("kwh_floor_ts"),
        "lost": state.get("kwh_lost"),
        "hist": state.get("kwh_hist", []),
        "rb": {str(k): list(v) for k, v in state.get("rb_counts", {}).items()},
        "bp": {str(k): list(v) for k, v in state.get("base_pend", {}).items()},
        "esc": {str(k): list(v) for k, v in state.get("esc_counts", {}).items()},
        "rbc": state.get("rb_confirm"),
        # Auch die UHREN muessen Neustarts ueberleben (Runde 4, #2/#5/#12):
        # die 6h-Notausweg-Uhr und der Watchdog-Fortschritt starteten
        # sonst je Prozess bei Null — ein Neustart-Sturm hielt jeden
        # Deadlock fuer immer offen.
        "gerr": _gemini_err_since, "gok": _gemini_ok_ts,
        "gerrn": _gemini_err_n, "dh": state.get("down_hist", []),
        "cycle": state.get("cycle"), "segw": state.get("seg_warn"),
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        fh.write(json.dumps(data))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STATE_FILE)


def load_state() -> dict:
    """State laden und strukturell heilen.

    Persistiert wird ALLES, was die Monotonie schuetzt: Stand, Zeitstempel,
    Monotonie-Boden und das 6h-Physik-Fenster. Bis 1.7.35 ueberlebte der
    Boden keinen Neustart ({"kwh": null} -> leerer State) — danach war der
    Stand mit zwei Lesungen frei waehlbar. Zeitstempel aus der Zukunft
    werden verworfen (RTC-Sprung/NTP), sonst degradiert der Physik-Deckel.

    Ein Stand > KWH_ABS_MAX (28.07.: 358914) ist Korruption — er wird
    verworfen und die plausibelste Ableitung (Ziffern von rechts
    abschneiden) als BODEN gesetzt: die Kamera setzt den exakten Stand
    neu, aber nie darunter."""
    state: dict = {}
    if STATE_FILE.exists():
        try:
            raw = json.loads(STATE_FILE.read_text())
        except (ValueError, OSError):
            raw = {}
        now = time.time()

        # Zeitstempel werden NICHT geklemmt (nur Typ-geprueft): eine
        # Klemme "Zukunft -> jetzt" riss bei nachgehender Boot-Uhr nach
        # dem NTP-Sprung den Physik-Deckel auf. Negative/kuenftige
        # Abstaende werden an der VERWENDUNGSSTELLE auf 0 geklemmt —
        # das ist immer die strengere, sichere Richtung.
        def _num(v):
            return v if isinstance(v, (int, float)) else now

        if isinstance(raw.get("kwh"), int):
            state = {"kwh": raw["kwh"], "kwh_ts": _num(raw.get("ts"))}
        if isinstance(raw.get("floor"), int) and raw["floor"] <= KWH_ABS_MAX:
            state["kwh_floor"] = raw["floor"]
            state["kwh_floor_ts"] = _num(raw.get("floor_ts"))
            if isinstance(raw.get("lost"), int):
                state["kwh_lost"] = raw["lost"]
        try:
            hist = [[t, k] for t, k in raw.get("hist", [])
                    if isinstance(t, (int, float)) and isinstance(k, int)
                    and 0 < k <= KWH_ABS_MAX][-100:]
            if hist:
                state["kwh_hist"] = hist
        except (TypeError, ValueError):
            pass

        def _counts(key, arity):
            out = {}
            for ks, v in (raw.get(key) or {}).items():
                try:
                    if (isinstance(v, list) and len(v) in (2, 3)
                            and isinstance(v[0], int)):
                        t = tuple(v)
                        if arity == 3 and len(t) == 2:
                            t = (t[0], t[1], t[1])   # Altformat auffuellen
                        elif arity == 2 and len(t) == 3:
                            t = (t[0], t[1])
                        out[int(ks)] = t
                except (ValueError, TypeError):
                    pass
            return out

        for skey, dkey, arity in (("rb", "rb_counts", 3),
                                  ("bp", "base_pend", 2),
                                  ("esc", "esc_counts", 3)):
            c = _counts(skey, arity)
            if c:
                state[dkey] = c
        rbc = raw.get("rbc")
        if isinstance(rbc, list) and len(rbc) == 3:
            state["rb_confirm"] = (rbc[0], rbc[1], rbc[2])
        global _gemini_err_since, _gemini_ok_ts, _gemini_err_n
        if isinstance(raw.get("gerr"), (int, float)):
            _gemini_err_since = raw["gerr"]
        if isinstance(raw.get("gok"), (int, float)):
            _gemini_ok_ts = raw["gok"]
        if isinstance(raw.get("gerrn"), int):
            _gemini_err_n = raw["gerrn"]
        try:
            dh = [[t, a] for t, a in raw.get("dh", [])
                  if isinstance(t, (int, float)) and isinstance(a, int)
                  and 0 < a <= KWH_HEAL_MAX][-50:]
            if dh:
                state["down_hist"] = dh
        except (TypeError, ValueError):
            pass
        if isinstance(raw.get("cycle"), int):
            state["cycle"] = raw["cycle"]
        if isinstance(raw.get("segw"), int):
            state["seg_warn"] = raw["segw"]
    k = state.get("kwh")
    if k is not None and k > KWH_ABS_MAX:
        heal = k
        while heal > KWH_ABS_MAX:
            heal //= 10
        print(f"KRITISCH: state.json enthaelt strukturell unmoeglichen "
              f"Stand {k} (> {KWH_ABS_MAX}) — verworfen. Boden {heal}, "
              f"die Kamera setzt den Stand neu.", file=sys.stderr)
        if SAVE_SAMPLES_DIR:
            save_event(SAVE_SAMPLES_DIR, None, "state_corrupt_healed",
                       stored=k, floor=heal)
        # kwh_hist und kwh_ts UEBERLEBEN die Heilung: sonst startete der
        # geheilte Zustand ohne Physik-Fenster und ohne Blindflug-Anker
        # (Runde 5, #8) — genau dann, wenn beide am dringendsten
        # gebraucht werden.
        keep = {k2: v for k2, v in state.items()
                if k2 in ("kwh_hist", "kwh_ts")}
        state = {"kwh": None, "kwh_floor": heal,
                 "kwh_floor_ts": time.time(), **keep}
    return state


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


# Ruhespannungskennlinie LiFePO4 (V je Zelle -> Ladestand %), digitalisiert
# aus veroeffentlichten LFP/C-Messreihen. Entlade-Richtung; die Ladekurve
# liegt ~25 mV hoeher, was im flachen Bereich ~20 Prozentpunkten entspricht —
# deshalb ist der Wert eine SCHAETZUNG und heisst auch so.
# Interaktive Fassung: docs/lifepo4-soc.html
_OCV = [(2.600, 0), (3.040, 5), (3.168, 10), (3.233, 20), (3.262, 30),
        (3.281, 40), (3.294, 50), (3.303, 60), (3.309, 65), (3.320, 70),
        (3.339, 76), (3.342, 80), (3.346, 85), (3.350, 90), (3.357, 95),
        (3.375, 98), (3.460, 100)]
# Innenwiderstand des gesamten Packs inkl. Verkabelung (Ohm) fuer die
# Lastkorrektur: unter Entnahme liegt die Klemmenspannung unter der Ruhelage
BATT_RI = float(os.environ.get("BATT_RI_MOHM", "10")) / 1000
BATT_CAPACITY_KWH = float(os.environ.get("BATT_CAPACITY_KWH", "0"))


def soc_estimate(v_pack: float, pv_w: float) -> float | None:
    """Geschaetzter Ladestand in % aus der Packspannung.

    Der Entladestrom wird aus der Inverter-Leistung abgeleitet (alle
    Straenge haengen am Akku-Bus) und ueber BATT_RI auf Ruhespannung
    zurueckgerechnet. Im flachen Kurvenbereich ist das prinzipbedingt
    grob — dafuer braucht es den Coulomb-Zaehler des BMS."""
    if not v_pack or v_pack < 20:
        return None
    cells = len(BATT_STRINGS) and 16 or 16      # 16S
    amps = (pv_w / 0.96) / v_pack if pv_w and v_pack > 1 else 0.0
    cell = (v_pack + amps * BATT_RI) / cells
    if cell <= _OCV[0][0]:
        return 0.0
    if cell >= _OCV[-1][0]:
        return 100.0
    for i in range(1, len(_OCV)):
        if cell <= _OCV[i][0]:
            (v0, s0), (v1, s1) = _OCV[i - 1], _OCV[i]
            return round(s0 + (cell - v0) / (v1 - v0) * (s1 - s0), 1)
    return 100.0


def battery_guard(state: dict, pv_w: float,
                  dc: dict[int, tuple[float, float]], now: float) -> int:
    """Tiefentladeschutz, simpel und ehrlich.

    Am HMS haengt ausschliesslich der Akku-Bus — jede Ausgangsleistung ist
    also Akku-Entnahme. Es gibt daher nichts fein zu dosieren: faellt die
    Bus-Spannung unter BATT_LOW_V, wird der Inverter abgeschaltet; erholt
    sie sich, wird wieder freigegeben. (Frueher versuchte der Waechter, per
    "Sonnen-Probe" das Limit tastend anzuheben — das ergab nur Sinn, solange
    Solar direkt am Inverter haengen sollte, und fuehrte hier zu einem
    Dauer-Cap von 50 W.)

    Entprellt in beide Richtungen: unter Last sackt der Bus kurz ein (20 A
    ueber Kabel, BMS und Innenwiderstand), das darf nicht sofort ausloesen.
    Leere Eingaenge (Spannung ~0) werden ignoriert, sonst wuerde ein
    unbelegter String den Waechter dauerhaft in den Schutz zwingen."""
    volts = [dc[s][0] for s in BATT_STRINGS if s in dc and dc[s][0] > 5.0]
    hold = state.get("batt_hold", False)
    if not volts:                      # keine Messwerte -> Zustand halten
        return MIN_LIMIT_W if hold else MAX_LIMIT_W
    v = min(volts)
    state["batt_v"] = v

    if not hold:
        if v < BATT_LOW_V:
            if state.get("batt_low_since") is None:
                state["batt_low_since"] = now
            elif now - state["batt_low_since"] >= BATT_TRIP_S:
                state["batt_hold"] = True
                state.pop("batt_low_since", None)
                print(f"Akku-Waechter: {v:.1f}V unter {BATT_LOW_V}V "
                      f"(>{BATT_TRIP_S:.0f}s) — Inverter abgeschaltet")
                return MIN_LIMIT_W
        else:
            state["batt_low_since"] = None
        return MAX_LIMIT_W

    rel = BATT_LOW_V + BATT_RECOVER_V
    if v >= rel:
        if state.get("batt_ok_since") is None:
            state["batt_ok_since"] = now
        elif now - state["batt_ok_since"] >= BATT_RELEASE_S:
            state["batt_hold"] = False
            state.pop("batt_ok_since", None)
            print(f"Akku-Waechter: {v:.1f}V >= {rel:.1f}V "
                  f"({BATT_RELEASE_S:.0f}s gehalten) — wieder freigegeben")
            return MAX_LIMIT_W
    else:
        state.pop("batt_ok_since", None)
    return MIN_LIMIT_W

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


_deye_cache: "tuple[float, dict | None]" = (0.0, None)


def deye_value() -> dict | None:
    """Letzter bekannter Stand der zweiten Quelle — NICHT blockierend.
    Geholt wird im Hintergrund-Thread, damit ein WLAN-Hänger am Logger
    (Signalqualitaet schwankt, deshalb haengt dort ein Repeater) niemals
    den 0,5s-Regelzyklus aufhaelt."""
    ts, val = _deye_cache
    if val is not None and time.time() - ts > 10 * DEYE_POLL_S:
        return None          # Logger stumm -> lieber nichts als Altwert
    return val


def _mb_crc(d: bytes) -> int:
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


_deye_seq = 0


def deye_modbus(addr: int, count: int) -> list | None:
    """Holding-Register ueber Solarman V5 (Port 8899) lesen.

    Funktioniert in BEIDEN Logger-Modi: bei yz_tmode=cmd liefert der
    Logger seinen ~5min alten Cache, bei yz_tmode=throughput geht die
    Anfrage direkt an den Wechselrichter (dann echtzeitfaehig, und die
    HTML-Statusseite fuehrt keine Daten mehr — deshalb ist Modbus der
    primaere Pfad)."""
    global _deye_seq
    _deye_seq = (_deye_seq + 1) & 0xFFFF
    mb = bytes([1, 3]) + struct.pack('>HH', addr, count)
    mb += struct.pack('<H', _mb_crc(mb))
    pl = bytes([0x02]) + bytes(14) + mb
    f = bytearray([0xA5]) + struct.pack('<H', len(pl)) + bytes([0x10, 0x45])
    f += struct.pack('<H', _deye_seq) + struct.pack('<I', DEYE_LOGGER_SN) + pl
    f += bytes([sum(f[1:]) & 0xFF, 0x15])
    s = socket.socket()
    s.settimeout(6)
    try:
        s.connect((DEYE_HOST.split(":")[0], 8899))
        try:
            s.recv(256)          # Begruessungsbanner des Loggers
        except Exception:
            pass
        s.sendall(bytes(f))
        r = s.recv(1024)
    finally:
        s.close()
    i = r.find(b'\x01\x03')
    if i < 0 or len(r) < i + 3:
        return None
    n = r[i + 2]
    d = r[i + 3:i + 3 + n]
    if len(d) < n:
        return None
    return [struct.unpack('>H', d[j:j + 2])[0] for j in range(0, n - 1, 2)]


def _deye_poll_once() -> dict | None:
    # Registerkarte SUN600G3 (am 28.07. am Geraet ermittelt):
    #   0x003C Ertrag heute (x0,1 kWh) | 0x003E/0x003F Ertrag gesamt (32bit)
    #   0x0056 AC-Leistung (x0,1 W)
    if DEYE_LOGGER_SN:
        try:
            v = deye_modbus(0x003C, 27)     # 0x003C .. 0x0056 in einem Rutsch
            if v and len(v) >= 27:
                total = ((v[2] << 16) | v[3]) / 10.0
                return {"w": v[26] / 10.0, "today": v[0] / 10.0,
                        "total": total if total > 0 else None}
        except Exception:
            pass                            # -> HTML-Fallback
    r = requests.get(f"http://{DEYE_HOST}/status.html",
                     auth=(DEYE_USER, DEYE_PASS), timeout=8)
    r.raise_for_status()

    def num(key):
        m = re.search(rf'var {key}\s*=\s*"([^"]*)"', r.text)
        if not m or not m.group(1).strip():
            return None
        try:
            return float(m.group(1).strip())
        except ValueError:
            return None

    w = num("webdata_now_p")
    if w is None:
        return None          # Logger online, aber ohne Inverterdaten (Nacht)
    return {"w": w, "today": num("webdata_today_e"),
            "total": num("webdata_total_e")}


def deye_start():
    """Hintergrund-Poller starten (nur wenn DEYE_HOST gesetzt ist)."""
    if not DEYE_HOST:
        return

    def worker():
        global _deye_cache
        fails = 0
        while True:
            try:
                _deye_cache = (time.time(), _deye_poll_once())
                fails = 0
            except Exception as e:
                fails += 1
                if fails in (1, 10, 100):   # nicht das Log fluten
                    print(f"Deye nicht lesbar ({fails}x): {e}",
                          file=sys.stderr)
            time.sleep(DEYE_POLL_S)

    threading.Thread(target=worker, daemon=True).start()
    print(f"Deye-Auslesung aktiv: {DEYE_HOST} alle {DEYE_POLL_S:.0f}s")


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
        soc = soc_estimate(state["batt_v"], state.get("batt_pv", 0.0))
        if soc is not None:
            msgs.append((f"{TOPIC}/batt_soc", f"{soc:.0f}"))
            if BATT_CAPACITY_KWH > 0:
                msgs.append((f"{TOPIC}/batt_kwh",
                             f"{soc / 100 * BATT_CAPACITY_KWH:.1f}"))
    dey = deye_value()
    if dey is not None:
        msgs.append((f"{TOPIC}/deye_w", f"{dey['w']:.0f}"))
        if dey.get("today") is not None:
            msgs.append((f"{TOPIC}/deye_today", f"{dey['today']:.2f}"))
        if dey.get("total") is not None:
            msgs.append((f"{TOPIC}/deye_total", f"{dey['total']:.1f}"))
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
        sensors["batt_soc"] = {"name": "Akku-Ladestand (geschaetzt)",
                               "unit_of_measurement": "%",
                               "device_class": "battery",
                               "state_class": "measurement",
                               "icon": "mdi:battery-50"}
        if BATT_CAPACITY_KWH > 0:
            sensors["batt_kwh"] = {"name": "Akku-Energie (geschaetzt)",
                                   "unit_of_measurement": "kWh",
                                   "device_class": "energy_storage",
                                   "state_class": "measurement",
                                   "icon": "mdi:battery-charging-medium"}
    if DEYE_HOST:
        sensors["deye_w"] = {"name": "Deye Leistung",
                             "unit_of_measurement": "W",
                             "device_class": "power",
                             "state_class": "measurement",
                             "icon": "mdi:solar-power-variant"}
        sensors["deye_today"] = {"name": "Deye Ertrag heute",
                                 "unit_of_measurement": "kWh",
                                 "device_class": "energy",
                                 "state_class": "total_increasing",
                                 "icon": "mdi:solar-power"}
        sensors["deye_total"] = {"name": "Deye Ertrag gesamt",
                                 "unit_of_measurement": "kWh",
                                 "device_class": "energy",
                                 "state_class": "total_increasing",
                                 "icon": "mdi:counter"}
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


def ctl_tick(grid_w: int, pv_w: float, limit, batt_v=None):
    rec = {"t": round(time.time(), 2), "ev": "tick", "grid": grid_w,
           "pv": round(pv_w, 1), "limit": limit}
    if batt_v is not None:
        rec["bv"] = round(batt_v, 2)
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
    state["batt_pv"] = pv_w
    if BATT_STRINGS and dc:
        v = [x[0] for x in dc.values() if x[0] > 5.0]
        if v:
            state["batt_v"] = min(v)
    elif dc:   # ohne Waechter trotzdem mitschreiben, fuer die Auswertung
        v = [x[0] for x in dc.values() if x[0] > 5.0]
        if v:
            state["batt_v"] = min(v)
    horizon = PENDING_THETA_S + 4 * PENDING_TAU_S
    pend = [(ts, d) for ts, d in state.get("pending", [])
            if now - ts < horizon]
    state["pending"] = pend
    pending = sum(d * pending_weight(now - ts) for ts, d in pend)
    target = target_grid(state)
    error_raw = grid_w - target  # >0: zu viel Bezug
    # wanted bleibt absolut aus Roh-Messwerten (staleness-invariant);
    # ENTSCHIEDEN wird auf dem kompensierten Fehler
    error = error_raw - int(round(pending))
    wanted = int(round(max(MIN_LIMIT_W, min(max_limit, pv_w + error_raw))))
    current = state.get("limit_w")
    ctl_tick(grid_w, pv_w, current, state.get("batt_v"))

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

    def send_if_needed(value: int):
        """Limit nur senden, wenn es sich nennenswert aendert."""
        if current is not None and abs(current - value) < MIN_STEP_W:
            return current, pv_w
        return (send(value, "floor-schlaf") or current), pv_w

    # Unterhalb des ansteuerbaren Floors nicht weiter runterjagen (s.o.).
    # Der Akku-Waechter hat Vorrang: haelt er, darf der Floor nicht dagegen
    # arbeiten — leerer Akku schlaegt Ueberschuss-Einspeisung.
    if (SUSTAIN_FLOOR_W and wanted < SUSTAIN_FLOOR_W
            and not state.get("batt_hold")
            and SUSTAIN_FLOOR_W <= max_limit):
        # Unterhalb des Floors gibt es nur zwei ehrliche Optionen: das Limit
        # HALTEN (dann geht Ueberschuss ins Netz) oder den Inverter GANZ
        # abschalten (dann deckt das Netz die Restlast). Halten kostet
        # (Floor - wanted), Abschalten kostet (wanted) — der Kipppunkt liegt
        # damit bei Floor/2. Relevant, sobald eine zweite Quelle (Deye) oder
        # viel Sonne die Last schon deckt: dann waere Halten reine
        # Verschwendung von Akku-Energie ans Netz.
        # Ausnahme: ist der Akku voll, waere der Ueberschuss ohnehin
        # abgeregelt — dann ist Einspeisen gratis und Halten immer richtig.
        v = state.get("batt_v")
        batt_full = v is not None and v >= BATT_HIGH_V
        # Die Schlafen/Halten-Entscheidung auf einem GEGLAETTETEN Wunschwert
        # faellen. Die Hauslast zappelt (26.07. 03:48-03:49: 180 W <-> 266 W
        # im Sekundentakt) und lief dabei staendig ueber beide Schwellen —
        # daraus wurden drei Limit-Wechsel in 25 Sekunden. Median ueber
        # FLOOR_SMOOTH_S ist unempfindlich gegen einzelne Ausreisser,
        # reagiert aber auf echte Lastwechsel.
        wh = state.setdefault("want_hist", [])
        wh.append((now, wanted))
        del wh[:max(0, len(wh) - 40)]
        fenster = sorted(w for t, w in wh if now - t <= FLOOR_SMOOTH_S)
        wanted_s = fenster[len(fenster) // 2] if fenster else wanted
        # Hysterese um den KIPPPUNKT (Floor/2), nicht um den Floor: bei
        # Nachtlast ~390W liegt das Wunsch-Limit unter dem Floor, aber weit
        # ueber dem Kipppunkt — dort ist Halten klar guenstiger als Schlafen.
        if state.get("floor_sleep"):
            if wanted_s >= int(SUSTAIN_FLOOR_W * 0.6):
                state["floor_sleep"] = False
                print(f"Sustain-Floor: Ziel {wanted_s}W wieder ueber "
                      f"{int(SUSTAIN_FLOOR_W * 0.6)}W — Inverter aufwecken")
            else:
                return send_if_needed(MIN_LIMIT_W)
        if not batt_full and wanted_s < SUSTAIN_FLOOR_W // 2:
            print(f"Sustain-Floor: Ziel {wanted_s}W < {SUSTAIN_FLOOR_W // 2}W — "
                  f"Fremdquelle deckt die Last, Inverter schlafen legen "
                  f"(Halten wuerde {SUSTAIN_FLOOR_W - wanted}W verschenken)")
            state["floor_sleep"] = True
            return send_if_needed(MIN_LIMIT_W)
        if state.get("floor_since") is None:
            print(f"Sustain-Floor: Ziel {wanted}W unter {SUSTAIN_FLOOR_W}W — "
                  f"halte Limit, statt den MPPT auszuhebeln")
            state["floor_since"] = now
        wanted = SUSTAIN_FLOOR_W
    else:
        state["floor_since"] = None
        state["floor_sleep"] = False
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
        # Klemmen erkennt man an FLACHHEIT, nicht an fehlendem Fortschritt.
        # Im Attraktor steht die Leistung wie festgenagelt (26.07. 01:21-01:29:
        # pv = 178,3 W ueber 7,5 Minuten, Schwankung 0,2 W) — waehrend echtes
        # Nachfuehren staendig zappelt. Frueher wurde die Bewegung seit
        # Fensterbeginn gemessen; das Einschwingen VOR dem Klemmen zaehlte als
        # Fortschritt und der Kick blieb aus.
        hist = state.setdefault("pv_hist", [])
        hist.append((now, pv_w))
        del hist[:max(0, len(hist) - 60)]
        fenster = [v for t, v in hist if now - t <= STUCK_S]
        flach = len(fenster) >= 5 and (max(fenster) - min(fenster)) < STUCK_FLAT_W
        if (flach and now - hist[0][0] >= STUCK_S
                and now - state.get("kick_ts", 0) > KICK_COOLDOWN_S):
            state["pv_hist"] = []
            print(f"MPPT-Kick: pv {pv_w:.0f}W steht seit {STUCK_S:.0f}s "
                  f"wie festgenagelt unter Limit {current}W "
                  f"— Eskalation startet (+{KICK_STEPS_W[0]}W)")
            state["kick"] = {"base": current, "pv0": pv_w, "step": 1,
                             "ts": now}
            target = int(min(max_limit, current + KICK_STEPS_W[0]))
            return send(target, "kick1") or current, pv_w
    else:
        state["pv_hist"] = []
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
        try:
            save_state(state)   # Uhren/Zaehler sichern (atomar)
        except Exception:
            pass
        if _cam is not None:
            _cam.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _bye)
    atexit.register(lambda: _cam is not None and _cam.shutdown())

    state = load_state()
    print(f"kWh-Tore: Schritt +{MAX_KWH_STEP} (2x bestaetigt), Heilung "
          f"-{KWH_HEAL_MAX} (nur mit Gemini), Struktur-Deckel {KWH_ABS_MAX}, "
          f"Aufwaerts-Physik {KWH_MAX_RATE_KWH_H} kWh/h, Re-Baseline "
          f"{REBASE_MIN_COUNT}x ueber {REBASE_MIN_SPAN_S:.0f}s, "
          f"Stand: {state.get('kwh')}")
    if INTERVAL_S > 60:
        print(f"WARNUNG: INTERVAL_S={INTERVAL_S:.0f}s ist zu langsam — "
              f"bei Dauerlast > 20 kWh/h tickt der Zaehler mehrfach "
              f"zwischen zwei Frames und der Stand haengt hinterher. "
              f"Lesetakt unter 60 s halten.", file=sys.stderr)
    if BATT_STRINGS:
        print(f"Akku-Waechter aktiv: Straenge {BATT_STRINGS}, "
              f"abschalten unter {BATT_LOW_V:.1f}V ({BATT_LOW_V/16:.2f}V/Zelle) "
              f"fuer {BATT_TRIP_S:.0f}s, Freigabe ab "
              f"{BATT_LOW_V + BATT_RECOVER_V:.1f}V")
    else:
        print("Akku-Waechter AUS (batt_strings leer)")
    print(f"Netz-Ziel {TARGET_GRID_W:+d}W (leer) .. {TARGET_GRID_FULL_W:+d}W (voll), "
          f"Floor {SUSTAIN_FLOOR_W}W, max {MAX_LIMIT_W}W")
    deye_start()
    publish_discovery()
    w_hist: list[int] = []  # Median-3: einzelner Ausreisser-Frame regelt nicht
    last_written_kwh = (state.get("kwh"), state.get("kwh_floor"))
    # 0 = die erste Schleife schreibt SOFORT: die 900s-Uhr startete sonst
    # je Prozess neu, und bei Zaehler-Plateau + Neustart-Sturm wurde
    # state.json nie geschrieben — der Platten-ts alterte unbegrenzt und
    # oeffnete den Physik-Deckel (Runde 4, #0: 4h Sturm -> +100 kWh).
    last_write_ts = 0.0
    while True:
        limit = None
        reading = None
        try:
            state["cycle"] = state.get("cycle", 0) + 1
            reading, source = read_meter(state["cycle"])
            # Watchdog gegen den STILLEN Fehler: liest das kNN dauerhaft
            # falsch, passt alles zusammen und nichts loest aus. Alle
            # SEG_WATCH_EVERY Zyklen prueft der unabhaengige Segment-Dekoder
            # den akzeptierten Stand gegen. Widerspricht er mehrfach, wird
            # der Stand verworfen statt still weiterzulaufen.
            if (state.get("kwh") is not None and _local_reader is not None
                    and state["cycle"] % SEG_WATCH_EVERY == 0):
                # _local_reader-Check: ohne lokalen Leser liefert seg_decide
                # bedingungslos (None, 0.0) — der Watchdog haette dann im
                # Gemini-Modus alle 600 Zyklen grundlos den Stand freigegeben
                best, _ = seg_decide(state["kwh"], state["kwh"] + 1)
                if best is None:      # Bild stuetzt den Stand ueberhaupt nicht
                    state["seg_warn"] = state.get("seg_warn", 0) + 1
                    print(f"Segment-Watchdog: Bild stuetzt Stand "
                          f"{state['kwh']} nicht ({state['seg_warn']}x)",
                          file=sys.stderr)
                    if state["seg_warn"] >= 3:
                        print(f"Segment-Watchdog: Stand {state['kwh']} "
                              f"freigegeben — naechste Lesung setzt neu",
                              file=sys.stderr)
                        state["kwh_floor"] = state["kwh"] - KWH_HEAL_MAX
                        state["kwh_floor_ts"] = time.time()
                        state["kwh_lost"] = state["kwh"]
                        state["kwh"] = None
                        state["seg_warn"] = 0
                        state["rb_counts"] = {}
                else:
                    state["seg_warn"] = 0
            reading, source = guard_kwh(reading, source, state)
            state.update(reading)
            state["kwh_ts"] = time.time()
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
            if w_salvage(str(e), reading, state):
                # Kanaltrennung auch im Fehlerfall: das kWh-Tor ist zu,
                # aber der sauber gelesene W-Wert regelt weiter — kein
                # Failsafe, solange der Zaehler lesbar bleibt
                state["w"] = reading["w"]
                w_hist.append(reading["w"])
                del w_hist[:-3]
                if state["cycle"] % CONTROL_EVERY == 0:
                    try:
                        limit, _ = control(
                            sorted(w_hist)[len(w_hist) // 2], state)
                        if limit is not None:
                            state["limit_w"] = limit
                    except Exception as e2:
                        print(f"W-Weiterbetrieb: {e2}", file=sys.stderr)
                publish(None, "retry", state.get("limit_w"))
            elif state["failures"] >= FAILSAFE_AFTER:
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
        # Persistiert wird alles Monotonie-Relevante — bei Aenderung von
        # Stand ODER Boden; sonst alle 15 min (frischer Zeitstempel fuer
        # den Physik-Deckel) bzw. alle 30 s, solange Konsens-Zaehler
        # laufen (die 180s-Re-Baseline-Uhr muss Neustarts ueberleben).
        written = (state.get("kwh"), state.get("kwh_floor"))
        pending = bool(state.get("rb_counts") or state.get("base_pend")
                       or state.get("esc_counts") or state.get("rb_confirm"))
        if (written != last_written_kwh
                or time.time() - last_write_ts > (30 if pending else 900)):
            save_state(state)
            last_written_kwh = written
            last_write_ts = time.time()
        maybe_retrain(state)
        if state["cycle"] % 100 == 0:  # ~alle 1-2min nach neuem Modell schauen
            maybe_reload_model()
        if once:
            break
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main(once="--once" in sys.argv)
