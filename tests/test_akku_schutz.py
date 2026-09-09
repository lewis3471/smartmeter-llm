#!/usr/bin/env python3
"""Tests fuer den Tiefentlade-Schutz.  Lauf:
    .venv/bin/python tests/test_akku_schutz.py

Hintergrund: Am 28.08. lief der Akku bis zur BMS-Abschaltung leer, obwohl
der Waechter auf 47/48 V stand. Ursache waren drei Wege, auf denen ein
Limit am Waechter VORBEI an den Inverter ging bzw. der Schutz stillschweigend
verschwand. Jeder davon ist hier als Replay festgenagelt:

  A1  Kein Limit ohne Waechter: der Failsafe-Pfad (8 verworfene Lesungen)
      darf NIE mehr setzen als der Akku hergibt.
  A2  Der Schutz ueberlebt Neustarts (batt_hold in state.json).
  A3  Eingefrorene OpenDTU-Werte gelten als NICHT frisch — eine Spannung,
      die die DTU nur noch aus dem Cache wiederholt, darf nicht als
      Messwert durchgehen.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("OPENDTU_URL", "http://test.invalid")
os.environ.setdefault("OPENDTU_USER", "t")
os.environ.setdefault("OPENDTU_PASS", "t")
os.environ.setdefault("MQTT_USER", "CHANGE_ME")
os.environ.setdefault("MQTT_PASS", "x")
os.environ.setdefault("READER_MODE", "gemini")
os.environ.setdefault("SAVE_SAMPLES_DIR", "")
os.environ["BATT_STRINGS"] = "1,4"
os.environ["BATT_LOW_V"] = "47"
os.environ["BATT_HIGH_V"] = "54.4"
_tmp = Path(tempfile.mkdtemp())
os.environ["STATE_FILE"] = str(_tmp / "state.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import meter_reader as mr


class FakeTime:
    def __init__(self):
        import time as _t
        self._real = _t
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def __getattr__(self, a):
        return getattr(self._real, a)


FT = FakeTime()
mr.time = FT

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'}   {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


# --- A1: Failsafe respektiert den Waechter -------------------------------
def test_failsafe_geht_nicht_am_waechter_vorbei():
    """Leerer Akku + Dauerfehler der Kamera: der Failsafe darf den Inverter
    NICHT auf Vollgas stellen. Genau das war der Pfad in die Tiefentladung."""
    state = {}
    leer = (46.0, 300.0)          # Bus unter BATT_LOW_V
    mr._livedata_meta = {"ts": FT.now, "age_s": 0.0,
                         "reachable": True, "producing": True}
    mr.get_livedata = lambda: (300.0, {1: leer, 4: leer})

    # Waechter ausloesen lassen (Entprellung BATT_TRIP_S)
    mr.battery_guard(state, 300.0, {1: leer, 4: leer}, FT.now)
    FT.now += mr.BATT_TRIP_S + 1
    cap = mr.battery_guard(state, 300.0, {1: leer, 4: leer}, FT.now)
    check("waechter_loest_bei_unterspannung_aus",
          state.get("batt_hold") is True and cap == mr.MIN_LIMIT_W,
          f"hold={state.get('batt_hold')} cap={cap}")

    fs = mr.guarded_limit(state, 1999)
    check("failsafe_wird_auf_minimum_gedeckelt", fs == mr.MIN_LIMIT_W,
          f"guarded_limit(1999) = {fs}, erwartet {mr.MIN_LIMIT_W}")


def test_failsafe_ohne_akku_unveraendert():
    """Ohne konfigurierten Akku bleibt der Failsafe, was er war."""
    alt = mr.BATT_STRINGS
    mr.BATT_STRINGS = []
    try:
        check("ohne_akku_kein_deckel", mr.guarded_limit({}, 1999) == 1999)
    finally:
        mr.BATT_STRINGS = alt


def test_failsafe_ohne_dtu_bleibt_vorsichtig():
    """DTU nicht erreichbar UND Schutz war aktiv -> Minimum, nicht Vollgas."""
    def boom():
        raise OSError("DTU weg")
    alt = mr.get_livedata
    mr.get_livedata = boom
    try:
        fs = mr.guarded_limit({"batt_hold": True}, 1999)
        check("dtu_weg_mit_hold_bleibt_minimum", fs == mr.MIN_LIMIT_W,
              f"= {fs}")
    finally:
        mr.get_livedata = alt


# --- A2: Der Schutz ueberlebt den Neustart -------------------------------
def test_schutz_ueberlebt_neustart():
    state = {"kwh": 35891, "kwh_ts": FT.now, "kwh_floor": 35891,
             "kwh_floor_ts": FT.now, "batt_hold": True,
             "batt_ok_since": FT.now}
    mr.save_state(state)
    neu = mr.load_state()
    check("batt_hold_ueberlebt", neu.get("batt_hold") is True)
    check("freigabe_uhr_ueberlebt_frisch",
          abs(neu.get("batt_ok_since", 0) - FT.now) < 1)

    # ... aber eine ALTE Freigabe-Uhr darf nicht uebernommen werden, sonst
    # gaebe der Neustart sofort frei.
    FT.now += 4000
    neu2 = mr.load_state()
    check("alte_freigabe_uhr_wird_verworfen", "batt_ok_since" not in neu2,
          f"= {neu2.get('batt_ok_since')}")
    check("batt_hold_bleibt_trotzdem", neu2.get("batt_hold") is True)


def test_ohne_schutz_kein_geisterhold():
    state = {"kwh": 35891, "kwh_ts": FT.now, "kwh_floor": 35891,
             "kwh_floor_ts": FT.now}
    mr.save_state(state)
    check("kein_hold_ohne_grund", mr.load_state().get("batt_hold") is None)


# --- A3: Eingefrorene DTU-Werte sind keine Messwerte ---------------------
def test_frische_erkennung():
    mr._livedata_meta = {}
    check("nie_abgefragt_ist_stale", mr.livedata_stale(30) is True)

    mr._livedata_meta = {"ts": FT.now, "age_s": 0.0,
                         "reachable": True, "producing": True}
    check("frisch_ist_frisch", mr.livedata_stale(30) is False)

    mr._livedata_meta = {"ts": FT.now, "age_s": 120.0,
                         "reachable": True, "producing": True}
    check("alter_wert_der_dtu_ist_stale", mr.livedata_stale(30) is True)

    mr._livedata_meta = {"ts": FT.now, "age_s": 0.0,
                         "reachable": False, "producing": False}
    check("inverter_nicht_erreichbar_ist_stale", mr.livedata_stale(30) is True)

    mr._livedata_meta = {"ts": FT.now - 600, "age_s": 0.0,
                         "reachable": True, "producing": True}
    check("eigene_abfrage_zu_alt_ist_stale", mr.livedata_stale(30) is True)


for fn in (test_failsafe_geht_nicht_am_waechter_vorbei,
           test_failsafe_ohne_akku_unveraendert,
           test_failsafe_ohne_dtu_bleibt_vorsichtig,
           test_schutz_ueberlebt_neustart,
           test_ohne_schutz_kein_geisterhold,
           test_frische_erkennung):
    print(f"\n{fn.__name__}:")
    fn()

print()
if FAILS:
    print(f"{len(FAILS)} FEHLGESCHLAGEN: {', '.join(FAILS)}")
    sys.exit(1)
print("Alle Tests gruen")
