#!/usr/bin/env python3
"""Umfassende Tests fuer ALLE kWh-Tore.  Lauf:  .venv/bin/python tests/test_kwh_gates.py

Jeder historische Vorfall UND jeder Befund des adversarialen Angriffs-
Workflows (monotonie-angriff-2, 28.07.) ist als Replay verewigt. Die
Zeugen (Gemini, Segment-Dekoder) sind in den Angriffstests MAXIMAL
boesartig. Invarianten:

  I1  Der Stand sinkt nie um mehr als KWH_HEAL_MAX (1) — und selbst das
      nur mit exakter Gemini-Bestaetigung.
  I2  STRIKT: Jeder akzeptierte Anstieg ist <= max(1, Rate x Zeit seit
      letzter akzeptierter Lesung). KEIN konstanter Schlupf.
  I2k KUMULATIV: Ueber jedes Paar akzeptierter Lesungen (auch ueber
      Neustarts hinweg) steigt der Stand hoechstens Rate x Zeit + 1.
  I3  Ein Stand > KWH_ABS_MAX (99999) wird NIE akzeptiert.
  I4  Ein Gemini-Kandidat wird nie von Gemini bestaetigt.
  I5  Re-Baseline braucht >= 4 konsistente Lesungen ueber >= 3 Minuten.

WICHTIG (Lehre aus dem Angriff): die Assertions hier duerfen NIE die
Formel aus dem Produktivcode nachbilden — sonst erben sie seine Fehler.
"""
import json
import os
import random
import sys
import tempfile
import traceback
from pathlib import Path

# --- Import-Umgebung: keine echten Verbindungen -------------------------
os.environ.setdefault("OPENDTU_URL", "http://test.invalid")
os.environ.setdefault("OPENDTU_USER", "t")
os.environ.setdefault("OPENDTU_PASS", "t")
os.environ.setdefault("MQTT_USER", "CHANGE_ME")
os.environ.setdefault("MQTT_PASS", "x")
os.environ.setdefault("READER_MODE", "gemini")
os.environ.setdefault("SAVE_SAMPLES_DIR", "")
_state_tmp = Path(tempfile.mkdtemp()) / "state.json"
os.environ["STATE_FILE"] = str(_state_tmp)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import meter_reader as mr


# --- Fake-Zeit: Tests warten keine 3 Minuten -----------------------------
class FakeTime:
    def __init__(self):
        import time as _t
        self._real = _t
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def __getattr__(self, a):          # strftime etc. durchreichen
        return getattr(self._real, a)


FT = FakeTime()
mr.time = FT
mr.GEMINI_COOLDOWN_S = 0
RATE = mr.KWH_MAX_RATE_KWH_H


# --- Kontrollierbare Zeugen ---------------------------------------------
class World:
    """Steuert, was Gemini und der Segment-Dekoder 'sehen'."""

    def reset(self):
        self.gemini = None                       # dict | Exception | Callable
        self.seg_decide = lambda *c: (None, 0.0)
        self.seg_confirm = lambda lo, hi, st: None
        self.gemini_calls = 0
        mr._gemini_err_since = None

    def gemini_read(self, img):
        self.gemini_calls += 1
        g = self.gemini
        if callable(g):
            g = g()
        if isinstance(g, Exception) or g is None:
            raise RuntimeError("Gemini down (Test)") if g is None else g
        return dict(g)


W = World()
mr.gemini_read = W.gemini_read
mr.get_snapshot = lambda: b"img"
mr.seg_decide = lambda *c: W.seg_decide(*c)
mr.seg_confirm = lambda lo, hi, st: W.seg_confirm(lo, hi, st)
mr.save_event = lambda *a, **k: None
mr.retrain_mark = lambda *a, **k: None

ACCEPTS: list = []                     # (t, kwh) — ueberlebt Neustarts (I2k)


def fresh_state(kwh=None, w=400, kwh_ts=None):
    s = {}
    if kwh is not None:
        s["kwh"], s["w"] = kwh, w
        s["kwh_ts"] = FT.now if kwh_ts is None else kwh_ts
    return s


