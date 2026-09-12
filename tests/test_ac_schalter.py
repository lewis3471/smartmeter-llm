#!/usr/bin/env python3
"""Tests fuer den Steckdosen-Schalter (ac_guard.AcSwitch).  Lauf:
    .venv/bin/python tests/test_ac_schalter.py

Die Dose folgt dem Akku-Waechter: AUS, sobald er haelt; EIN, sobald er
freigibt — fruehestens OFF_MIN_S nach dem Aus. Was hier festgenagelt
wird, sind die Faelle, in denen ein simpler Schalter trotzdem falsch
liegen koennte:

  S1  Aus bei hold — und VORHER das Limit persistent auf Minimum.
  S2  Ein erst nach der Mindest-Aus-Zeit, nicht schon, wenn sich der
      Bus nach dem Abschalten um 1 V erholt hat.
  S3  Handeingriff EIN: schaltet jemand bei hold wieder ein, nimmt der
      Waechter das zurueck (die Dose gehoert ihm).
  S4  Dose nie lesbar (WLAN-Aussetzer der P100, 5x taeglich gesehen):
      nichts tun, nicht abstuerzen, nach 5 min einmal laut werden.
  S5  Kein Urteil (Waechter lief noch nie) -> kein Schalten. ac_start()
      reicht nur ein gesichertes HALTEN durch (S20).
  S6  Nie zwei Befehle in einer Minute; ein nicht reagierendes Geraet
      wird danach erneut geschaltet.
  S7  Persistieren scheitert -> trotzdem abschalten.
  S8  Aus-Zeitpunkt ueberlebt den Neustart; unplausible Anker nicht.
  S9  HTTP-Fehler zaehlen, Zustand bleibt ehrlich.
  S10 Integration: Dose aus -> control() regelt nicht, aber der Waechter
      sieht die DC-Spannung weiter und gibt frei; die Dose schaltet ein;
      solange der HMS das Netz beobachtet (producing=false) schweigt der
      Regler; danach startet er vom persistierten Minimum.
  S11 ac_start verweigert ohne batt_strings.
  S12 Der Failsafe-Pfad (Kamera tot) reicht das Urteil ebenfalls durch.
  S13 Eingefrorene DTU-Werte loesen im Regelzyklus nichts aus.
  S14 Aus-Befehl laeuft in den Timeout, Relais oeffnet trotzdem: die
      Sperre ist bewaffnet (Review-Fund).
  S15 Aussetzer der Dose loescht den bekannten Zustand nicht und sperrt
      Befehle, bis sie wieder lesbar ist (Review-Fund).
  S16 Handeingriff AUS startet die Sperre; danach folgt die Dose wieder
      dem Waechter (Review-Fund).
  S17 Erster Start mit vorgefundenem Aus ohne Anker: Sperre laeuft ab
      jetzt, kein sofortiges Einschalten (Review-Fund).
  S18 Failsafe: kein Limit an die stromlose Dose, sonst hoechstens alle
      2 s (Review-Fund).
  S20 ac_start reicht nur ein gesichertes HALTEN durch, kein "frei".
  S21 Solange der Dosenzustand nie gelesen wurde, regelt der Regler
      nicht — der erste Takt nach dem Start schickt kein Limit an einen
      moeglicherweise stromlosen HMS (Review-Fund, 2. Runde).
  S22 Ein wirkungsloser EIN-Befehl wird nach einer Minute wiederholt und
      nicht als Hand-Aus mit 30-min-Sperre gedeutet (Review-Fund, 2. Runde).
  S23 Das Minimum wird je Halte-Episode nur EINMAL persistiert, auch wenn
      ein Schaltkonflikt die Dose minuetlich wieder einschaltet
      (Review-Fund, 2. Runde).
  S24 Beim Wiederanlauf liest der Regler das echte Limit aus der DTU
      statt das persistierte Minimum anzunehmen (Review-Fund, 2. Runde).
  S25 Failsafe sendet nichts, solange der Dosenzustand unbekannt ist, und
      setzt limit_w nur bei echtem Senden (Review-Fund, 2. Runde).
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
os.environ["INVERTER_SERIAL"] = "1164a00ab8d4"
os.environ["BATT_STRINGS"] = "1,2,4"
os.environ["BATT_LOW_V"] = "50"
os.environ["BATT_HIGH_V"] = "54.4"
os.environ["AC_SWITCH_ENTITY"] = "switch.testdose"
_tmp = Path(tempfile.mkdtemp())
os.environ["STATE_FILE"] = str(_tmp / "state.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ac_guard  # noqa: E402
import meter_reader as mr  # noqa: E402

READ_LIMIT_ECHT = mr.read_limit_w     # bevor Tests es mocken
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'}   {name}"
          + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


class Uhr:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def wall(self):
        return self.t

    def mono(self):
        return self.t


class FakeHa:
    """HA-Ersatz: ein Zustand, ein Protokoll der Service-Aufrufe.
    reagiert=False simuliert eine Dose, die den Befehl schluckt;
    flip_trotz_fehler=True eine Dose, die schaltet, aber deren Antwort
    erst nach unserem Timeout kommt (code 0)."""

    def __init__(self, zustand="on", reagiert=True, code=200,
                 flip_trotz_fehler=False):
        self.zustand, self.reagiert, self.code = zustand, reagiert, code
        self.flip_trotz_fehler = flip_trotz_fehler
        self.calls = []

    def state(self, entity):
        return self.zustand

    def service(self, domain, service, daten):
        self.calls.append(service)
        if self.reagiert and (self.code == 200 or self.flip_trotz_fehler):
            self.zustand = "on" if service == "turn_on" else "off"
        return self.code


def bau(ha, uhr, persist=None, off_min_s=1800):
    logs = []
    sw = ac_guard.AcSwitch(ha, "switch.testdose", logs.append,
                           persist=persist, off_min_s=off_min_s,
                           wall=uhr.wall, mono=uhr.mono)
    return sw, logs


# --- S1 -------------------------------------------------------------------
def test_aus_bei_hold_mit_persistiertem_minimum():
    ha, uhr = FakeHa("on"), Uhr()
    reihenfolge = []
    sw, _ = bau(ha, uhr, persist=lambda: reihenfolge.append("persist"))
    ha.calls = reihenfolge          # gemeinsames Protokoll
    sw.want(True)
    sw.tick()
    check("dose_aus", ha.calls == ["persist", "turn_off"] and sw.on is False,
          f"calls={ha.calls} on={sw.on}")
    check("aus_zeitpunkt_gesetzt", sw.off_wall == uhr.t)
    check("snapshot_aus", sw.snapshot()["text"] == "aus", sw.snapshot())


# --- S2 -------------------------------------------------------------------
def test_ein_erst_nach_sperre():
    ha, uhr = FakeHa("off"), Uhr()
    sw, _ = bau(ha, uhr)
    sw.off_wall = uhr.t - 100        # vor 100 s abgeschaltet
    sw.want(False)                   # Waechter gibt frei (Bus erholt)
    sw.tick()
    check("innerhalb_sperre_kein_ein", ha.calls == [], ha.calls)
    check("snapshot_zeigt_restsperre",
          sw.snapshot()["text"].startswith("aus (frei in"), sw.snapshot())
    uhr.t += 1800
    sw.tick()
    check("nach_sperre_ein_befohlen", ha.calls == ["turn_on"] and sw.on is False,
          f"calls={ha.calls} on={sw.on}")
    uhr.t += 20
    sw.tick()                        # erst die Lesung macht EIN wahr
    check("ein_erst_nach_lesung", sw.on is True and ha.calls == ["turn_on"]
          and sw.off_wall is None, f"on={sw.on} off_wall={sw.off_wall}")


# --- S3 -------------------------------------------------------------------
def test_hand_ein_bei_hold_wird_zurueckgenommen():
    ha, uhr = FakeHa("on"), Uhr()
    sw, _ = bau(ha, uhr)
    sw.want(True)
    sw.tick()
    check("erst_aus", ha.calls == ["turn_off"])
    ha.zustand = "on"                # jemand schaltet von Hand ein
    uhr.t += 30
    sw.tick()
    check("keine_hektik_innerhalb_minute", ha.calls == ["turn_off"])
    uhr.t += 31
    sw.tick()                        # Poll faellig (20 s) + Minute vorbei
    check("hand_ein_zurueckgenommen", ha.calls == ["turn_off", "turn_off"],
          ha.calls)


# --- S4 -------------------------------------------------------------------
def test_unbekannt_tut_nichts_und_meldet_sich_einmal():
    ha, uhr = FakeHa("unavailable"), Uhr()
    sw, logs = bau(ha, uhr)
    sw.want(True)
    for _ in range(10):
        sw.tick()
        uhr.t += 20
    check("keine_befehle_bei_unavailable", ha.calls == [], ha.calls)
    check("zustand_unbekannt", sw.on is None
          and sw.snapshot()["text"] == "unbekannt")
    uhr.t += 300
    sw.tick()
    laut = [l for l in logs if "nicht lesbar" in l]
    check("nach_5min_einmal_laut", len(laut) == 1, logs)
    ha.zustand = "on"                # WLAN wieder da
    uhr.t += 20
    sw.tick()
    check("danach_sofort_aus", ha.calls == ["turn_off"], ha.calls)


# --- S5 -------------------------------------------------------------------
def test_ohne_urteil_kein_schalten():
    ha, uhr = FakeHa("on"), Uhr()
    sw, _ = bau(ha, uhr)
    sw.tick()
    # Gelesen wird vom ersten Takt an (der Regler braucht den Zustand),
    # geschaltet erst mit Urteil.
    check("kein_urteil_kein_befehl_aber_gelesen",
          ha.calls == [] and sw.on is True, f"calls={ha.calls} on={sw.on}")


# --- S6 -------------------------------------------------------------------
def test_ratenlimit_und_wiederholung():
    ha, uhr = FakeHa("on", reagiert=False), Uhr()
    sw, _ = bau(ha, uhr)
    sw.want(True)
    sw.tick()
    check("erster_befehl", ha.calls == ["turn_off"])
    for _ in range(5):
        uhr.t += 10
        sw.tick()
    check("keine_wiederholung_in_der_minute", ha.calls == ["turn_off"],
          ha.calls)
    uhr.t += 15                      # > 60 s seit dem Befehl, Poll faellig
    sw.tick()
    check("dose_reagierte_nicht_erneut_aus",
          ha.calls == ["turn_off", "turn_off"], ha.calls)


# --- S7 -------------------------------------------------------------------
def test_persist_fehler_schaltet_trotzdem_ab():
    def boom():
        raise OSError("DTU weg")
    ha, uhr = FakeHa("on"), Uhr()
    sw, logs = bau(ha, uhr, persist=boom)
    sw.want(True)
    sw.tick()
    check("trotz_persist_fehler_aus", ha.calls == ["turn_off"], ha.calls)
    check("fehler_protokolliert", any("persistieren" in l for l in logs))


# --- S8 -------------------------------------------------------------------
def test_aus_zeitpunkt_ueberlebt_neustart():
    ha, uhr = FakeHa("off"), Uhr()
    sw, _ = bau(ha, uhr)
    sw.off_wall = uhr.t - 600
    st = {}
    sw.store(st)
    neu, _ = bau(ha, uhr)
    neu.load(st)
    check("anker_uebernommen", neu.off_wall == uhr.t - 600)
    check("sperre_rechnet_weiter", 1100 < neu.sperre_rest_s() <= 1200,
          neu.sperre_rest_s())
    alt, _ = bau(ha, uhr)
    alt.load({"aco": uhr.t - 3 * 86400})
    check("uralter_anker_verworfen", alt.off_wall is None)
    zukunft, _ = bau(ha, uhr)
    zukunft.load({"aco": uhr.t + 3600})
    check("zukunfts_anker_verworfen", zukunft.off_wall is None)
    kaputt, _ = bau(ha, uhr)
    kaputt.load({"aco": True})
    check("bool_ist_kein_anker", kaputt.off_wall is None)


# --- S9 -------------------------------------------------------------------
def test_http_fehler():
    ha, uhr = FakeHa("on", code=500), Uhr()
    sw, logs = bau(ha, uhr)
    sw.want(True)
    sw.tick()
    check("fehler_gezaehlt", sw.errors == 1 and sw.on is True, sw.errors)
    check("fehler_geloggt", any("ohne Bestaetigung" in l for l in logs))


# --- S10: Integration mit dem Regelzyklus --------------------------------
class FakeTime:
    def __init__(self, uhr):
        import time as _t
        self._real, self._uhr = _t, uhr

    def time(self):
        return self._uhr.t

    def monotonic(self):
        return self._uhr.t

    def sleep(self, s):
        self._uhr.t += s

    def __getattr__(self, a):
        return getattr(self._real, a)


def _livedata(uhr, v, producing=False, ac_w=0.0):
    mr._livedata_meta = {"ts": uhr.t, "age_s": 1.0,
                         "reachable": True, "producing": producing,
                         "ac_w": ac_w}
    return (ac_w, {1: (v, 0.0), 2: (v + 0.1, 0.0),
                   3: (0.7, 1.7), 4: (v, 0.0)})


def test_dose_aus_regler_schweigt_waechter_sieht_weiter():
    uhr = Uhr()
    mr.time = FakeTime(uhr)
    ha = FakeHa("off")
    sw, _ = bau(ha, uhr)
    mr._ac = sw
    gesendet = []
    mr.set_limit = lambda w, persistent=False: gesendet.append((w, persistent))
    # Vorgeschichte: Waechter haelt, Dose ist seit einer Stunde aus, der
    # Regler hat noch Zwischenstaende von vorher.
    state = {"batt_hold": True, "limit_w": 612,
             "pv_hist": [(uhr.t - 100, 400.0)], "kick": {"step": 2}}
    sw.off_wall = uhr.t - 3600
    sw.want(True)
    sw.tick()
    check("ausgangslage_dose_aus", sw.on is False and ha.calls == [])

    mr.get_livedata = lambda: _livedata(uhr, 51.0)
    r = mr.control(300, state)
    check("regler_schweigt", r == (None, None) and gesendet == [],
          f"r={r} gesendet={gesendet}")
    check("spannung_kommt_trotzdem_an", state.get("batt_v") == 51.0,
          state.get("batt_v"))
    check("limit_auf_minimum_gesetzt", state.get("limit_w") == mr.MIN_LIMIT_W)
    check("zwischenstaende_verworfen",
          "kick" not in state and "pv_hist" not in state)
    check("hold_bleibt", state.get("batt_hold") is True and sw.hold is True)

    # Victron laedt: 52 V ueber die Freigabezeit gehalten
    mr.get_livedata = lambda: _livedata(uhr, 52.5)
    mr.control(300, state)
    uhr.t += mr.BATT_RELEASE_S + 1
    mr.control(300, state)
    check("waechter_gibt_frei_obwohl_dose_aus",
          state.get("batt_hold") is False and sw.hold is False,
          f"hold={state.get('batt_hold')} sw.hold={sw.hold}")
    sw.tick()
    check("dose_schaltet_ein", ha.calls == ["turn_on"], ha.calls)
    uhr.t += 20
    sw.tick()                        # Lesung bestaetigt EIN
    check("ein_bestaetigt", sw.on is True, sw.on)
    check("kein_limit_an_stillen_hms", gesendet == [], gesendet)
    mr.read_limit_w = lambda: mr.MIN_LIMIT_W   # DTU bestaetigt das Minimum

    # HMS beobachtet das Netz (producing=false): Regler schweigt weiter
    mr.get_livedata = lambda: _livedata(uhr, 52.5, producing=False)
    for _ in range(3):
        uhr.t += 0.5
        r = mr.control(300, state)
    check("netzbeobachtung_regler_schweigt",
          r == (None, None) and gesendet == [], f"r={r} gesendet={gesendet}")
    check("current_bleibt_minimum", state.get("limit_w") == mr.MIN_LIMIT_W)

    # HMS speist: Regler startet — vom persistierten Minimum, nicht von
    # einem Phantom-612 (Pending-Delta = neu - current beweist das).
    mr.get_livedata = lambda: _livedata(uhr, 52.5, producing=True,
                                        ac_w=35.0)
    uhr.t += 0.5
    r = mr.control(300, state)
    pend = state.get("pending") or []
    check("regler_startet_vom_minimum",
          r[0] is not None and gesendet and pend
          and pend[-1][1] == r[0] - mr.MIN_LIMIT_W,
          f"r={r} gesendet={gesendet} pending={pend}")


# --- S11 ------------------------------------------------------------------
def test_ac_start_ohne_strings_verweigert():
    alt_strings, alt_ac = mr.BATT_STRINGS, mr._ac
    mr.BATT_STRINGS, mr._ac = [], None
    try:
        mr.ac_start({"batt_hold": True})
        check("ohne_strings_kein_schalter", mr._ac is None)
    finally:
        mr.BATT_STRINGS, mr._ac = alt_strings, alt_ac


# --- S12 ------------------------------------------------------------------
def test_failsafe_reicht_urteil_durch():
    uhr = Uhr()
    mr.time = FakeTime(uhr)
    ha = FakeHa("on")
    sw, _ = bau(ha, uhr)
    mr._ac = sw
    leer = (46.0, 300.0)
    mr._livedata_meta = {"ts": uhr.t, "age_s": 0.0,
                         "reachable": True, "producing": True}
    mr.get_livedata = lambda: (300.0, {1: leer, 2: leer, 4: leer})
    state = {}
    mr.guarded_limit(state, 1999)
    uhr.t += mr.BATT_TRIP_S + 1
    fs = mr.guarded_limit(state, 1999)
    check("failsafe_am_minimum", fs == mr.MIN_LIMIT_W, fs)
    check("urteil_beim_schalter", sw.hold is True)
    sw.tick()
    check("dose_aus_im_failsafe", ha.calls == ["turn_off"], ha.calls)


# --- S13 ------------------------------------------------------------------
def test_eingefrorene_dtu_werte_loesen_nichts_aus():
    uhr = Uhr()
    mr.time = FakeTime(uhr)
    ha = FakeHa("on")
    sw, _ = bau(ha, uhr)
    mr._ac = sw
    mr.set_limit = lambda w, persistent=False: None
    leer = (41.5, 0.0)
    # Inverter nicht erreichbar (BMS hat abgeschaltet), DTU haelt Altwerte
    mr._livedata_meta = {"ts": uhr.t, "age_s": 900.0,
                         "reachable": False, "producing": False, "ac_w": 0.0}
    mr.get_livedata = lambda: (0.0, {1: leer, 2: leer, 4: leer})
    state = {"batt_hold": False}
    mr.control(300, state)
    uhr.t += mr.BATT_TRIP_S + 1
    mr.control(300, state)
    check("altwert_loest_nicht_aus", state.get("batt_hold") is False,
          state.get("batt_hold"))
    check("kein_phantom_batt_v", "batt_v" not in state, state.get("batt_v"))
    # ... und umgekehrt gibt ein eingefrorener Hochwert nicht frei
    state = {"batt_hold": True}
    voll = (52.5, 0.0)
    mr.get_livedata = lambda: (0.0, {1: voll, 2: voll, 4: voll})
    mr.control(300, state)
    uhr.t += mr.BATT_RELEASE_S + 1
    mr.control(300, state)
    check("altwert_gibt_nicht_frei", state.get("batt_hold") is True)


# --- S14 ------------------------------------------------------------------
def test_timeout_aus_bewaffnet_sperre():
    ha, uhr = FakeHa("on", code=0, flip_trotz_fehler=True), Uhr()
    sw, _ = bau(ha, uhr)
    sw.want(True)
    sw.tick()
    check("befehl_raus_antwort_fehlt",
          ha.calls == ["turn_off"] and sw.on is True and sw.errors == 1)
    check("sperre_ab_befehl", sw.off_wall == uhr.t, sw.off_wall)
    uhr.t += 20
    sw.tick()                        # Poll liest jetzt "off"
    check("lesung_korrigiert_zustand", sw.on is False)
    sw.want(False)                   # Bus erholt sich -> Waechter frei
    uhr.t += 13 * 60
    sw.tick()
    check("kein_sofortiges_ein", ha.calls == ["turn_off"], ha.calls)
    check("snapshot_zeigt_sperre",
          sw.snapshot()["text"].startswith("aus (frei in"), sw.snapshot())
    uhr.t += 20 * 60
    sw.tick()
    check("nach_sperre_ein", ha.calls == ["turn_off", "turn_on"], ha.calls)


# --- S15 ------------------------------------------------------------------
def test_aussetzer_loescht_zustand_nicht():
    ha, uhr = FakeHa("off"), Uhr()
    sw, logs = bau(ha, uhr)
    sw.want(True)
    sw.tick()
    check("bekannt_aus", sw.on is False and ha.calls == [])
    ha.zustand = "unavailable"       # WLAN-Aussetzer
    uhr.t += 20
    sw.tick()
    check("zustand_bleibt_aus", sw.on is False, sw.on)
    check("snapshot_markiert", "nicht lesbar" in sw.snapshot()["text"],
          sw.snapshot())
    sw.want(False)                   # Waechter gibt frei
    sw.off_wall = uhr.t - 3600       # Sperre laengst vorbei
    uhr.t += 20
    sw.tick()
    check("kein_befehl_solange_unlesbar", ha.calls == [], ha.calls)
    ha.zustand = "off"               # wieder lesbar
    uhr.t += 20
    sw.tick()
    check("danach_ein", ha.calls == ["turn_on"], ha.calls)


# --- S16 ------------------------------------------------------------------
def test_hand_aus_startet_sperre():
    ha, uhr = FakeHa("on"), Uhr()
    sw, _ = bau(ha, uhr)
    sw.want(False)
    sw.tick()
    check("ein_und_ruhig", sw.on is True and ha.calls == [])
    ha.zustand = "off"               # Papa zieht die Dose in HA
    uhr.t += 20
    sw.tick()
    check("hand_aus_erkannt", sw.on is False and ha.calls == [])
    check("sperre_laeuft", 1700 < sw.sperre_rest_s() <= 1800,
          sw.sperre_rest_s())
    uhr.t += 600
    sw.tick()
    check("innerhalb_sperre_kein_ein", ha.calls == [], ha.calls)
    uhr.t += 1200
    sw.tick()
    check("danach_folgt_dose_dem_waechter", ha.calls == ["turn_on"], ha.calls)


# --- S17 ------------------------------------------------------------------
def test_erster_start_mit_vorgefundenem_aus():
    ha, uhr = FakeHa("off"), Uhr()
    sw, _ = bau(ha, uhr)             # kein aco in der state.json
    sw.want(False)
    sw.tick()
    check("kein_sofortiges_ein", ha.calls == [], ha.calls)
    check("anker_gesetzt", sw.off_wall == uhr.t, sw.off_wall)
    uhr.t += 1800
    sw.tick()
    check("nach_sperre_ein", ha.calls == ["turn_on"], ha.calls)
    # Mit gespeichertem Anker bleibt der gespeicherte massgeblich
    ha2, uhr2 = FakeHa("off"), Uhr()
    sw2, _ = bau(ha2, uhr2)
    sw2.load({"aco": uhr2.t - 1750})
    sw2.want(False)
    sw2.tick()
    check("gespeicherter_anker_bleibt", sw2.off_wall == uhr2.t - 1750
          and ha2.calls == [])
    uhr2.t += 60
    sw2.tick()
    check("gespeicherte_sperre_laeuft_ab", ha2.calls == ["turn_on"], ha2.calls)


# --- S18 ------------------------------------------------------------------
def test_failsafe_kein_limit_an_stromlose_dose():
    uhr = Uhr()
    mr.time = FakeTime(uhr)
    ha = FakeHa("off")
    sw, _ = bau(ha, uhr)
    mr._ac = sw
    gesendet = []
    mr.set_limit = lambda w, persistent=False: gesendet.append(w)
    sw.want(True)
    sw.tick()
    mr.get_livedata = lambda: _livedata(uhr, 47.0)
    state = {"batt_hold": True}
    fs = mr.failsafe_limit(state)
    check("dose_aus_kein_send", fs == mr.MIN_LIMIT_W and gesendet == []
          and state.get("limit_w") == mr.MIN_LIMIT_W, f"fs={fs} {gesendet}")
    # Dose an: senden, aber nicht im 0,5-s-Takt
    sw.on = True
    mr.get_livedata = lambda: _livedata(uhr, 52.0, producing=True, ac_w=40.0)
    state = {"batt_hold": False}
    mr.failsafe_limit(state)
    uhr.t += 0.5
    mr.failsafe_limit(state)
    uhr.t += 0.5
    mr.failsafe_limit(state)
    check("dose_an_einmal_gesendet", gesendet == [mr.FAILSAFE_LIMIT_W],
          gesendet)
    uhr.t += 2.0
    mr.failsafe_limit(state)
    check("nach_2s_erneut", gesendet == [mr.FAILSAFE_LIMIT_W] * 2, gesendet)


# --- S20 ------------------------------------------------------------------
def test_start_reicht_nur_halten_durch():
    alt_start, alt_ha, alt_ac = ac_guard.start, ac_guard.Ha, mr._ac
    ac_guard.start = lambda sw, log: None            # kein Thread im Test
    ac_guard.Ha = lambda: FakeHa("off")
    try:
        mr._ac = None
        mr.ac_start({"batt_hold": False})
        check("kein_hold_kein_urteil", mr._ac is not None and mr._ac.hold is None,
              getattr(mr._ac, "hold", "?"))
        mr._ac = None
        # ac_start() baut den Schalter mit der ECHTEN Uhr (ac_guard.time),
        # nicht mit der gefaelschten aus den Integrationstests oben —
        # der Anker muss also zur echten Uhr passen.
        echt = ac_guard.time.time()
        mr.ac_start({"batt_hold": True, "aco": echt - 100})
        check("hold_wird_durchgereicht", mr._ac.hold is True)
        check("anker_geladen", mr._ac.off_wall == echt - 100,
              f"{mr._ac.off_wall} vs {echt - 100}")
    finally:
        ac_guard.start, ac_guard.Ha, mr._ac = alt_start, alt_ha, alt_ac


# --- S21 ------------------------------------------------------------------
def test_unbekannter_dosenzustand_regelt_nicht():
    uhr = Uhr()
    mr.time = FakeTime(uhr)
    ha = FakeHa("on")
    sw, _ = bau(ha, uhr)             # noch nie gepollt: on is None
    mr._ac = sw
    gesendet = []
    mr.set_limit = lambda w, persistent=False: gesendet.append(w)
    mr.read_limit_w = lambda: 300    # DTU bestaetigt den angenommenen Wert
    mr.get_livedata = lambda: _livedata(uhr, 52.5, producing=True, ac_w=35.0)
    state = {"batt_hold": False, "limit_w": 300}
    r = mr.control(300, state)
    check("unbekannt_kein_limit", r == (None, None) and gesendet == [],
          f"r={r} gesendet={gesendet}")
    check("limit_w_unangetastet", state.get("limit_w") == 300)
    sw.tick()                        # erster Poll: Dose ist an
    uhr.t += 0.5
    r = mr.control(300, state)
    check("nach_lesung_regelt_er", r[0] is not None and gesendet,
          f"r={r} gesendet={gesendet}")


# --- S22 ------------------------------------------------------------------
def test_wirkungsloses_ein_wird_wiederholt():
    ha, uhr = FakeHa("off", reagiert=False), Uhr()
    sw, _ = bau(ha, uhr)
    sw.off_wall = uhr.t - 3600       # Sperre laengst vorbei
    sw.want(False)
    sw.tick()
    check("ein_befohlen", ha.calls == ["turn_on"] and sw.on is False)
    uhr.t += 20
    sw.tick()                        # Lesung: immer noch aus
    check("kein_hand_aus_gedeutet", sw.off_wall == uhr.t - 3620,
          sw.off_wall)
    uhr.t += 41                      # > 60 s seit dem Befehl
    sw.tick()
    check("nach_minute_erneut_ein", ha.calls == ["turn_on", "turn_on"],
          ha.calls)


# --- S23 ------------------------------------------------------------------
def test_persist_nur_einmal_je_episode():
    ha, uhr = FakeHa("on", reagiert=False), Uhr()   # Dose kommt immer wieder
    persists = []
    sw, _ = bau(ha, uhr, persist=lambda: persists.append(uhr.t))
    sw.want(True)
    for _ in range(4):
        sw.tick()
        uhr.t += 65
    check("aus_wird_wiederholt", ha.calls == ["turn_off"] * 4, ha.calls)
    check("flash_nur_einmal", len(persists) == 1, persists)
    sw.want(False)                   # Episode zu Ende ...
    sw.want(True)                    # ... naechste beginnt
    sw.tick()
    check("neue_episode_neuer_persist", len(persists) == 2, persists)
    # Innerhalb derselben Episode nach 10 min noch einmal: die DTU
    # bestaetigt nur das Einreihen, ein per Funk verlorener Persist
    # darf nicht fuer immer als erledigt gelten.
    for _ in range(9):
        uhr.t += 65
        sw.tick()
    check("kein_persist_in_den_ersten_10min", len(persists) == 2, persists)
    uhr.t += 65
    sw.tick()
    check("nach_10min_wiederholt", len(persists) == 3, persists)


# --- S24 ------------------------------------------------------------------
def test_wiederanlauf_liest_echtes_limit():
    uhr = Uhr()
    mr.time = FakeTime(uhr)
    ha = FakeHa("on")
    sw, _ = bau(ha, uhr)
    mr._ac = sw
    sw.want(False)
    sw.tick()                        # on True
    gesendet = []
    mr.set_limit = lambda w, persistent=False: gesendet.append(w)
    mr.read_limit_w = lambda: 488    # DTU: der HMS faehrt den Flash-Wert
    state = {"batt_hold": False, "limit_w": mr.MIN_LIMIT_W}
    mr.get_livedata = lambda: _livedata(uhr, 52.5, producing=False)
    r = mr.control(300, state)
    check("netzbeobachtung_still", r == (None, None) and gesendet == [])
    mr.get_livedata = lambda: _livedata(uhr, 52.5, producing=True, ac_w=470.0)
    uhr.t += 0.5
    r = mr.control(300, state)
    pend = state.get("pending") or []
    check("ausgangswert_aus_dtu",
          r[0] is not None and pend and pend[-1][1] == r[0] - 488,
          f"r={r} pending={pend} gesendet={gesendet}")
    check("resume_flag_verbraucht", "ac_resume" not in state)


# --- S25 ------------------------------------------------------------------
def test_failsafe_unbekannt_sendet_nicht():
    uhr = Uhr()
    mr.time = FakeTime(uhr)
    ha = FakeHa("on")
    sw, _ = bau(ha, uhr)             # nie gepollt
    mr._ac = sw
    gesendet = []
    mr.set_limit = lambda w, persistent=False: gesendet.append(w)
    mr.get_livedata = lambda: _livedata(uhr, 52.5, producing=True, ac_w=40.0)
    state = {"batt_hold": False, "limit_w": 300}
    fs = mr.failsafe_limit(state)
    check("unbekannt_nichts_gesendet", gesendet == [] and fs == 300
          and state.get("limit_w") == 300, f"fs={fs} {gesendet}")
    sw.tick()                        # jetzt bekannt: an
    fs = mr.failsafe_limit(state)
    check("bekannt_an_gesendet", gesendet == [mr.FAILSAFE_LIMIT_W]
          and fs == mr.FAILSAFE_LIMIT_W, f"fs={fs} {gesendet}")
    uhr.t += 0.5
    state["limit_w"] = 999           # Marker: darf ohne Senden nicht kippen
    fs = mr.failsafe_limit(state)
    check("gedrosselt_limit_w_bleibt", gesendet == [mr.FAILSAFE_LIMIT_W]
          and state.get("limit_w") == 999, f"{gesendet} {state.get('limit_w')}")
    # Unbekannte Dose, aber der Waechter haelt: dann muss wenigstens das
    # Minimum raus (beide Schutzebenen duerfen nie gleichzeitig schweigen).
    sw2, _ = bau(FakeHa("unavailable"), uhr)
    mr._ac = sw2
    gesendet.clear()
    leer = {"batt_hold": True, "limit_w": 300}
    mr.get_livedata = lambda: _livedata(uhr, 46.0, producing=True, ac_w=400.0)
    uhr.t += 2.5
    fs = mr.failsafe_limit(leer)
    check("unbekannt_mit_hold_minimum", gesendet == [mr.MIN_LIMIT_W]
          and fs == mr.MIN_LIMIT_W, f"fs={fs} {gesendet}")


# --- S26 ------------------------------------------------------------------
def test_unlesbare_dose_mit_hold_sendet_minimum():
    """Beide Schutzebenen duerfen nie gleichzeitig schweigen: Dose seit
    dem Start unlesbar, Akku leer -> wenigstens das Minimum an den HMS."""
    uhr = Uhr()
    mr.time = FakeTime(uhr)
    ha = FakeHa("unavailable")
    sw, _ = bau(ha, uhr)
    mr._ac = sw
    gesendet = []
    mr.set_limit = lambda w, persistent=False: gesendet.append(w)
    mr.read_limit_w = lambda: None
    mr.get_livedata = lambda: _livedata(uhr, 46.0, producing=True, ac_w=780.0)
    state = {"batt_hold": False, "limit_w": 800}
    for _ in range(40):              # 20 s: Waechter loest nach 15 s aus
        sw.tick()
        mr.control(300, state)
        uhr.t += 0.5
    check("waechter_hat_ausgeloest", state.get("batt_hold") is True)
    check("minimum_trotz_unlesbarer_dose",
          gesendet and set(gesendet) == {mr.MIN_LIMIT_W}, gesendet)
    check("ratenlimit_2s", len(gesendet) <= 3, gesendet)
    check("keine_schaltbefehle", ha.calls == [], ha.calls)


# --- S27 ------------------------------------------------------------------
def test_wiederanlauf_wiederholt_dtu_abfrage():
    uhr = Uhr()
    mr.time = FakeTime(uhr)
    ha = FakeHa("on")
    sw, _ = bau(ha, uhr)
    mr._ac = sw
    sw.want(False)
    sw.tick()
    gesendet = []
    mr.set_limit = lambda w, persistent=False: gesendet.append(w)
    antworten = [None, None, 488]
    mr.read_limit_w = lambda: antworten.pop(0) if antworten else 488
    state = {"batt_hold": False, "limit_w": mr.MIN_LIMIT_W}
    mr.get_livedata = lambda: _livedata(uhr, 52.5, producing=False)
    mr.control(300, state)
    mr.get_livedata = lambda: _livedata(uhr, 52.5, producing=True, ac_w=470.0)
    r1 = mr.control(300, state)
    r2 = mr.control(300, state)
    check("zwei_fehlversuche_still", r1 == (None, None) and r2 == (None, None)
          and gesendet == [], f"{r1} {r2} {gesendet}")
    r3 = mr.control(300, state)
    pend = state.get("pending") or []
    check("dritter_versuch_liefert_488",
          r3[0] is not None and pend and pend[-1][1] == r3[0] - 488,
          f"r3={r3} pending={pend}")
    # Dauerhaft keine Antwort: nach 3 Versuchen wird geregelt (Annahme)
    mr.read_limit_w = lambda: None
    state = {"batt_hold": False, "limit_w": mr.MIN_LIMIT_W}
    mr.get_livedata = lambda: _livedata(uhr, 52.5, producing=False)
    mr.control(300, state)
    mr.get_livedata = lambda: _livedata(uhr, 52.5, producing=True, ac_w=40.0)
    rs = [mr.control(300, state) for _ in range(3)]
    check("nach_drei_fehlversuchen_geregelt",
          rs[0] == (None, None) and rs[1] == (None, None)
          and rs[2][0] is not None and "ac_resume" not in state, rs)


# --- S28 ------------------------------------------------------------------
def test_read_limit_w_parst_dtu():
    class R:
        def __init__(self, d): self._d = d
        def raise_for_status(self): pass
        def json(self): return self._d
    alt = mr.requests.get
    lese = READ_LIMIT_ECHT           # die echte Funktion, nicht ein Mock
    try:
        mr.requests.get = lambda *a, **k: R({"1164a00ab8d4": {
            "limit_relative": 24.4, "max_power": 2000, "limit_set_status": "Ok"}})
        check("24_4_prozent_sind_488", lese() == 488, lese())
        mr.requests.get = lambda *a, **k: R({"1164a00ab8d4": {
            "limit_relative": 0.0, "max_power": 2000, "limit_set_status": "Unknown"}})
        check("null_prozent_ist_kein_limit", lese() is None)
        mr.requests.get = lambda *a, **k: R({"1164A00AB8D4": {
            "limit_relative": 2.5, "max_power": 2000}})
        check("serial_gross_klein_egal", lese() == 50, lese())
        mr.requests.get = lambda *a, **k: R({"1164a00ab8d4": {
            "limit_relative": 50, "max_power": 0}})
        check("max_power_0_ist_kein_limit", lese() is None)
        def boom(*a, **k): raise OSError("timeout")
        mr.requests.get = boom
        check("timeout_ist_none", lese() is None)
    finally:
        mr.requests.get = alt


# --- S29 ------------------------------------------------------------------
def test_funkstille_regler_schweigt():
    """BMS hat abgeschaltet: DTU friert producing=true samt AC-Wert ein.
    Der Regler darf nicht auf Altwerten in die Funkstille regeln."""
    uhr = Uhr()
    mr.time = FakeTime(uhr)
    ha = FakeHa("on")
    sw, _ = bau(ha, uhr)
    mr._ac = sw
    sw.want(False)
    sw.tick()
    gesendet = []
    mr.set_limit = lambda w, persistent=False: gesendet.append(w)
    mr.read_limit_w = lambda: 488
    state = {"batt_hold": False, "limit_w": 300, "batt_v": 41.5,
             "pv_hist": [(uhr.t, 400.0)]}
    def eingefroren():
        mr._livedata_meta = {"ts": uhr.t, "age_s": 900.0, "reachable": False,
                             "producing": True, "ac_w": 430.0}
        return (430.0, {1: (41.5, 0.0), 2: (41.5, 0.0), 4: (41.5, 0.0)})
    mr.get_livedata = eingefroren
    r = mr.control(300, state)
    check("funkstille_kein_limit", r == (None, None) and gesendet == [],
          f"r={r} {gesendet}")
    check("kein_phantom_batt_v", "batt_v" not in state and "pv_hist" not in state)
    # HMS bootet aus dem Flash (488 W): Wiederanlauf liest das echte Limit
    mr.get_livedata = lambda: _livedata(uhr, 52.0, producing=True, ac_w=470.0)
    r = mr.control(300, state)
    pend = state.get("pending") or []
    check("nach_funkstille_echtes_limit",
          r[0] is not None and pend and pend[-1][1] == r[0] - 488,
          f"r={r} pending={pend}")


for fn in (test_aus_bei_hold_mit_persistiertem_minimum,
           test_ein_erst_nach_sperre,
           test_hand_ein_bei_hold_wird_zurueckgenommen,
           test_unbekannt_tut_nichts_und_meldet_sich_einmal,
           test_ohne_urteil_kein_schalten,
           test_ratenlimit_und_wiederholung,
           test_persist_fehler_schaltet_trotzdem_ab,
           test_aus_zeitpunkt_ueberlebt_neustart,
           test_http_fehler,
           test_dose_aus_regler_schweigt_waechter_sieht_weiter,
           test_ac_start_ohne_strings_verweigert,
           test_failsafe_reicht_urteil_durch,
           test_eingefrorene_dtu_werte_loesen_nichts_aus,
           test_timeout_aus_bewaffnet_sperre,
           test_aussetzer_loescht_zustand_nicht,
           test_hand_aus_startet_sperre,
           test_erster_start_mit_vorgefundenem_aus,
           test_failsafe_kein_limit_an_stromlose_dose,
           test_start_reicht_nur_halten_durch,
           test_unbekannter_dosenzustand_regelt_nicht,
           test_wirkungsloses_ein_wird_wiederholt,
           test_persist_nur_einmal_je_episode,
           test_wiederanlauf_liest_echtes_limit,
           test_failsafe_unbekannt_sendet_nicht,
           test_unlesbare_dose_mit_hold_sendet_minimum,
           test_wiederanlauf_wiederholt_dtu_abfrage,
           test_read_limit_w_parst_dtu,
           test_funkstille_regler_schweigt):
    print(f"\n{fn.__name__}:")
    fn()

print()
if FAILS:
    print(f"{len(FAILS)} FEHLGESCHLAGEN: {', '.join(FAILS)}")
    sys.exit(1)
print("Alle Tests gruen")
