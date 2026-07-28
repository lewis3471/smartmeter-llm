#!/usr/bin/env python3
"""Umfassende Tests fuer ALLE kWh-Tore.  Lauf:  .venv/bin/python tests/test_kwh_gates.py

Jeder historische Vorfall ist als Replay verewigt, dazu ein adversarialer
Fuzzer, in dem ein boesartiger "Gemini" und ein boesartiger Segment-
Dekoder JEDEN Kandidaten bestaetigen. Die Invarianten muessen trotzdem
halten:

  I1  Der Stand sinkt nie um mehr als KWH_HEAL_MAX (1) — und selbst das
      nur mit exakter Gemini-Bestaetigung.
  I2  Der Stand steigt pro Akzept um max. +1; per Re-Baseline hoechstens
      um (vergangene Zeit) x KWH_MAX_RATE_KWH_H.
  I3  Ein Stand > KWH_ABS_MAX (99999) wird NIE akzeptiert — das Display
      hat 6 Stellen mit fuehrender Null.
  I4  Ein Gemini-Kandidat wird nie von Gemini bestaetigt (Zeugen-Trennung).
  I5  Ein Re-Baseline braucht >= 4 konsistente Lesungen UEBER >= 3 Minuten
      — wir haben Zeit, niemand spekuliert auf eine Einzel-Lesung.
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


# --- Kontrollierbare Zeugen ---------------------------------------------
class World:
    """Steuert, was Gemini und der Segment-Dekoder 'sehen'."""

    def reset(self):
        self.gemini = None                       # dict | Exception | Callable
        self.seg_decide = lambda *c: (None, 0.0)
        self.seg_confirm = lambda lo, hi, st: None
        self.gemini_calls = 0

    def gemini_read(self, img):
        self.gemini_calls += 1
        g = self.gemini
        if callable(g):
            g = g()
        if isinstance(g, Exception):
            raise g
        if g is None:
            raise RuntimeError("Gemini down (Test)")
        return dict(g)


W = World()
mr.gemini_read = W.gemini_read
mr.get_snapshot = lambda: b"img"
mr.seg_decide = lambda *c: W.seg_decide(*c)
mr.seg_confirm = lambda lo, hi, st: W.seg_confirm(lo, hi, st)
mr.save_event = lambda *a, **k: None
mr.retrain_mark = lambda *a, **k: None


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
    except ValueError as e:
        return False, str(e)
    state.update(reading)
    state["kwh_ts"] = FT.now
    cur = state["kwh"]
    if prev is not None and cur is not None:
        assert cur >= prev - mr.KWH_HEAL_MAX, f"I1 VERLETZT: {prev} -> {cur}"
        cap = (prev + mr.MAX_KWH_STEP + 2
               + mr.KWH_MAX_RATE_KWH_H * max(0.0, FT.now - prev_ts) / 3600)
        assert cur <= cap, f"I2 VERLETZT: {prev} -> {cur} (Deckel {cap:.0f})"
    if cur is not None:
        assert cur <= mr.KWH_ABS_MAX, f"I3 VERLETZT: {cur}"
    return True, src


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
    for ghost in (358914, 358911, 585870, 880080, 100000, 999999, 358914_0):
        st = fresh_state(35891)
        W.gemini = {"kwh": ghost, "w": 400}
        W.seg_decide = lambda *c: (c[0], 5.0)          # bestaetigt ALLES
        for _ in range(400):                            # ~10 min
            ok, info = step(st, ghost)
            assert not ok, f"Geist {ghost} akzeptiert: {info}"
        assert st["kwh"] == 35891
        assert "strukturell" in info, info


@test
def gemini_nachkomma_normalisierung():
    """35891.4 -> '358914' wird auf 35891 repariert; der 8er-Segmenttest
    bleibt als 888888 erkennbar; normale Werte bleiben unangetastet."""
    W.reset()
    assert mr.gemini_normalize({"kwh": 358914, "w": 444})["kwh"] == 35891
    assert mr.gemini_normalize({"kwh": 358911, "w": 506})["kwh"] == 35891
    assert mr.gemini_normalize({"kwh": 888888, "w": 888888})["kwh"] == 888888
    assert mr.gemini_normalize({"kwh": 35891, "w": 400})["kwh"] == 35891
    assert mr.gemini_normalize({"kwh": 0, "w": 0})["kwh"] == 0


@test
def zeugen_trennung_gemini_bestaetigt_sich_nie():
    """Replay 28.07. ~09:40: lokales OCR zeitweise unlesbar, Gemini-Lesung
    358914 wird zum Kandidaten UND Gemini bestaetigt sie. Mit Zeugen-
    Trennung + Struktur-Deckel darf NICHTS davon durchkommen — auch nicht
    ein 5-stelliger Gemini-Fehler (45891), den der Deckel nicht faengt."""
    W.reset()
    st = fresh_state(35891)
    W.gemini = {"kwh": 45891, "w": 400}                # strukturell "ok"
    W.seg_decide = lambda *c: (None, 0.0)
    for i in range(1200):                               # ~30 min
        if i % 6 == 5:                                  # lokal unlesbar ->
            ok, _ = step(st, 45891, source="gemini")    # Gemini als Lesung
        else:
            ok, _ = step(st, 35891)
            assert ok
    assert st["kwh"] == 35891


@test
def replay_28_07_morgen_schatten_senkung():
    """Replay 07:07: Schatten loescht Segment B, lokal liest konstant
    35850 statt 35890 — und im Test bestaetigen sogar Gemini UND der
    Segment-Dekoder den Fehler. Die Monotonie-Invariante blockt trotzdem:
    -40 geht NIE."""
    W.reset()
    st = fresh_state(35890)
    W.gemini = {"kwh": 35850, "w": 400}
    W.seg_decide = lambda *c: ((35850, 5.0) if 35850 in c else (None, 0.0))
    W.seg_confirm = lambda lo, hi, s: None
    for _ in range(2400):                               # 1 h Schatten
        ok, _ = step(st, 35850)
        assert not ok
    assert st["kwh"] == 35890


@test
def senkung_um_1_nur_mit_exaktem_gemini():
    """-1 (die einzige erlaubte Heilung) braucht Gemini EXAKT."""
    W.reset()
    st = fresh_state(35892)                             # +1 vergiftet
    W.gemini = {"kwh": 35891, "w": 400}                 # Gemini: Wahrheit
    healed = False
    for _ in range(600):
        ok, _ = step(st, 35891)
        if ok:
            healed = True
            break
    assert healed and st["kwh"] == 35891
    # Gegenprobe: Gemini liest etwas anderes -> Heilung verweigert
    W.reset()
    st = fresh_state(35892)
    W.gemini = {"kwh": 35890, "w": 400}
    for _ in range(600):
        ok, _ = step(st, 35891)
        assert not ok
    assert st["kwh"] == 35892


@test
def aufwaerts_physikdeckel():
    """+40 kWh in Minuten ist physikalisch unmoeglich -> Veto, selbst
    wenn alle Zeugen zustimmen. Nach 10 h 'Blindflug' ist +40 dagegen
    moeglich -> Ausfall-Heilung greift (zeitskalierter Deckel)."""
    W.reset()
    st = fresh_state(35891)
    W.gemini = {"kwh": 35931, "w": 400}
    W.seg_decide = lambda *c: ((35931, 5.0) if 35931 in c else (None, 0.0))
    for _ in range(1200):                               # 30 min druecken
        ok, _ = step(st, 35931)
        assert not ok, "Physik-Deckel durchbrochen"
    assert st["kwh"] == 35891
    # Jetzt: 10 h Ausfall simulieren -> Deckel waechst mit, Heilung ok
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
    """I5: 4 Lesungen in 10 s reichen NICHT — der Kandidat muss >= 3 min
    konsistent bleiben, erst dann wird der Zeuge ueberhaupt gefragt."""
    W.reset()
    st = fresh_state(35891, kwh_ts=FT.now - 10 * 3600)  # Heilung waere legitim
    W.gemini = {"kwh": 35931, "w": 400}
    t0 = FT.now
    accepted_at = None
    for _ in range(600):
        ok, _ = step(st, 35931, dt=2)
        if ok:
            accepted_at = FT.now
            break
    assert accepted_at is not None, "legitime Heilung kam nie durch"
    assert accepted_at - t0 >= mr.REBASE_MIN_SPAN_S, (
        f"zu schnell: nach {accepted_at - t0:.0f}s akzeptiert")
    assert W.gemini_calls >= 1


@test
def replay_26_07_kanaltrennung():
    """Replay 26.07.: W-Flattern (+6000 W) darf den kWh-Kanal nicht
    freischalten — 35881 -> 35861/35801 bleibt verboten."""
    W.reset()
    st = fresh_state(35881, w=400)
    W.gemini = {"kwh": 35801, "w": 9075}
    for kwh_bad, w_bad in ((35861, 9075), (35801, 3075), (35801, 9075)):
        for _ in range(800):
            ok, _ = step(st, kwh_bad, w=w_bad)
            assert not ok
    assert st["kwh"] == 35881


@test
def lcd_segmenttest_und_muell():
    """8er-Test, dunkles LCD, Null, Negatives: beruehrt den Stand nie."""
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
    """Der normale +1-Schritt verlangt zwei uebereinstimmende Lesungen."""
    W.reset()
    st = fresh_state(35891)
    ok, _ = step(st, 35892)
    assert ok and st["kwh"] == 35891      # akzeptiert, aber Stand haelt
    ok, _ = step(st, 35892)
    assert ok and st["kwh"] == 35892      # zweite Lesung -> uebernommen


@test
def basis_fenster_nach_standverlust():
    """Watchdog hat den Stand freigegeben (kwh=None, Boden 35890):
    die echte Basis (35891) braucht 2 Lesungen; Muell weit ueber dem
    Fenster (88888) kommt ohne Gemini-Doppel-Bestaetigung nie rein."""
    W.reset()
    st = {"kwh_floor": 35890, "kwh_floor_ts": FT.now}
    W.gemini = None                                     # Gemini tot
    ok, _ = step(st, 88888)
    assert not ok
    for _ in range(300):                                # Muell druecken
        ok, _ = step(st, 88888)
        assert not ok, "Basis-Muell ohne Zeugen akzeptiert"
    ok, _ = step(st, 35891)
    assert not ok, "Basis ohne zweite Lesung akzeptiert"
    ok, _ = step(st, 35891)
    assert ok and st["kwh"] == 35891 and "kwh_floor" not in st


@test
def basis_unter_boden_nur_mit_doppel_gemini():
    """Korrupt geheilter Boden liegt UEBER der Wahrheit (585870 -> 58587):
    zurueck zur echten 35891 geht nur ueber 4x/3min + ZWEI exakte
    Gemini-Bestaetigungen — und mit widersprechendem Gemini nie."""
    W.reset()
    st = {"kwh_floor": 58587, "kwh_floor_ts": FT.now}
    W.gemini = {"kwh": 35891, "w": 400}
    healed = False
    for _ in range(600):
        ok, _ = step(st, 35891)
        if ok:
            healed = True
            break
    assert healed and st["kwh"] == 35891
    assert W.gemini_calls >= 2, "Doppel-Bestaetigung wurde nicht verlangt"
    # Gegenprobe: Gemini widerspricht -> Boden haelt
    W.reset()
    st = {"kwh_floor": 58587, "kwh_floor_ts": FT.now}
    W.gemini = {"kwh": 35892, "w": 400}
    for _ in range(600):
        ok, _ = step(st, 35891)
        assert not ok
    assert st.get("kwh") is None


@test
def state_json_korruptur_heilung():
    """DIE Rettung fuer heute: state.json enthaelt 358914 -> Laden heilt
    zu kwh=None + Boden 35891, die Kamera setzt den Stand neu."""
    W.reset()
    _state_tmp.write_text(json.dumps({"kwh": 358914, "ts": FT.now - 3600}))
    st = mr.load_state()
    assert st.get("kwh") is None
    assert st.get("kwh_floor") == 35891
    ok, _ = step(st, 35891)
    assert not ok                                        # 1. Lesung: pend
    ok, _ = step(st, 35891)
    assert ok and st["kwh"] == 35891                     # wiederhergestellt
    # Normalfall bleibt normal:
    _state_tmp.write_text(json.dumps({"kwh": 35891, "ts": FT.now}))
    assert mr.load_state()["kwh"] == 35891
    # Muell-Datei -> leerer State, kein Crash:
    _state_tmp.write_text("kaputt{")
    assert mr.load_state() == {}
    _state_tmp.write_text(json.dumps({"kwh": None}))
    assert mr.load_state().get("kwh") is None


@test
def fuzz_adversarial_48h():
    """48 h simulierter Betrieb. Lokal liefert zufaellige Korruptions-
    klassen (Schatten 9->5, Geisterziffer vorn/hinten, Ziffer weg,
    verschobene Zeile, 8er-Test, Zufallsmuell), und Gemini + Segment-
    Dekoder sind MAXIMAL boesartig: sie bestaetigen jeden Kandidaten.
    I1-I3 werden bei jedem Schritt geprueft (in step()); am Ende darf
    der Stand nicht weiter als 3 kWh von der Wahrheit abweichen."""
    rng = random.Random(42)
    W.reset()
    truth = 35891.0
    st = fresh_state(35891)
    asked = {"kwh": None}

    def evil_gemini():
        # bestaetigt, was auch immer gerade Kandidat ist
        return {"kwh": asked["kwh"], "w": 400}

    def corrupt(k):
        r = rng.random()
        s = f"{k:06d}"
        if r < 0.30:                                    # Schatten: 9 -> 5
            return int(s.replace("9", "5"))
        if r < 0.45:                                    # Geisterziffer hinten
            return k * 10 + rng.randrange(10)
        if r < 0.55:                                    # Geisterziffer vorn
            return int(str(rng.randrange(1, 10)) + s)
        if r < 0.65:                                    # Ziffer verloren
            i = rng.randrange(6)
            return int(s[:i] + s[i + 1:])
        if r < 0.75:                                    # Zeile verrutscht
            return int(s[1:] + str(rng.randrange(10)))
        if r < 0.85:
            return 888888                               # Segmenttest
        return rng.randrange(1, 999999)                 # Zufallsmuell

    W.seg_decide = lambda *c: (c[0], 9.9)               # boesartig: immer ja
    W.gemini = evil_gemini
    steps = int(48 * 3600 / 10)
    for i in range(steps):
        truth += 10 / 1800.0                            # ~2 kWh/h
        good = int(truth)
        kwh = corrupt(good) if rng.random() < 0.25 else good
        asked["kwh"] = kwh
        step(st, kwh, dt=10)                            # prueft I1-I3
    drift = abs(st["kwh"] - int(truth))
    assert drift <= 3, f"Drift {drift} kWh (Stand {st['kwh']}, Wahrheit {int(truth)})"


@test
def ausfall_heilung_bleibt_moeglich():
    """3 Tage Addon aus, Zaehler real +90 kWh: die Heilung MUSS durchgehen
    (Boese-Fall-Absicherung darf den Gutfall nicht toeten)."""
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
        FT.now += 7200                                  # Tests entkoppeln
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