def step(state, kwh, w=400, source="local c=0.97", dt=1.5):
    """Ein Zyklus durch ALLE Tore. -> (akzeptiert, Info). Prueft I1-I3."""
    FT.now += dt
    prev, prev_ts = state.get("kwh"), state.get("kwh_ts", FT.now)
    try:
        reading, src = mr.guard_kwh({"kwh": kwh, "w": w}, source, state)
    except AssertionError:
        raise
    except Exception as e:             # wie der Hauptloop: Frame verworfen
        return False, str(e)
    state.update(reading)
    state["kwh_ts"] = FT.now
    cur = state["kwh"]
    if prev is not None and cur is not None:
        assert cur >= prev - mr.KWH_HEAL_MAX, f"I1 VERLETZT: {prev} -> {cur}"
        allow = max(1.0, RATE * max(0.0, FT.now - prev_ts) / 3600)
        assert cur - prev <= allow + 1e-9, (
            f"I2 VERLETZT: {prev} -> {cur} (erlaubt +{allow:.2f})")
    if cur is not None:
        assert cur <= mr.KWH_ABS_MAX, f"I3 VERLETZT: {cur}"
        for t0, k0 in ACCEPTS:
            assert cur - k0 <= RATE * (FT.now - t0) / 3600 + 1 + 1e-9, (
                f"I2k VERLETZT: {k0} -> {cur} in {(FT.now - t0)/3600:.2f}h")
        ACCEPTS.append((FT.now, cur))
        del ACCEPTS[:-500]
    return True, src


def persist(state):
    """Exakt das Persist-Format des Hauptloops."""
    _state_tmp.write_text(json.dumps(
        {"kwh": state.get("kwh"), "ts": state.get("kwh_ts"),
         "floor": state.get("kwh_floor"),
         "floor_ts": state.get("kwh_floor_ts"),
         "hist": state.get("kwh_hist", [])}))


def restart(state):
    persist(state)
    return mr.load_state()


def watchdog_free(state):
    """Exakt die Watchdog-Freigabe aus main() (Zeilen kwh_floor=...)."""
    state["kwh_floor"] = state["kwh"] - mr.KWH_HEAL_MAX
    state["kwh_floor_ts"] = FT.now
    state["kwh_lost"] = state["kwh"]
    state["kwh"] = None
    state["rb_counts"] = {}


# ========================================================================
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@test
def struktur_deckel_28_07():
    """DER Vorfall: 358914 (Nachkommastelle angehaengt) und jede andere
    >5-stellige Geisterlesung prallt ab — selbst wenn Gemini und der
    Segment-Dekoder sie bestaetigen."""
    W.reset()
    for ghost in (358914, 358911, 585870, 880080, 100000, 999999, 3589140):
        st = fresh_state(35891)
        W.gemini = {"kwh": ghost, "w": 400}
        W.seg_decide = lambda *c: (c[0], 5.0)          # bestaetigt ALLES
        for _ in range(400):                            # ~10 min
            ok, info = step(st, ghost)
            assert not ok, f"Geist {ghost} akzeptiert: {info}"
        assert st["kwh"] == 35891


@test
def witness_signatur():
    """witness_match: exakt oder Nachkomma-Signatur (kandidat*10+zehntel)
    zaehlt als Bestaetigung — alles andere nicht. Gemini-Lesungen werden
    NIE mehr normalisiert (das wusch 6-stelligen Muell in gueltige Werte)."""
    W.reset()
    assert mr.witness_match(35891, 35891)
    assert mr.witness_match(358914, 35891)      # 35891.4
    assert mr.witness_match(358910, 35891)      # 35891.0
    assert not mr.witness_match(358914, 35894)
    assert not mr.witness_match(888888, 88888)  # Segmenttest ist kein Zeuge
    assert not mr.witness_match(35890, 35891)
    assert not mr.witness_match(3589140, 35891)


@test
def zeugen_trennung_gemini_bestaetigt_sich_nie():
    """I4-Replay 28.07. ~09:40: lokales OCR zeitweise unlesbar, Gemini-
    Lesung wird zum Kandidaten UND Gemini bestaetigt sie — auch ein
    5-stelliger Gemini-Fehler (45891), den der Struktur-Deckel nicht
    faengt, darf so nie reinkommen."""
    W.reset()
    st = fresh_state(35891)
    W.gemini = {"kwh": 45891, "w": 400}                # strukturell "ok"
    for i in range(1200):                               # ~30 min
        if i % 6 == 5:
            ok, _ = step(st, 45891, source="gemini")    # Gemini als Lesung
            assert not ok
        else:
            ok, _ = step(st, 35891)
            assert ok
    assert st["kwh"] == 35891


