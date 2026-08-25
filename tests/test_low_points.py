#!/usr/bin/env python3
"""Tests fuer die Arbeitspunkt-Leiter, den SoC-Sollwert und den
Anti-Zappel-Schutz auf dem Hoch-Pfad.

Lauf:  python3 tests/test_low_points.py

Die Leiter ersetzt die alte Zwei-Wege-Entscheidung am Sustain-Floor
(halten ODER schlafen). Invarianten, die hier festgenagelt werden:

  L1  Bei einem Bedarf, den ein Arbeitspunkt besser deckt als Floor oder
      Schlaf, wird dieser Punkt gewaehlt — das ist der ganze Zweck.
  L2  Voller Akku: es wird IMMER gehalten (Ueberschuss waere ohnehin
      abgeregelt, Einspeisen kostet dann nichts) — Verhalten aus 1.7.23.
  L3  Ein Punkt, der zweimal verfehlt wurde, wird gesperrt; danach ist das
      Verhalten exakt das alte (Floor oder Schlaf). Die Leiter kann also
      nie SCHLECHTER sein als die Zwei-Wege-Logik.
  L4  Zappelnde Last erzeugt keine Kette von Limit-Wechseln (Dwell +
      Hysterese + Median-Glaettung).
  L5  Der Netz-Sollwert folgt dem LADESTAND, nicht linear der Spannung.
  L6  Kleine Aufwaerts-Schritte gehen erst nach Bestaetigung raus, grosse
      sofort.
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
os.environ.setdefault("INVERTER_SERIAL", "1164a00ab8d4")
os.environ.setdefault("BATT_STRINGS", "1,4")
os.environ.setdefault("BATT_LOW_V", "47.0")
os.environ.setdefault("BATT_HIGH_V", "54.4")
os.environ["STATE_FILE"] = str(Path(tempfile.mkdtemp()) / "state.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import meter_reader as mr  # noqa: E402

FAILED = []


def check(name, cond, info=""):
    print(f"  {'OK  ' if cond else 'FAIL'}  {name}" + (f"  — {info}" if info else ""))
    if not cond:
        FAILED.append(name)


class Rig:
    """Minimal-Ersatz fuer control(): ruft nur low_control und protokolliert
    die gesendeten Limits, mit einem Inverter-Modell aus den Messdaten."""

    def __init__(self, land=None, batt_v=52.0):
        # land: Kommando -> tatsaechlich gelieferte AC-Leistung
        self.land = land or {50: 37.0, 300: 160.0, 430: 425.0}
        self.state = {"batt_v": batt_v}
        self.current = None
        self.pv = 0.0
        self.now = 1_800_000_000.0
        self.sent = []

    def send(self, value, tag):
        self.sent.append((round(self.now, 1), value, tag))
        self.current = value
        return value

    def step(self, need, dt=1.0, settle=True):
        """Ein Regelzyklus mit Wunsch-Limit `need`."""
        self.now += dt
        if settle and self.current is not None:
            # Inverter folgt mit Totzeit; hier vereinfacht: sofort nach dem
            # naechsten Zyklus auf dem Landeplatz seines Kommandos
            self.pv = self.land.get(self.current, float(self.current))
        r = mr.low_control(self.state, need, self.pv, self.now,
                           self.current, self.send)
        self.current = r if r is not None else self.current
        return r

    def run(self, need, seconds, dt=1.0):
        for _ in range(int(seconds / dt)):
            self.step(need, dt)


def t_choice():
    print("\nL1: Arbeitspunkt wird nach Kosten gewaehlt")
    # 150 W Bedarf: Schlaf kostet 150 W Netzbezug, Floor verschenkt 275 W,
    # der 160-W-Punkt kostet 10 W.
    r = Rig()
    r.run(150, 60)
    check("150 W Bedarf -> 300er Kommando (~160 W AC)", r.current == 300,
          f"limit={r.current}")

    r = Rig()
    r.run(20, 60)
    check("20 W Bedarf -> schlafen", r.current == mr.MIN_LIMIT_W,
          f"limit={r.current}")

    r = Rig()
    r.run(400, 60)
    check("400 W Bedarf -> Floor halten", r.current == mr.SUSTAIN_FLOOR_W,
          f"limit={r.current}")

    # Kipppunkt-Kontrolle: knapp unter der Mitte zwischen 160 und 425
    r = Rig()
    r.run(300, 60)
    check("300 W Bedarf -> Floor (naeher an 425 als an 160)",
          r.current == mr.SUSTAIN_FLOOR_W, f"limit={r.current}")


def t_batt_full():
    print("\nL2: voller Akku -> immer halten (1.7.23)")
    r = Rig(batt_v=mr.BATT_HIGH_V + 0.1)
    r.run(120, 60)
    check("voller Akku, 120 W Bedarf -> Floor", r.current == mr.SUSTAIN_FLOOR_W,
          f"limit={r.current}")


def t_miss_fallback():
    print("\nL3: verfehlter Punkt wird gesperrt, Verhalten faellt aufs alte zurueck")
    # Inverter faellt beim 300er Kommando ganz aus (der gemessene 26-%-Fall)
    r = Rig(land={50: 37.0, 300: 0.0, 430: 425.0})
    r.run(150, 30)
    check("erster Versuch: 300 gesendet", 300 in [s[1] for s in r.sent])
    r.run(150, 120)
    tries = [s for s in r.sent if s[1] == 300]
    check("hoechstens zwei Anlaeufe", len(tries) <= 2, f"{len(tries)} Versuche")
    check("Punkt gesperrt", r.state["lp_block"].get(300, 0) > r.now)
    check("faellt auf Schlaf zurueck (150 W < 215 W Kipppunkt)",
          r.current == mr.MIN_LIMIT_W, f"limit={r.current}")
    # Und bleibt dort, solange die Sperre laeuft
    before = len(r.sent)
    r.run(150, 300)
    check("keine neuen Versuche waehrend der Sperre",
          all(s[1] != 300 for s in r.sent[before:]))


def t_learn():
    print("\nLernen: die Erwartung zieht auf den gemessenen Punkt nach")
    r = Rig(land={50: 37.0, 300: 185.0, 430: 425.0})
    r.run(150, 90)
    check("Treffer innerhalb der Toleranz akzeptiert", r.current == 300,
          f"limit={r.current}")
    gelernt = r.state["lp_ac"].get(300, 0)
    check("Erwartung wandert zur gemessenen Leistung",
          167.0 <= gelernt <= 185.0, f"gelernt={gelernt} (Start 160, ist 185)")
    check("Schlaf/Floor werden passiv mitvermessen, nie gesperrt",
          not r.state.get("lp_block"), f"{r.state.get('lp_block')}")


def t_wegkippen():
    print("\nSpaeteres Wegkippen: Punkt wird nachverifiziert und gesperrt")
    r = Rig()
    r.run(150, 60)
    check("erst auf dem Arbeitspunkt", r.current == 300, f"limit={r.current}")
    r.land[300] = 0.0                      # Inverter faellt spaeter aus
    r.run(150, 180)
    check("Punkt gesperrt", bool(r.state.get("lp_block", {}).get(300)),
          f"{r.state.get('lp_block')}")
    check("Erwartung verworfen (faire zweite Chance nach der Sperre)",
          300 not in r.state.get("lp_ac", {}))
    check("Rueckfall auf Schlaf", r.current == mr.MIN_LIMIT_W,
          f"limit={r.current}")


def t_floor_nie_gesperrt():
    print("\nFloor kippt weg: Erwartung sinkt, Sperre gibt es nicht")
    r = Rig(land={50: 37.0, 300: 160.0, 430: 165.0})   # Floor liefert nur 165
    r.run(400, 400)
    check("Floor nicht gesperrt", mr.SUSTAIN_FLOOR_W not in r.state.get("lp_block", {}),
          f"{r.state.get('lp_block')}")
    gelernt = r.state["lp_ac"].get(mr.SUSTAIN_FLOOR_W, 430)
    check("Erwartung des Floors sinkt Richtung Messwert", gelernt < 400,
          f"gelernt={gelernt:.0f}")


def t_no_flapping():
    print("\nL4: zappelnde Last erzeugt keine Kommando-Kette")
    r = Rig()
    seq = [180, 266, 150, 240, 170, 255, 160, 230] * 40   # 320 s Gezappel
    for i, need in enumerate(seq):
        r.step(need, 1.0)
    check("hoechstens 3 Limit-Kommandos in 320 s", len(r.sent) <= 3,
          f"{len(r.sent)} Kommandos: {[s[1] for s in r.sent]}")


def t_target_soc():
    print("\nL5: Netz-Sollwert folgt dem Ladestand")
    st = {"batt_v": 52.7, "batt_pv": 0.0}
    soc = mr.soc_estimate(52.7, 0.0)
    tgt = mr.target_grid(st)
    lin = mr.TARGET_GRID_W + ((52.7 - mr.BATT_LOW_V) /
                              (mr.BATT_HIGH_V - mr.BATT_LOW_V)) * \
        (mr.TARGET_GRID_FULL_W - mr.TARGET_GRID_W)
    check("52,7 V wird als ~50 % gelesen, nicht als 77 %",
          45 <= soc <= 55, f"soc={soc}")
    check("Ziel weniger einspeise-freudig als die lineare Rechnung",
          tgt > lin + 10, f"soc-Ziel={tgt} W, linear={lin:.0f} W")
    check("leerer Akku -> TARGET_GRID_W",
          mr.target_grid({"batt_v": mr.BATT_LOW_V, "batt_pv": 0.0})
          == mr.TARGET_GRID_W)
    check("voller Akku -> TARGET_GRID_FULL_W",
          mr.target_grid({"batt_v": mr.BATT_HIGH_V + 1, "batt_pv": 0.0})
          == mr.TARGET_GRID_FULL_W)


def t_up_confirm():
    """L6 gegen die echte control()-Funktion, mit gefaelschtem OpenDTU."""
    print("\nL6: kleine Hoch-Schritte brauchen Bestaetigung, grosse nicht")
    sent = []
    mr.get_livedata = lambda: (600.0, {1: (52.0, 300.0), 4: (52.0, 300.0)})
    mr.set_limit = lambda w: sent.append(w)
    now = [1_800_000_000.0]
    real_time = mr.time.time
    mr.time.time = lambda: now[0]
    try:
        st = {"limit_w": 600, "batt_v": 52.0}
        # +40 W Fehler -> kleiner Schritt, darf nicht sofort raus
        mr.control(40, st)
        check("kleiner Schritt: kein Sofort-Kommando", not sent, f"{sent}")
        now[0] += 1.0
        mr.control(40, st)
        check("nach 1 s immer noch nicht", not sent, f"{sent}")
        now[0] += 3.0
        mr.control(40, st)
        check("nach 4 s bestaetigt und gesendet", len(sent) == 1, f"{sent}")
        # Grosser Lastsprung -> sofort
        sent.clear()
        st = {"limit_w": 600, "batt_v": 52.0}
        now[0] += 10.0
        mr.control(900, st)
        check("grosser Lastsprung geht sofort raus", len(sent) == 1, f"{sent}")
    finally:
        mr.time.time = real_time


def t_kick_vorrang():
    """R1/R2: klemmt der Inverter, gehoert der Fall dem MPPT-Kick — die
    Leiter darf ihn weder uebernehmen noch einen laufenden Kick einfrieren."""
    print("\nR1: verklemmter Inverter geht an den Kick, nicht an die Leiter")
    sent = []
    dc = {1: (52.0, 100.0), 4: (52.0, 100.0)}
    pv = [178.0]
    mr.get_livedata = lambda: (pv[0], dc)
    mr.set_limit = lambda w: sent.append(w)
    now = [1_800_000_000.0]
    real_time = mr.time.time
    mr.time.time = lambda: now[0]
    try:
        # Limit 430, Inverter liefert nur 178 W, Haus zieht 250 W -> klemmt
        st = {"limit_w": 430, "batt_v": 52.0}
        for _ in range(40):          # STUCK_S lang flach halten
            now[0] += 1.0
            mr.control(250, st)
        check("Kick statt Leiter", any(w > 430 for w in sent),
              f"gesendet={sent}")
        check("laufender Kick wird von der Leiter nicht abgeraeumt",
              "kick" in st, f"kick={st.get('kick')}")
        check("Kick eskaliert weiter", len(sent) >= 2, f"gesendet={sent}")
        # Loest der Kick, wird er sauber abgeschlossen (kick_result) und die
        # Leiter uebernimmt erst danach wieder
        pv[0] = 430.0
        now[0] += 2.0
        mr.control(250, st)
        check("geloester Kick wird abgeschlossen", "kick" not in st,
              f"kick={st.get('kick')}")
    finally:
        mr.time.time = real_time


def t_up_since_verfaellt():
    """R6: die Bestaetigungsuhr darf eine Pause nicht ueberleben."""
    print("\nR6: alter Bestaetigungs-Zeitstempel wird verworfen")
    sent = []
    mr.get_livedata = lambda: (600.0, {1: (52.0, 300.0), 4: (52.0, 300.0)})
    mr.set_limit = lambda w: sent.append(w)
    now = [1_800_000_000.0]
    real_time = mr.time.time
    mr.time.time = lambda: now[0]
    try:
        st = {"limit_w": 600, "batt_v": 52.0}
        mr.control(40, st)                 # Uhr startet
        check("Uhr laeuft", st.get("up_since") is not None)
        now[0] += 1.0
        mr.control(0, st)                  # Fehler weg -> Uhr muss weg
        check("Uhr geloescht, sobald der Fehler weg ist",
              st.get("up_since") is None, f"{st.get('up_since')}")
        now[0] += 600.0
        mr.control(40, st)                 # neuer kleiner Fehler
        check("neuer kleiner Schritt wartet wieder", not sent, f"{sent}")
    finally:
        mr.time.time = real_time


def t_up_confirm_greift_bei_68w():
    """R7: der Median-Zappler aus den Logs (68 W) muss erfasst werden."""
    print("\nR7: 68-W-Zappler wird abgefangen, 200-W-Sprung nicht")
    sent = []
    mr.get_livedata = lambda: (430.0, {1: (52.0, 215.0), 4: (52.0, 215.0)})
    mr.set_limit = lambda w: sent.append(w)
    now = [1_800_000_000.0]
    real_time = mr.time.time
    mr.time.time = lambda: now[0]
    try:
        st = {"limit_w": 430, "batt_v": 52.0}
        mr.control(70, st)                 # ~68 W Fehler
        now[0] += 1.0
        mr.control(70, st)
        check("68-W-Zappler wartet auf Bestaetigung", not sent, f"{sent}")
        sent.clear()
        st = {"limit_w": 430, "batt_v": 52.0}
        now[0] += 10.0
        mr.control(200, st)                # klarer Lastsprung
        check("200-W-Fehler geht sofort raus", len(sent) == 1, f"{sent}")
    finally:
        mr.time.time = real_time


def t_soc_glaettung():
    print("\nR3: Ladestand fuers Ziel wird geglaettet (keine Mitkopplung)")
    real_time = mr.time.time
    now = [1_800_000_000.0]
    mr.time.time = lambda: now[0]
    try:
        st = {"batt_v": 52.7, "batt_pv": 0.0}
        leer = mr.target_grid(st)
        now[0] += 2.0
        st["batt_pv"] = 1400.0             # Lastsprung
        unter_last = mr.target_grid(st)
        check("Ziel springt nicht mit der Last",
              abs(unter_last - leer) <= 2, f"{leer} W -> {unter_last} W")
        for _ in range(30):                # nach Minuten zieht es nach
            now[0] += 60.0
            spaet = mr.target_grid(st)
        check("nach Minuten folgt es der Schaetzung", spaet < leer,
              f"{leer} W -> {spaet} W")
    finally:
        mr.time.time = real_time


def t_ladder_off():
    print("\nAbschaltbar: LOW_POINTS leer -> alte Zwei-Wege-Logik")
    saved_raw, saved_cache = mr.LOW_POINTS_RAW, mr._low_ladder
    try:
        mr.LOW_POINTS_RAW = ""
        mr._low_ladder = None
        check("Leiter leer", mr.low_ladder() == [])
    finally:
        mr.LOW_POINTS_RAW, mr._low_ladder = saved_raw, saved_cache
    saved_raw, saved_cache = mr.LOW_POINTS_RAW, mr._low_ladder
    try:
        mr.LOW_POINTS_RAW = "300:160, kaputt, 9999:100, 40:20, 250:130"
        mr._low_ladder = None
        pts = mr.low_ladder()
    finally:
        mr.LOW_POINTS_RAW, mr._low_ladder = saved_raw, saved_cache
    check("Parser nimmt nur gueltige Punkte innerhalb (MIN_LIMIT, Floor)",
          pts == [(250, 130), (300, 160)], f"{pts}")


if __name__ == "__main__":
    print(f"Leiter: {mr.low_ladder()}  Floor: {mr.SUSTAIN_FLOOR_W} W  "
          f"Min: {mr.MIN_LIMIT_W} W")
    t_choice()
    t_batt_full()
    t_miss_fallback()
    t_learn()
    t_wegkippen()
    t_floor_nie_gesperrt()
    t_no_flapping()
    t_target_soc()
    t_up_confirm()
    t_up_since_verfaellt()
    t_up_confirm_greift_bei_68w()
    t_kick_vorrang()
    t_soc_glaettung()
    t_ladder_off()
    print()
    if FAILED:
        print(f"{len(FAILED)} FEHLGESCHLAGEN: {FAILED}")
        sys.exit(1)
    print("alle Tests bestanden")
