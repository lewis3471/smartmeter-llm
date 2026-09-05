#!/usr/bin/env python3
"""Zusammenspiel Regler <-> AC-Automat.  Lauf:
    .venv/bin/python tests/test_ac_integration.py

Prueft die tragende Invariante: solange der Wechselrichter AC-seitig aus
ist, sendet control() KEIN einziges Limit — nicht ueber den Init-Pfad,
nicht ueber hoch/runter, nicht ueber Kick oder Floor-Schlaf. Ein Schutz,
der nur an einer von acht Sendestellen haengt, ist keiner.
"""
import os, sys, tempfile
from pathlib import Path
os.environ.update(OPENDTU_URL="http://test.invalid", OPENDTU_USER="t",
                  OPENDTU_PASS="t", MQTT_USER="CHANGE_ME", MQTT_PASS="x",
                  READER_MODE="gemini", SAVE_SAMPLES_DIR="",
                  BATT_STRINGS="1,4", INVERTER_SERIAL="1164a00ab8d4",
                  AC_SWITCH_ENTITY="switch.wr",
                  STATE_FILE=str(Path(tempfile.mkdtemp())/"s.json"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import meter_reader as mr

assert mr.ac_guard is not None, "ac_guard nicht importiert"
assert mr.ac_gate()["gate"] == "frei", "ohne Automat muss der Regler frei sein"

gesendet = []
mr.set_limit = lambda w, persistent=False: gesendet.append((w, persistent))
mr.get_livedata = lambda: (500.0, {1: (52.0, 250.0), 4: (52.0, 250.0)})
state = {}

# 1. Ohne Automat regelt control() wie bisher
lim, pv = mr.control(300, state)
assert gesendet, "control() haette ein Limit senden muessen"
print(f"  ok  ohne AC-Automat wird geregelt: {gesendet[-1]}")

# 2. Mit "stumm" schweigt der Regler vollstaendig
class FakeAc:
    def gate(self):
        return {"gate": "stumm", "cap": None, "state": "ac_aus",
                "reason": "Zelle leer", "block": "", "fault": False,
                "on": False, "deadman": "ok", "switches_today": 1,
                "automatik": True, "soc_valid": True, "ah_since_off": 0.0}
    def store(self, d, bye=False):
        d["acs"] = "ac_aus"; d["acx"] = bool(bye)
mr._ac = FakeAc()
state["pending"] = [(1, 2)]; state["kick"] = {"x": 1}
n = len(gesendet)
for _ in range(20):
    mr.control(2000, state)          # maximaler Bezug: wuerde sonst feuern
assert len(gesendet) == n, f"Regler hat trotz AC-aus {len(gesendet)-n} Limits gesendet"
assert "pending" not in state and "kick" not in state, "Zwischenstaende nicht verworfen"
print("  ok  bei AC-aus sendet der Regler kein einziges Limit")

# 3. Anlauf-Deckel
class CapAc(FakeAc):
    def gate(self):
        g = FakeAc.gate(self); g.update(gate="cap", cap=430, state="anlauf")
        return g
mr._ac = CapAc()
gesendet.clear()
state["limit_sent_ts"] = 0.0     # die 2s-Funkbremse nicht mitmessen
mr.control(2000, state)
assert gesendet and gesendet[-1][0] <= 430, f"Anlauf-Deckel verletzt: {gesendet}"
print(f"  ok  Anlauf deckelt auf {gesendet[-1][0]} W")

# 4. Zustand des Automaten landet in der state.json
mr.save_state(state, bye=True)
import json
roh = json.loads(Path(os.environ["STATE_FILE"]).read_text())
assert roh.get("acs") == "ac_aus" and roh.get("acx") is True, roh
print("  ok  Automatenzustand wird persistiert")
print("Rauchtest bestanden")