@test
def replay_28_07_morgen_schatten_senkung():
    """Replay 07:07: Schatten loescht Segment B, lokal liest konstant
    35850 statt 35890 — Gemini UND Segment-Dekoder bestaetigen im Test
    sogar den Fehler. -40 geht trotzdem NIE."""
    W.reset()
    st = fresh_state(35890)
    W.gemini = {"kwh": 35850, "w": 400}
    W.seg_decide = lambda *c: ((35850, 5.0) if 35850 in c else (None, 0.0))
    for _ in range(2400):                               # 1 h Schatten
        ok, _ = step(st, 35850)
        assert not ok
    assert st["kwh"] == 35890


@test
def senkung_um_1_nur_mit_exaktem_gemini():
    """-1 (die einzige erlaubte Heilung) braucht Gemini EXAKT — die
    Nachkomma-Signatur (358910 fuer 35891) zaehlt als exakt."""
    for gem_kwh, darf in ((35891, True), (358914, True), (35890, False),
                          (35892, False)):
        W.reset()
        st = fresh_state(35892)                         # +1 vergiftet
        W.gemini = {"kwh": gem_kwh, "w": 400}
        healed = False
        for _ in range(600):
            ok, _ = step(st, 35891)
            if ok:
                healed = True
                break
        assert healed == darf, f"Gemini {gem_kwh}: heal={healed}"
        assert st["kwh"] == (35891 if darf else 35892)


@test
def aufwaerts_physikdeckel():
    """+40 kWh in Minuten ist unmoeglich -> Veto trotz einstimmiger
    Zeugen. Nach 10 h Blindflug ist +40 moeglich -> Heilung greift."""
    W.reset()
    st = fresh_state(35891)
    W.gemini = {"kwh": 35931, "w": 400}
    W.seg_decide = lambda *c: ((35931, 5.0) if 35931 in c else (None, 0.0))
    for _ in range(1200):                               # 30 min druecken
        ok, _ = step(st, 35931)
        assert not ok, "Physik-Deckel durchbrochen"
    assert st["kwh"] == 35891
    st = fresh_state(35891, kwh_ts=FT.now - 10 * 3600)
    W.seg_decide = lambda *c: (None, 0.0)
    healed = False
    for _ in range(600):
        ok, _ = step(st, 35931)
        if ok:
            healed = True
            break
    assert healed and st["kwh"] == 35931


@test
def rebaseline_braucht_zeit_und_konsistenz():
    """I5: 4 Lesungen in 10 s reichen NICHT — >= 3 min Konsistenz."""
    W.reset()
    st = fresh_state(35891, kwh_ts=FT.now - 10 * 3600)
    W.gemini = {"kwh": 35931, "w": 400}
    t0 = FT.now
    accepted_at = None
    for _ in range(600):
        ok, _ = step(st, 35931, dt=2)
        if ok:
            accepted_at = FT.now
            break
    assert accepted_at is not None, "legitime Heilung kam nie durch"
    assert accepted_at - t0 >= mr.REBASE_MIN_SPAN_S
    assert W.gemini_calls >= 1


@test
def replay_26_07_kanaltrennung():
    """W-Flattern darf den kWh-Kanal nicht freischalten."""
    W.reset()
    st = fresh_state(35881, w=400)
    W.gemini = {"kwh": 35801, "w": 9075}
    for kwh_bad, w_bad in ((35861, 9075), (35801, 3075), (35801, 9075)):
        for _ in range(800):
            ok, _ = step(st, kwh_bad, w=w_bad)
            assert not ok
    assert st["kwh"] == 35881


