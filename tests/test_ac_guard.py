#!/usr/bin/env python3
"""Tests fuer den AC-seitigen Tiefentladeschutz.  Lauf:
    .venv/bin/python tests/test_ac_guard.py

Alles gefaelscht: Uhr, Home Assistant, BMS. Der Automat darf ohne Netz,
ohne Broker und ohne DTU vollstaendig durchgespielt werden — sonst
koennte man ihn nur an der echten Anlage testen, und das ist genau der
Akku, den er schuetzen soll.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("AC_SWITCH_ENTITY", "switch.wechselrichter")
os.environ.setdefault("AC_DEADMAN_NUMBER_ENTITY", "number.wr_auto_off_minutes")
os.environ.setdefault("AC_DEADMAN_SWITCH_ENTITY", "switch.wr_auto_off_enabled")
os.environ.setdefault("AC_DEADMAN_AT_ENTITY", "sensor.wr_auto_off_at")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ac_guard as ag

ag.OPENDTU_URL = ""          # keine HTTP-Gegenprobe im Test
ag.CAPACITY_AH = 100.0        # 100 Ah Pack, damit E4 rechenbar ist

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'}   {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


import time as _time
MITTAG = _time.mktime((2026, 7, 1, 12, 0, 0, 0, 0, -1))


class Uhr:
    def __init__(self):
        self.w = MITTAG              # Wanduhr: Sommer, 12 Uhr
        self.m = 10_000.0            # monoton
    def wall(self):
        return self.w
    def mono(self):
        return self.m
    def vor(self, s):
        self.w += s
        self.m += s


class FakeHa:
    def __init__(self, an=True):
        self.zustand = "on" if an else "off"
        self.rufe = []
        self.code = 200
        self.auto_off_at = "2026-09-06T12:00:00+00:00"
        self.notifications = []
    def state(self, entity):
        if self.code != 200:
            return None, {}, self.code
        if entity and entity.endswith("auto_off_at"):
            return self.auto_off_at, {}, 200
        return self.zustand, {"friendly_name": "Wechselrichter"}, 200
    def service(self, domain, service, daten):
        self.rufe.append((domain, service, daten.get("entity_id")))
        if domain == "switch" and daten.get("entity_id") == ag.SWITCH_ENTITY:
            self.zustand = "on" if service == "turn_on" else "off"
        return 200
    def notify(self, t, m):
        self.notifications.append((t, m))


class FakeBms:
    def __init__(self, **kw):
        self.d = {"frisch": True, "cell_min": 3280.0, "cell_diff": 30.0,
                  "soc": 60.0, "voltage": 52.5, "current": -5.0,
                  "data_age": 3.0, "cs": "Bulk", "ppv": 400.0}
        self.d.update(kw)
    def snapshot(self):
        return dict(self.d)
    def unbekannte_topics(self):
        return ["battery/stateOfCharge"]


def bau(ha=None, bms=None, uhr=None, dtu=None, leistung=200.0, st=None):
    uhr = uhr or Uhr()
    ha = ha or FakeHa()
    bms = bms or FakeBms()
    # Die DTU erreicht den Wechselrichter genau dann, wenn seine Dose an
    # ist — alles andere waere gerade der Widerspruchsfall.
    dtu_fn = ((lambda: dtu) if dtu is not None
              else (lambda: {"reachable": ha.zustand == "on", "age_s": 2.0}))
    gebremst = []
    g = ag.AcGuard(ha, bms, lambda: gebremst.append(uhr.m), lambda: None,
                   lambda m: None, dtu_meta=dtu_fn,
                   dtu_power=lambda: leistung, min_limit_w=50,
                   wall=uhr.wall, mono=uhr.mono)
    g.load(st or {})
    return g, ha, bms, uhr, gebremst


def takte(g, uhr, sekunden, schritt=1.0):
    """1 Hz wie im Echtbetrieb.

    WICHTIG: hier darf NICHT `g._ha_poll` zurueckgesetzt werden. Genau das
    tat eine fruehere Fassung — und verdeckte damit den schlimmsten Fehler
    des Automaten: der Ist-Zustand der Dose wird nur alle POLL_S (10 s)
    abgefragt, und der Anlauf las deshalb den VERALTETEN Wert von vor dem
    eigenen Einschaltbefehl. Ein Test, der die Zeitverhaeltnisse der
    Wirklichkeit wegnimmt, prueft nichts."""
    for _ in range(int(sekunden / schritt)):
        uhr.vor(schritt)
        g.tick()


# --- 1. Der Weg in die Abschaltung --------------------------------------
def test_leerer_akku_schaltet_ab():
    g, ha, bms, uhr, gebremst = bau()
    takte(g, uhr, 3)
    check("start_geht_nach_normal", g.state == "normal", g.state)
    check("gate_frei_bei_vollem_akku", g.gate()["gate"] == "frei")

    bms.d["cell_min"] = 3100.0                     # unter Drossel-Schwelle
    takte(g, uhr, 25)
    check("drosselt_nach_entprellung", g.state == "drossel", g.state)
    check("gate_deckelt_auf_minimum",
          g.gate()["gate"] == "cap" and g.gate()["cap"] == 50)

    bms.d.update(cell_min=3040.0, cs="Off", ppv=0.0)   # leer, keine Sonne
    n = len(gebremst)
    takte(g, uhr, 25)
    check("bremse_laeuft_in_drossel", len(gebremst) - n >= 20,
          str(len(gebremst) - n))
    check("fordert_abschaltung_an", g.state in ("aus_angefordert", "ac_aus"),
          g.state)
    bms.d["current"] = -0.2                        # Inverter ist tot
    takte(g, uhr, 30)
    check("erreicht_ac_aus", g.state == "ac_aus", g.state)
    check("dose_ist_wirklich_aus", ha.zustand == "off")
    check("gate_stumm_wenn_aus", g.gate()["gate"] == "stumm")
    check("schaltzaehler_gestiegen", g.switches_today == 1, str(g.switches_today))
    check("aus_grund_steht_im_klartext", "mV" in g.reason, g.reason)


def test_notaus_ohne_absetzzeit():
    """Zellalarm des BMS: sofort, ohne auf Absetzen zu warten."""
    g, ha, bms, uhr, _ = bau(leistung=1400.0)
    takte(g, uhr, 3)
    bms.d["cell_min"] = 2850.0
    takte(g, uhr, 4)
    check("notaus_schaltet_sofort", ha.zustand == "off", ha.zustand)


def test_alarmtopic_schaltet_ab():
    g, ha, bms, uhr, _ = bau()
    takte(g, uhr, 3)
    bms.d["alarm_uv"] = 1
    takte(g, uhr, 3)
    check("bms_alarm_schaltet_ab", ha.zustand == "off", ha.zustand)


# --- 2. Blindflug ist kein Freibrief ------------------------------------
def test_blindflug_fail_closed():
    g, ha, bms, uhr, _ = bau()
    takte(g, uhr, 3)
    bms.d.update(frisch=False, soc=30.0)
    takte(g, uhr, 130)
    check("kurzer_blindflug_drosselt", g.state == "drossel", g.state)
    check("dose_bleibt_zunaechst_an", ha.zustand == "on")
    takte(g, uhr, 300)
    check("langer_blindflug_bei_wenig_soc_schaltet_ab",
          ha.zustand == "off", f"{g.state}/{ha.zustand}")


def test_blindflug_voller_akku_haelt_laenger_durch():
    g, ha, bms, uhr, _ = bau(bms=FakeBms(soc=85.0))
    takte(g, uhr, 3)
    bms.d["frisch"] = False
    takte(g, uhr, 400)
    check("voller_akku_wird_nicht_sofort_geopfert", ha.zustand == "on",
          f"{g.state}/{ha.zustand}")
    takte(g, uhr, 600)
    check("aber_irgendwann_doch", ha.zustand == "off", g.state)


# --- 3. Freigabe braucht positiven Nachweis -----------------------------
def test_keine_freigabe_ohne_nachweis():
    g, ha, bms, uhr, _ = bau(ha=FakeHa(an=False),
                             st={"acs": "ac_aus", "acr": "Zelle 3040 mV",
                                 "acofs": 1_800_000_000.0 - 10_000,
                                 "acsoc": 12.0})
    takte(g, uhr, 5)
    check("startet_in_beobachtung_oder_aus",
          g.state in ("ac_aus", "freigabe_beobachtung"), g.state)
    bms.d.update(frisch=False, cell_min=None, soc=None)
    takte(g, uhr, 20)
    check("ohne_daten_keine_freigabe", ha.zustand == "off", g.state)

    # Daten da, aber Akku noch leer
    bms.d.update(frisch=True, cell_min=3150.0, soc=30.0, cs="Bulk", ppv=500.0,
                 current=10.0, data_age=2.0)
    takte(g, uhr, 20)
    check("leerer_akku_keine_freigabe", ha.zustand == "off", g.state)
    check("block_nennt_den_grund", "Freigabe fehlt" in g.block, g.block)


def test_freigabe_nach_ladung():
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=False), uhr=uhr,
                           dtu={"reachable": False, "age_s": 999},
                           st={"acs": "ac_aus", "acr": "Zelle leer",
                               "acofs": uhr.w - 10_000, "acsoc": 15.0,
                               "acah": 60.0, "acd": "x"})
    bms.d.update(cell_min=3330.0, soc=55.0, current=1.0, cs="Absorption",
                 ppv=600.0, frisch=True, data_age=2.0)
    takte(g, uhr, 5)
    takte(g, uhr, 150, schritt=1.0)     # Ruhe-Sample reifen lassen
    check("schaltet_nach_ladung_wieder_ein", ha.zustand == "on",
          f"{g.state} / {g.block}")
    check("laeuft_ueber_anlauf", g.state in ("anlauf", "normal"), g.state)
    check("anlauf_deckelt_das_limit",
          g.gate()["gate"] in ("cap", "frei"), str(g.gate()))


def test_automatik_aus_schaltet_nie_ein_aber_immer_ab():
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=False), uhr=uhr,
                           st={"acs": "ac_aus", "aca": False,
                               "acofs": uhr.w - 10_000})
    bms.d.update(cell_min=3350.0, soc=90.0, cs="Float", current=0.5)
    takte(g, uhr, 200)
    check("automatik_aus_bleibt_aus", ha.zustand == "off", g.state)
    check("meldet_das_auch", "Automatik" in g.block, g.block)


# --- 4. "Aus" ist eine Behauptung ---------------------------------------
def test_widerspruch_klebendes_relais():
    uhr = Uhr()
    g, ha, bms, _, gebremst = bau(ha=FakeHa(an=False), uhr=uhr,
                                  dtu={"reachable": True, "age_s": 2.0},
                                  st={"acs": "ac_aus", "acofs": uhr.w - 5000})
    bms.d.update(current=-12.0, cell_min=3200.0, soc=40.0)
    takte(g, uhr, 90)
    check("erkennt_widerspruch", g.state == "ac_aus_unbestaetigt", g.state)
    check("schlaegt_alarm", g.fault is True)
    n = len(gebremst)
    takte(g, uhr, 10)
    check("bremst_dabei_weiter", len(gebremst) > n)


# --- 5. Der Mensch hat Vorrang ------------------------------------------
def test_handschaltung_wird_respektiert():
    g, ha, bms, uhr, _ = bau()
    takte(g, uhr, 3)
    ha.zustand = "off"                       # jemand zieht in der App ab
    takte(g, uhr, 15)
    check("fremdes_aus_wird_erkannt", g.state == "manuell_aus", g.state)
    check("automat_schaltet_nicht_zurueck", ha.zustand == "off")
    ha.zustand = "on"                        # und wieder an
    takte(g, uhr, 15)
    check("fremdes_ein_fuehrt_in_anlauf", g.state in ("anlauf", "normal"),
          g.state)


def test_hand_freigabe_ueberspringt_bedingungen():
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=False), uhr=uhr,
                           st={"acs": "ac_aus", "acofs": uhr.w - 100})
    bms.d.update(cell_min=3200.0, soc=30.0, cs="Bulk", ppv=300.0)
    takte(g, uhr, 10)
    check("ohne_override_bleibt_aus", ha.zustand == "off")
    g.on_command("ac_freigabe_min", "30")
    takte(g, uhr, 10)
    check("hand_freigabe_schaltet_ein", ha.zustand == "on", g.state)


# --- 6. Totmann ---------------------------------------------------------
def test_totmann_wird_nachgetriggert_und_geprueft():
    g, ha, bms, uhr, _ = bau()
    takte(g, uhr, 3)
    check("totmann_bestaetigt", g.deadman == "ok", g.deadman)
    gesetzt = [r for r in ha.rufe if r[1] == "set_value"]
    check("frist_wurde_gesetzt", len(gesetzt) >= 1)
    vorher = len(gesetzt)
    takte(g, uhr, 400)
    check("frist_wird_nachgetriggert",
          len([r for r in ha.rufe if r[1] == "set_value"]) > vorher)


def test_ohne_totmann_entitaeten_kein_falsches_ok():
    alt = ag.DEADMAN_NUMBER
    ag.DEADMAN_NUMBER = ""
    try:
        g, ha, bms, uhr, _ = bau()
        takte(g, uhr, 5)
        check("meldet_fehlenden_totmann", g.deadman == "fehlt", g.deadman)
    finally:
        ag.DEADMAN_NUMBER = alt


# --- 7. Neustart --------------------------------------------------------
def test_neustart_dose_an_aber_akku_leer():
    """Der gefaehrlichste Neustart: state.json sagt 'aus', die Dose ist an."""
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=True), uhr=uhr,
                           st={"acs": "ac_aus", "acr": "Zelle leer",
                               "acofs": uhr.w - 600})
    bms.d.update(cell_min=3020.0, soc=10.0)
    takte(g, uhr, 20)
    check("schaltet_wieder_ab", ha.zustand == "off", g.state)


def test_neustart_dose_an_akku_voll_ist_handschaltung():
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=True), uhr=uhr,
                           st={"acs": "ac_aus", "acofs": uhr.w - 600})
    bms.d.update(cell_min=3300.0, soc=70.0)
    takte(g, uhr, 10)
    check("erkennt_handschaltung", g.state == "manuell_ein", g.state)
    check("laesst_sie_laufen", ha.zustand == "on")


def test_kaltstart_ohne_state_geht_nie_nach_manuell_aus():
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=False), uhr=uhr, st={})
    takte(g, uhr, 10)
    check("kaltstart_beobachtet_statt_zu_blockieren",
          g.state == "freigabe_beobachtung", g.state)


def test_zustand_wird_persistiert():
    g, ha, bms, uhr, _ = bau()
    takte(g, uhr, 3)
    st = {}
    g.store(st, bye=True)
    check("persistiert_zustand", st["acs"] == "normal", str(st.get("acs")))
    check("persistiert_sauberes_ende", st["acx"] is True)
    g2, _, _, uhr2, _ = bau(st=st)
    check("laedt_zustand", g2._geladen == "normal", str(g2._geladen))


# --- 8. Uhren -----------------------------------------------------------
def test_zeitsprung_verlaengert_nur():
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=False), uhr=uhr,
                           st={"acs": "ac_aus", "acofs": uhr.w - 100})
    vor = g.off_ts
    uhr.w += 3600                     # NTP springt vorwaerts
    g.tick()
    check("sprung_zieht_anker_mit", g.off_ts > vor, f"{vor} -> {g.off_ts}")
    uhr.w -= 7200                     # und zurueck
    g.tick()
    check("rueckwaertssprung_setzt_auf_jetzt",
          abs(g.off_ts - uhr.w) < 2, f"{g.off_ts} vs {uhr.w}")


def test_alte_uhren_werden_verworfen():
    uhr = Uhr()
    g, _, _, _, _ = bau(uhr=uhr, st={"acs": "ac_aus",
                                     "acofs": uhr.w - 5 * 24 * 3600,
                                     "acmu": uhr.w - 10})
    check("uralter_aus_zeitpunkt_verworfen", g.off_ts is None, str(g.off_ts))
    check("abgelaufenes_handfenster_verworfen", g.manual_until is None)


# --- 9. Schaltbudget ----------------------------------------------------
def test_tagesbudget_bremst_flattern():
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=False), uhr=uhr,
                           st={"acs": "ac_aus", "acofs": uhr.w - 10_000,
                               "acn": ag.MAX_SWITCH_PER_DAY, "acah": 99.0,
                               "acd": _time.strftime("%Y-%m-%d", _time.localtime(MITTAG))})
    bms.d.update(cell_min=3400.0, soc=90.0, cs="Float", current=1.0)
    takte(g, uhr, 200)
    check("budget_verhindert_einschalten", ha.zustand == "off", g.state)
    check("und_sagt_warum", "Tagesbudget" in g.block, g.block)


# --- 10. Regressionen aus der adversarialen Pruefung --------------------
def test_einschalten_gewinnt_kein_rennen():
    """DER schlimmste gefundene Fehler: der Ist-Zustand der Dose wird nur
    alle 10 s abgefragt. Der Anlauf las deshalb das veraltete "aus" von
    VOR dem eigenen Einschaltbefehl, hielt das fuer eine Handschaltung und
    ging nach manuell_aus — mit eingeschalteter Dose und stummem Regler.
    In 9 von 10 Phasenlagen. Wird hier ueber alle Phasenlagen geprueft."""
    schlecht = []
    for phase in range(10):
        uhr = Uhr()
        g, ha, bms, _, _ = bau(ha=FakeHa(an=False), uhr=uhr,
                               st={"acs": "ac_aus", "acofs": uhr.w - 10_000,
                                   "acsoc": 15.0, "acah": 99.0})
        bms.d.update(cell_min=3340.0, soc=60.0, current=1.0,
                     cs="Absorption", ppv=600.0)
        takte(g, uhr, phase)              # Phasenlage des Polls verschieben
        takte(g, uhr, 400)
        if (g.state not in ("anlauf", "normal") or ha.zustand != "on"
                or g.gate()["gate"] == "stumm"):
            schlecht.append((phase, g.state, ha.zustand, g.gate()["gate"]))
    check("kein_rennen_beim_einschalten", not schlecht, str(schlecht))


def test_hand_freigabe_schaltet_keinen_leeren_akku_zu():
    """Die Hand-Freigabe darf Ertragsbedingungen ueberspringen, nie die
    Schutzgrenzen — sonst taktet ein leerer Akku im Minutentakt."""
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=False), uhr=uhr,
                           st={"acs": "ac_aus", "acofs": uhr.w - 100})
    bms.d.update(cell_min=2960.0, soc=8.0, cs="Off", ppv=0.0, current=-0.1)
    g.on_command("ac_freigabe_min", "240")
    takte(g, uhr, 120)
    check("leerer_akku_bleibt_aus_trotz_hand", ha.zustand == "off",
          f"{g.state}/{g.block}")
    check("und_sagt_dass_der_akku_leer_ist", "leer" in g.block, g.block)


def test_hand_freigabe_respektiert_schaltbudget():
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=False), uhr=uhr,
                           st={"acs": "ac_aus", "acofs": uhr.w - 100,
                               "acn": ag.MAX_SWITCH_PER_DAY,
                               "acd": _time.strftime("%Y-%m-%d",
                                                     _time.localtime(MITTAG))})
    bms.d.update(cell_min=3300.0, soc=70.0)
    g.on_command("ac_freigabe_min", "60")
    takte(g, uhr, 60)
    check("hand_hebelt_das_tagesbudget_nicht_aus", ha.zustand == "off",
          f"{g.state}/{g.block}")


def test_abschaltgrund_beendet_die_hand_freigabe():
    g, ha, bms, uhr, _ = bau()
    takte(g, uhr, 3)
    g.on_command("ac_freigabe_min", "240")
    bms.d.update(cell_min=3040.0, cs="Off", ppv=0.0)
    takte(g, uhr, 60)
    check("abschaltung_loescht_die_hand_freigabe", g._freigabe_bis is None,
          str(g._freigabe_bis))


def test_gegenprobe_faelscht_keine_frische():
    """Die HTTP-Gegenprobe an die Fusion sagt nur, dass die Fusion lebt —
    sie liefert keine Zellspannung. Sie darf Zeit kaufen, nie 'frisch'
    behaupten."""
    g, ha, bms, uhr, _ = bau()
    takte(g, uhr, 3)
    bms.d.update(cell_min=None, soc=None)      # Herzschlag ohne Werte
    takte(g, uhr, 130)
    s = g.gate()
    check("ohne_werte_keine_frische", s["blind_s"] > 100, str(s["blind_s"]))
    check("und_der_automat_drosselt", g.state == "drossel", g.state)


def test_soc_sprung_blockiert_die_abschaltung_nicht():
    """Ein SoC-Sprung macht den SoC unglaubwuerdig. Das darf die FREIGABE
    bremsen — niemals die Abschaltung."""
    g, ha, bms, uhr, _ = bau()
    takte(g, uhr, 3)
    bms.d["soc"] = 90.0
    takte(g, uhr, 2)
    bms.d["soc"] = 10.0            # Sprung: soc_valid wird False
    takte(g, uhr, 2)
    check("soc_gilt_als_unglaubwuerdig", g.soc_valid is False)
    bms.d.update(cs="Off", ppv=0.0, current=-20.0)
    takte(g, uhr, 60)
    check("niedriger_soc_schaltet_trotzdem_ab", ha.zustand == "off",
          f"{g.state}/{ha.zustand}")


def test_manuell_aus_erkennt_klebendes_relais():
    g, ha, bms, uhr, _ = bau()
    takte(g, uhr, 3)
    ha.zustand = "off"                     # jemand schaltet in der App ab
    takte(g, uhr, 15)
    check("erst_manuell_aus", g.state == "manuell_aus", g.state)
    bms.d["current"] = -15.0                # ... der Akku wird trotzdem leer
    takte(g, uhr, 90)
    check("widerspruch_wird_auch_hier_erkannt",
          g.state == "ac_aus_unbestaetigt", g.state)
    check("mit_alarm", g.fault is True)


def test_ohne_victron_topics_kommt_er_trotzdem_hoch():
    """Der Schutz darf nicht daran haengen, dass die Victron-Topics
    ueberhaupt konfiguriert sind — sonst bleibt der Wechselrichter fuer
    immer aus."""
    uhr = Uhr()
    g, ha, bms, _, _ = bau(ha=FakeHa(an=False), uhr=uhr,
                           st={"acs": "ac_aus", "acofs": uhr.w - 10_000,
                               "acsoc": 15.0, "acah": 99.0})
    bms.d.update(cell_min=3340.0, soc=60.0, current=2.0)
    bms.d.pop("cs", None)
    bms.d.pop("ppv", None)
    takte(g, uhr, 400)
    check("ladestrom_allein_genuegt", ha.zustand == "on",
          f"{g.state} / {g.block}")


def test_ha_ausfall_flattert_nicht():
    g, ha, bms, uhr, _ = bau()
    takte(g, uhr, 3)
    ha.code = 500                       # Home Assistant antwortet nicht mehr
    wechsel = []
    for _ in range(300):
        vor = g.state
        takte(g, uhr, 1)
        if g.state != vor:
            wechsel.append((vor, g.state))
    check("hoechstens_wenige_zustandswechsel", len(wechsel) <= 3,
          f"{len(wechsel)}: {wechsel[:6]}")
    check("landet_in_unbekannt_oder_getrennt",
          g.state in ("unbekannt", "getrennt", "stoerung", "drossel"), g.state)


for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    print(f"\n{fn.__name__}:")
    fn()

print()
if FAILS:
    print(f"{len(FAILS)} FEHLGESCHLAGEN: {', '.join(FAILS)}")
    sys.exit(1)
print("Alle AC-Tests gruen")