@test
def angriff_w_sprung_oeffnet_kwh_rebaseline_nicht():
    """Angriff #6: 'Sprung +8675 W > 5000 W' matchte frueher den
    kWh-Re-Baseline-Trigger; Gemini bestaetigte den (korrekten!) Stand
    und der GEISTER-W ging durch. Jetzt: W-Gruende bleiben im W-Kanal."""
    W.reset()
    st = fresh_state(35891, w=400)
    W.gemini = {"kwh": 35891, "w": 400}                # bestaetigt Stand
    for _ in range(800):                                # 20 min W-Geister
        ok, _ = step(st, 35891, w=9075)
        assert not ok, "Geister-W per kWh-Re-Baseline freigegeben"
    assert st["w"] == 400
    assert W.gemini_calls == 0, "kWh-Re-Baseline lief fuer W-Fehler an"


@test
def angriff_seg_arbiter_prueft_struktur_und_w():
    """Angriff #2/#3: Der Seg-Schiedsrichter darf bei Stand 99999 kein
    100000 hineinreichen (I3) und entbindet nicht von der W-Pruefung."""
    W.reset()
    st = fresh_state(99999)
    W.seg_confirm = lambda lo, hi, s: hi               # boesartig: 100000
    for _ in range(50):
        ok, _ = step(st, 3)
        assert st["kwh"] <= mr.KWH_ABS_MAX
    assert st["kwh"] == 99999
    # W-Kanal: rueckläufige kWh + Geister-W, Arbiter bestaetigt den Stand
    W.reset()
    st = fresh_state(35891, w=400)
    W.seg_confirm = lambda lo, hi, s: lo               # bestaetigt Stand
    ok, info = step(st, 35879, w=9075)
    assert not ok and "Sprung" in info, f"Geister-W durchgerutscht: {info}"
    assert st["w"] == 400


@test
def lcd_segmenttest_und_muell():
    W.reset()
    st = fresh_state(35891)
    W.gemini = {"kwh": 888888, "w": 888888}
    for bad in (888888, 0, -5, 3, 379, 3579):
        for _ in range(200):
            ok, _ = step(st, bad)
            assert not ok
    assert st["kwh"] == 35891


@test
def plus1_braucht_zwei_lesungen():
    W.reset()
    st = fresh_state(35891)
    ok, _ = step(st, 35892)
    assert ok and st["kwh"] == 35891      # akzeptiert, aber Stand haelt
    ok, _ = step(st, 35892)
    assert ok and st["kwh"] == 35892      # zweite Lesung -> uebernommen


@test
def legit_ticken_wird_nie_blockiert():
    """Regression fuers Physik-Fenster: echtes Zaehlverhalten (2,2 kWh/h
    ueber 12 h, auch mal 2 Ticks in 20 min) darf NIE haengenbleiben."""
    W.reset()
    st = fresh_state(35891)
    kwh = 35891
    for i in range(int(12 * 3600 / 10)):
        if i % 160 == 159:                              # Tick alle ~27 min
            kwh += 1
        ok, _ = step(st, kwh, dt=10)
        assert ok, f"legitime Lesung {kwh} blockiert"
    # letzter Tick kann noch in der +1-Doppelbestaetigung haengen
    assert st["kwh"] >= kwh - 1


@test
def angriff_plus1_ratsche_wird_gedeckelt():
    """Angriff #1/#8: die +3-Ratsche ist tot (Seg-Pfad nur noch +1, kein
    +2-Schlupf). Und selbst eine perfekte +1-Geister-Ratsche alle 3 min
    (20 kWh/h) laeuft ins kumulative 6h-Fenster: mehr als Rate x Zeit + 1
    geht nicht — egal wie boesartig die Zeugen sind."""
    W.reset()
    start = 35891
    st = fresh_state(start)
    W.seg_decide = lambda *c: ((c[0], 9.9) if c and c[0] != st.get("kwh")
                               else (None, 0.0))
    W.gemini = lambda: {"kwh": (st.get("kwh") or 0) + 3, "w": 400}
    for _ in range(1200):                               # +3-Ratsche: nie
        ok, _ = step(st, st["kwh"] + 3)
        assert not ok
    assert st["kwh"] == start
    # +1-Ratsche: einzeln nicht unterscheidbar von echtem Tick, aber
    # kumulativ hart gedeckelt (I2k wird in step() ohnehin geprueft)
    W.gemini = lambda: {"kwh": (st.get("kwh") or 0) + 1, "w": 400}
    t0 = FT.now
    for _ in range(int(6 * 3600 / 10)):
        step(st, st["kwh"] + 1, dt=10)
    rise = st["kwh"] - start
    hours = (FT.now - t0) / 3600
    assert rise <= RATE * hours + 2, f"Ratsche: +{rise} in {hours:.1f}h"


@test
def angriff_watchdog_pumpe_gedeckelt():
    """Angriff #4/#10: Watchdog-Freigabe -> Basis-Fenster war eine
    zeugenlose Pumpe (+2 je 14 min, -1 ohne Gemini). Jetzt: -1 nur mit
    Gemini, +Schritte bleiben im kumulativen Physik-Fenster."""
    W.reset()
    start = 35891
    st = fresh_state(start)
    W.gemini = None                                     # Gemini tot
    for _ in range(30):                                 # 30 Pump-Runden
        watchdog_free(st)
        FT.now += 840                                   # ~14 min je Runde
        for _ in range(20):                             # Angreifer drueckt
            step(st, (st.get("kwh") or st["kwh_floor"] + 1) + 1)
        if st.get("kwh") is None:                       # Basis wieder setzen
            step(st, st["kwh_floor"] + 1)
            step(st, st["kwh_floor"] + 1)
        assert st.get("kwh") is not None
    rise = st["kwh"] - start
    hours = (FT.now - ACCEPTS[0][0]) / 3600 if ACCEPTS else 7
    assert rise <= RATE * hours + 2, f"Pumpe: +{rise} in {hours:.1f}h"
    # Und: -1 ueber die Watchdog-Tuer braucht Gemini
    W.reset()
    st = fresh_state(35891)
    W.gemini = None
    watchdog_free(st)
    for _ in range(200):
        ok, _ = step(st, 35890)                         # unter altem Stand
        assert not ok, "-1 ohne Gemini durch die Watchdog-Tuer"
    W.gemini = {"kwh": 35890, "w": 400}                 # Zeuge da -> ok
    accepted = False
    for _ in range(200):
        ok, _ = step(st, 35890)
        if ok:
            accepted = True
            break
    assert accepted and st["kwh"] == 35890


@test
def angriff_restart_verliert_boden_nicht():
    """Angriff #0/#5/#11: Watchdog-Freigabe + Neustart loeschte den Boden
    ({"kwh": null} -> leerer State) — danach war der Stand mit zwei
    Lesungen frei waehlbar. Jetzt ueberlebt der Boden den Neustart."""
    W.reset()
    st = fresh_state(35891)
    step(st, 35891)
    watchdog_free(st)
    st2 = restart(st)                                   # <- der Angriff
    assert st2.get("kwh_floor") == 35890, "Boden im Neustart verloren"
    W.gemini = None
    for ziel in (12345, 99999, 1):
        for _ in range(300):
            ok, _ = step(st2, ziel)
            assert not ok, f"Stand nach Neustart frei waehlbar: {ziel}"
    ok1, _ = step(st2, 35891)
    ok2, _ = step(st2, 35891)
    assert ok2 and st2["kwh"] == 35891                  # echte Basis ok


@test
def kaltstart_braucht_konsens():
    """Echter Erststart (kein Anker, keine Datei): 4 Lesungen ueber 60 s
    je Kandidat — ein einzelner Geister-Frame setzt nie den Anker, und
    Alternation (Angriff #9) blockiert nicht dauerhaft."""
    W.reset()
    st = {}
    ok, _ = step(st, 8443)
    assert not ok, "Einzelframe setzte den Anker"
    st = {}
    accepted_at = None
    t0 = FT.now
    for i in range(20):
        kwh = 35891 if i % 2 == 0 else 35892            # strenge Alternation
        ok, _ = step(st, kwh, dt=10)
        if ok:
            accepted_at = FT.now
            break
    assert accepted_at is not None, "Alternation blockierte den Kaltstart"
    assert accepted_at - t0 >= 60
    assert st["kwh"] in (35891, 35892)


@test
def basis_fenster_nach_standverlust():
    """Nach Watchdog-Freigabe: Fenster ist [Stand-1, Stand+1]; Muell weit
    ausserhalb (88888) kommt ohne Zeugen nie rein."""
    W.reset()
    st = fresh_state(35891)
    watchdog_free(st)
    W.gemini = None
    for _ in range(300):
        ok, _ = step(st, 88888)
        assert not ok, "Basis-Muell ohne Zeugen akzeptiert"
    ok, _ = step(st, 35891)
    assert not ok, "Basis ohne zweite Lesung akzeptiert"
    ok, _ = step(st, 35891)
    assert ok and st["kwh"] == 35891 and "kwh_floor" not in st


@test
def basis_unter_boden_nur_mit_doppel_gemini():
    """Vergifteter Boden UEBER der Wahrheit (585870-Heilung -> 58587):
    zurueck zur echten 35891 nur ueber 4x/3min + ZWEI exakte Gemini-
    Bestaetigungen — mit widersprechendem Gemini nie."""
    W.reset()
    st = {"kwh_floor": 58587, "kwh_floor_ts": FT.now}
    W.gemini = {"kwh": 35891, "w": 400}
    healed = False
    for _ in range(600):
        ok, _ = step(st, 35891)
        if ok:
            healed = True
            break
    assert healed and st["kwh"] == 35891 and "kwh_floor" not in st
    assert W.gemini_calls >= 2, "Doppel-Bestaetigung wurde nicht verlangt"
    W.reset()
    st = {"kwh_floor": 58587, "kwh_floor_ts": FT.now}
    W.gemini = {"kwh": 35892, "w": 400}                 # widerspricht
    for _ in range(600):
        ok, _ = step(st, 35891)
        assert not ok
    assert st.get("kwh") is None


@test
def angriff_deadlock_hat_notausweg():
    """Angriff #7: vergifteter Boden + Gemini TOT war ein Dauer-Deadlock
    (72 h Failsafe). Jetzt: nach >= 6 h durchgehendem Gemini-AUSFALL
    (Widerspruch zaehlt nicht!) oeffnet der enge lokale Notausweg —
    vorher nicht, und mit lebendem Gemini nie."""
    W.reset()
    st = {"kwh_floor": 58587, "kwh_floor_ts": FT.now}
    W.gemini = None
    mr._gemini_err_since = FT.now                       # Ausfall beginnt
    W.seg_decide = lambda *c: ((35891, 2.0) if 35891 in c else (None, 0.0))
    t0 = FT.now
    accepted_at = None
    for _ in range(6000):
        ok, _ = step(st, 35891, dt=10)
        if ok:
            accepted_at = FT.now
            break
    assert accepted_at is not None, "Deadlock: Notausweg kam nie"
    assert accepted_at - t0 >= mr.GEMINI_DEAD_GRACE_H * 3600 - 60, (
        f"Notausweg zu frueh: nach {(accepted_at - t0)/3600:.1f}h")
    assert st["kwh"] == 35891
    # Gemini lebt (widerspricht nur) -> Notausweg bleibt ZU
    W.reset()
    st = {"kwh_floor": 58587, "kwh_floor_ts": FT.now}
    W.gemini = {"kwh": 35892, "w": 400}
    W.seg_decide = lambda *c: ((35891, 2.0) if 35891 in c else (None, 0.0))
    for _ in range(4000):                               # > 11 h
        ok, _ = step(st, 35891, dt=10)
        assert not ok, "Notausweg trotz lebendem (widersprechendem) Gemini"


@test
def angriff_zeitstempel_manipulation():
    """Angriff #13: ts aus der Zukunft oder Uralt-ts darf den Physik-
    Deckel nicht oeffnen. Zukunft -> auf jetzt geklemmt; Vergangenheit
    -> auf 72 h gedeckelt."""
    W.reset()
    _state_tmp.write_text(json.dumps({"kwh": 35891, "ts": FT.now + 9e6}))
    st = mr.load_state()
    assert st["kwh_ts"] <= FT.now                       # Zukunft geklemmt
    W.gemini = {"kwh": 35941, "w": 400}
    for _ in range(600):
        ok, _ = step(st, 35941)                         # +50 "nach Ausfall"
        assert not ok, "Zukunfts-ts oeffnete den Deckel"
    # Uralt-ts: Deckel auf 72 h begrenzt -> +400 bleibt verboten
    W.reset()
    st = fresh_state(35891, kwh_ts=FT.now - 90 * 24 * 3600)
    W.gemini = {"kwh": 36291, "w": 400}
    for _ in range(600):
        ok, _ = step(st, 36291)
        assert not ok, "Uralt-ts erlaubte +400"
    assert st["kwh"] == 35891


@test
def state_json_korruptur_heilung():
    """DIE Rettung: state.json enthaelt 358914 -> Laden heilt zu
    kwh=None + Boden 35891, die Kamera setzt den Stand neu."""
    W.reset()
    _state_tmp.write_text(json.dumps({"kwh": 358914, "ts": FT.now - 3600}))
    st = mr.load_state()
    assert st.get("kwh") is None
    assert st.get("kwh_floor") == 35891
    ok, _ = step(st, 35891)
    assert not ok
    ok, _ = step(st, 35891)
    assert ok and st["kwh"] == 35891
    _state_tmp.write_text(json.dumps({"kwh": 35891, "ts": FT.now}))
    assert mr.load_state()["kwh"] == 35891
    _state_tmp.write_text("kaputt{")
    assert mr.load_state() == {}
    _state_tmp.write_text(json.dumps({"kwh": None}))
    assert mr.load_state().get("kwh") is None


@test
def fuzz_adversarial_48h_mit_neustarts():
    """48 h Betrieb, alle Korruptionsklassen, MAXIMAL boesartige Zeugen
    (bestaetigen jeden Kandidaten) — und alle ~2 h ein Neustart ueber den
    echten Persist/Load-Pfad. I1-I3 + I2k prueft step(); am Ende darf der
    Stand nicht weiter als 3 kWh von der Wahrheit abweichen."""
    rng = random.Random(42)
    W.reset()
    truth = 35891.0
    st = fresh_state(35891)
    asked = {"kwh": None}

    def evil_gemini():
        return {"kwh": asked["kwh"], "w": 400}

    def corrupt(k):
        r = rng.random()
        s = f"{k:06d}"
        if r < 0.30:
            return int(s.replace("9", "5"))             # Schatten 9->5
        if r < 0.45:
            return k * 10 + rng.randrange(10)           # Nachkomma/Geist
        if r < 0.55:
            return int(str(rng.randrange(1, 10)) + s)   # Geisterziffer vorn
        if r < 0.65:
            i = rng.randrange(6)
            return int(s[:i] + s[i + 1:])               # Ziffer verloren
        if r < 0.75:
            return int(s[1:] + str(rng.randrange(10)))  # Zeile verrutscht
        if r < 0.85:
            return 888888
        return rng.randrange(1, 999999)

    W.seg_decide = lambda *c: (c[0], 9.9)               # boesartig: immer ja
    W.gemini = evil_gemini
    for i in range(int(48 * 3600 / 10)):
        truth += 10 / 1800.0                            # ~2 kWh/h
        good = int(truth)
        kwh = corrupt(good) if rng.random() < 0.25 else good
        asked["kwh"] = kwh
        step(st, kwh, dt=10)
        if i % 720 == 719:                              # alle 2 h: Neustart
            st = restart(st)
    drift = abs((st.get("kwh") or 0) - int(truth))
    assert st.get("kwh") is not None and drift <= 3, (
        f"Drift {drift} kWh (Stand {st.get('kwh')}, Wahrheit {int(truth)})")


@test
def ausfall_heilung_bleibt_moeglich():
    """3 Tage Addon aus, Zaehler real +90 kWh: Heilung MUSS durchgehen."""
    W.reset()
    st = fresh_state(35891, kwh_ts=FT.now - 72 * 3600)
    W.gemini = {"kwh": 35981, "w": 400}
    healed = False
    for _ in range(600):
        ok, _ = step(st, 35981)
        if ok:
            healed = True
            break
    assert healed and st["kwh"] == 35981


# ========================================================================
def main():
    failed = 0
    for fn in TESTS:
        FT.now += 48 * 3600                             # Tests entkoppeln
        ACCEPTS.clear()
        mr._last_gemini_call = 0.0
        try:
            fn()
            print(f"  OK   {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} Tests gruen")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
