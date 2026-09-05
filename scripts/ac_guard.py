#!/usr/bin/env python3
"""AC-seitiger Tiefentladeschutz — Zustandsautomat.

WARUM ES DAS GIBT: Der HMS-2000-4T laesst sich per Limit nicht stilllegen.
Das kleinste ansteuerbare Limit ist 50 W, und unter ~500 W folgt er einem
Limitbefehl nur unzuverlaessig (gemessen an 929 Kommandos: 250-300 W zu
25 %, 350-400 W zu 67 %) — er faellt stattdessen in einen Attraktor bei
~157 W und speist die Nacht durch. Am Solarstrang war das egal, am
AKKU-Bus ist es der Weg in die Tiefentladung: am 28.08. lief der Pack bis
zur BMS-Abschaltung leer, waehrend der Waechter brav sein 50-W-Limit
sendete.

Ein Limit ist also keine Abschaltung. Eine Steckdose ist eine.

DIE DREI TRAGENDEN IDEEN:

1. TOTMANN STATT BEFEHL. Die Dose bekommt ihre geraeteseitige Auto-Off-
   Frist gesetzt (Tapo: `auto_off_minutes`) und wird periodisch
   nachgetriggert. Faellt WLAN, Broker, HA oder dieser Prozess aus,
   schaltet das Relais VON SELBST ab. Der Schutz haengt damit nicht an
   der Zustellung eines Befehls im Moment der Not — die Ruhelage ist AUS.

2. FREIGABE BRAUCHT POSITIVEN NACHWEIS. Abschalten darf jede Quelle,
   einschalten nur ein frischer, plausibler, nicht-retained Messwert.
   Fehlende Daten sind nie ein Grund einzuschalten und ab einer Dauer ein
   Grund abzuschalten.

3. "AUS" IST EINE BEHAUPTUNG, KEIN MESSWERT. Meldet das BMS Entladestrom
   oder die DTU einen erreichbaren Inverter, waehrend wir "aus" glauben,
   ist der Zustand gelogen (klebendes Relais, einpolig N getrennt, falsche
   Dose, umgesteckte Verlaengerung) — dann wird gebremst und Alarm
   geschlagen, statt sich auf den eigenen Zustand zu verlassen.

QUELLE DER SCHUTZGROESSE: die ZELLSPANNUNG aus dem JK-BMS (ueber
OpenDTU-on-Battery). Die Packspannung taugt nicht: die LFP-Kurve ist im
Betriebsbereich flach, der Victron zieht den Bus beim Laden sofort hoch
(volle Spannung bei leerem Akku), und das BMS schuetzt auf die
SCHWAECHSTE ZELLE — bei 200 mV Drift steht die bei 2,6 V, waehrend der
Pack noch 48 V zeigt. Genau daran ist der 47-V-Schutz gescheitert.

Das Modul ist absichtlich ohne Import von meter_reader: alles, was es
von dort braucht (bremsen, speichern, publizieren), kommt als Callback.
Damit ist der Automat ohne Netz, ohne MQTT und ohne DTU testbar.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

# --- Schwellen (Zelle in mV, SoC in %, Zeiten in s, Leistung in W) -------
# Die Zahlen stammen aus der LFP-Entladekurve und dem JK-Datenblatt:
# 3120 mV ~ 25 % (drosseln), 3050 mV ~ 5 % (abschalten), 2900 mV liegt
# 300 mV ueber dem JK-Werks-UVP von 2600 mV — Reserve fuer den ohmschen
# Abfall unter Last (bei 40 A und ~0,7 mOhm/Zelle rund 30 mV) und fuer die
# Steilheit der Kurve an dieser Stelle.
def _f(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def _i(name, default):
    return int(_f(name, default))


THROTTLE_CELL_MV = _i("AC_THROTTLE_CELL_MV", 3120)
THROTTLE_SOC = _i("AC_THROTTLE_SOC", 25)
OFF_CELL_MV = _i("AC_OFF_CELL_MV", 3050)
OFF_SOC = _i("AC_OFF_SOC", 18)
HARD_CELL_MV = _i("AC_HARD_CELL_MV", 2900)
ON_CELL_MV = _i("AC_ON_CELL_MV", 3300)
ON_SOC = _i("AC_ON_SOC", 50)
ON_DSOC = _i("AC_ON_DSOC", 25)
ON_AH = _f("AC_ON_AH", 0)                  # 0 = aus Kapazitaet ableiten
CAPACITY_AH = _f("BATT_CAPACITY_AH", 0)
CELL_DIFF_MAX_MV = _i("AC_CELL_DIFF_MAX_MV", 0)     # 0 = Veto aus
ON_DIFF_MAX_MV = _i("AC_ON_DIFF_MAX_MV", 0)         # 0 = Veto aus

OFF_TRIP_S = _f("AC_OFF_TRIP_S", 20)
THROTTLE_BUDGET_S = _f("AC_THROTTLE_BUDGET_S", 600)
THROTTLE_WINDOW_S = _f("AC_THROTTLE_WINDOW_S", 1800)
OFF_MIN_S = _f("AC_OFF_MIN_S", 2700)
SWITCH_COOLDOWN_S = _f("AC_SWITCH_COOLDOWN_S", 300)
STALE_S = _f("AC_STALE_S", 120)
BLIND_OFF_S = _f("AC_BLIND_OFF_S", 900)
BLIND_OFF_LOW_S = _f("AC_BLIND_OFF_LOW_S", 300)
BLIND_THROTTLE_S = _f("AC_BLIND_THROTTLE_S", 120)
SETTLE_S = _f("AC_SETTLE_S", 20)
VERIFY_S = _f("AC_VERIFY_S", 25)
OFF_CONFIRM_S = _f("AC_OFF_CONFIRM_S", 120)
OFF_VERIFY_W = _f("AC_OFF_VERIFY_W", 5)
WIDERSPRUCH_W = _f("AC_WIDERSPRUCH_W", 25)
WIDERSPRUCH_A = _f("AC_WIDERSPRUCH_A", 2.0)
WIDERSPRUCH_S = _f("AC_WIDERSPRUCH_S", 60)
START_BLIND_S = _f("AC_START_BLIND_S", 180)
# Der Anlauf-Timeout MUSS groesser sein als das Anlauf-Fenster — sonst
# meldet ein zulaessig konfiguriertes langes Fenster garantiert "Inverter
# kommt nicht hoch" und sperrt einen gesunden Wechselrichter.
START_TIMEOUT_S = max(_f("AC_START_TIMEOUT_S", 600), START_BLIND_S + 180)
START_LIMIT_W = _i("AC_START_LIMIT_W", 430)
MAX_SWITCH_LOAD_W = _f("AC_MAX_SWITCH_LOAD_W", 2500)
MAX_SWITCH_PER_DAY = _i("AC_MAX_SWITCH_PER_DAY", 6)
POLL_S = _f("AC_POLL_S", 10)
UNAVAIL_S = _f("AC_UNAVAIL_S", 60)
MANUAL_ON_MAX_S = _f("AC_MANUAL_ON_MAX_S", 3600)
MANUAL_HARD_S = _f("AC_MANUAL_HARD_S", 60)
FAULT_CLEAR_S = _f("AC_FAULT_CLEAR_S", 600)
DEADMAN_S = _f("AC_DEADMAN_S", 900)
# Nachtriggern muss deutlich haeufiger passieren als die Frist ablaeuft,
# sonst schaltet der Totmann im NORMALBETRIEB ab.
KEEPALIVE_S = min(_f("AC_KEEPALIVE_S", 300), DEADMAN_S / 3)
MANUAL_OFF_MAX_S = _f("AC_MANUAL_OFF_MAX_S", 86400)
CHARGE_PPV_W = _f("AC_CHARGE_PPV_W", 150)
ON_EARLIEST_H = _i("AC_ON_EARLIEST_H", 9)
ON_LATEST_H = _i("AC_ON_LATEST_H", 16)
REST_I_A = _f("AC_REST_I_A", 2.0)
REST_S = _f("AC_REST_S", 120)
REST_MAX_AGE_S = _f("AC_REST_MAX_AGE_S", 5400)
BMS_MAX_AGE_S = _f("AC_BMS_MAX_AGE_S", 20)      # dataAge-Tor
ARRIVAL_MAX_S = _f("AC_ARRIVAL_MAX_S", 30)      # Ankunfts-Tor
FLOOR_SOC = _i("AC_FLOOR_SOC", 60)
SLEEP_MAX_S = _f("AC_SLEEP_MAX_S", 900)

SWITCH_ENTITY = os.environ.get("AC_SWITCH_ENTITY", "").strip()
POWER_ENTITY = os.environ.get("AC_POWER_ENTITY", "").strip()
DEADMAN_SWITCH = os.environ.get("AC_DEADMAN_SWITCH_ENTITY", "").strip()
DEADMAN_NUMBER = os.environ.get("AC_DEADMAN_NUMBER_ENTITY", "").strip()
DEADMAN_AT = os.environ.get("AC_DEADMAN_AT_ENTITY", "").strip()
AUTOMATIK_DEFAULT = os.environ.get("AC_AUTOMATIK", "true").lower() not in (
    "false", "0", "no", "off")

# MQTT-Topics der OpenDTU-on-Battery. Die JK-spezifischen Namen sind in
# deren Doku ausdruecklich als unvollstaendig markiert — deshalb
# konfigurierbar, und beim Start wird protokolliert, was WIRKLICH ankommt.
BATT_PREFIX = os.environ.get("BATT_MQTT_PREFIX", "solar/")
T_CELLMIN = os.environ.get("BATT_TOPIC_CELLMIN", "battery/CellMinMilliVolt")
T_CELLDIFF = os.environ.get("BATT_TOPIC_CELLDIFF", "battery/CellDiffMilliVolt")
T_ALARM_UV = os.environ.get("BATT_TOPIC_ALARM_UV", "battery/alarms/underVoltage")
T_ONLINE = os.environ.get("BATT_TOPIC_ONLINE", "battery/status/BatteryOnline")
T_DISCHARGE_OK = os.environ.get("BATT_TOPIC_DISCHARGE_EN",
                                "battery/charging/dischargeEnabled")
T_SOC = os.environ.get("BATT_TOPIC_SOC", "battery/stateOfCharge")
T_VOLT = os.environ.get("BATT_TOPIC_VOLT", "battery/voltage")
T_CURRENT = os.environ.get("BATT_TOPIC_CURRENT", "battery/current")
T_DATAAGE = os.environ.get("BATT_TOPIC_DATAAGE", "battery/dataAge")
# Vorzeichen von battery/current beim LADEN. +1 = laden ist positiv.
CURRENT_SIGN = 1.0 if _f("BATT_CURRENT_CHARGE_POSITIVE", 1) >= 0 else -1.0

OPENDTU_URL = os.environ.get("OPENDTU_URL", "").rstrip("/")
SUPERVISOR = "http://supervisor/core/api"

# Plausibilitaetsband: ein Wert ausserhalb zaehlt wie "fehlt", NIE wie ein
# Alarm. Eine 0 aus einem Fehlframe der Kette JK -> RS485 -> OpenDTU ->
# MQTT erfuellt sonst jede Unterspannungsbedingung und oeffnet das Relais
# unter voller Last.
BAND = {"cell_min": (2000.0, 3800.0), "cell_diff": (0.0, 800.0),
        "soc": (0.0, 100.0), "voltage": (20.0, 70.0),
        "current": (-300.0, 300.0), "data_age": (0.0, 86400.0)}

ZUSTAENDE = ("init", "unbekannt", "normal", "drossel", "aus_angefordert",
             "ac_aus", "ac_aus_unbestaetigt", "freigabe_beobachtung",
             "ein_angefordert", "anlauf", "manuell_ein", "manuell_aus",
             "getrennt", "stoerung")


def enabled() -> bool:
    """Leeres ac_switch_entity = Feature aus, Verhalten wie bisher."""
    return bool(SWITCH_ENTITY)


# --- BMS-Daten ----------------------------------------------------------
class Bms:
    """Sammelt die BMS-/Victron-Werte aus MQTT und beurteilt ihre FRISCHE.

    Vier Tore, alle noetig (das dritte ist das wichtigste):
      a) retain: eine retained Nachricht kam wegen eines neuen Abos, nicht
         weil gerade gemessen wurde. Fuer die FREIGABE nie gueltig.
      b) Ankunft: eigener monotoner Stempel je Topic, juenger als 30 s.
      c) dataAge: der vom BMS gemeldete Alter-Wert selbst <= 20 s. Ohne
         dieses Tor laeuft bei totem RS485 der eingefrorene SoC alle 5 s
         als frische Nachricht weiter ein — "kam gerade an" ist kein
         Frischebeweis.
      d) Plausibilitaet (BAND).
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._v: dict = {}          # topic-suffix -> (wert, mono, retain)
        self.gesehen: set = set()   # alle je empfangenen battery/-Topics
        self._neu_geloggt = False

    def on_message(self, topic: str, payload: str, retain: bool = False):
        """Aus dem MQTT-Thread. Muss billig und exception-frei sein."""
        if not topic.startswith(BATT_PREFIX):
            return
        suf = topic[len(BATT_PREFIX):]
        try:
            val: object = float(payload)
        except (TypeError, ValueError):
            val = (payload or "").strip()
        with self._lock:
            if suf.startswith("battery/"):
                self.gesehen.add(suf)
            self._v[suf] = (val, self._clock(), bool(retain))

    def _get(self, suf, max_arrival=None, erlaube_retain=False):
        with self._lock:
            e = self._v.get(suf)
        if not e:
            return None
        val, ts, retain = e
        if retain and not erlaube_retain:
            return None
        if max_arrival is not None and self._clock() - ts > max_arrival:
            return None
        return val

    def _zahl(self, suf, band_key, **kw):
        v = self._get(suf, **kw)
        if not isinstance(v, (int, float)):
            return None
        lo, hi = BAND[band_key]
        return float(v) if lo <= v <= hi else None      # ausserhalb = fehlt

    def snapshot(self) -> dict:
        """Alles, was der Automat braucht — plus ein ehrliches `frisch`."""
        age = self._zahl(T_DATAAGE, "data_age", max_arrival=ARRIVAL_MAX_S)
        # Ohne dataAge-Topic behelfen wir uns mit dem Herzschlag der
        # Messwerte selbst (Ankunfts-Tor allein).
        herz = self._get(T_VOLT, max_arrival=ARRIVAL_MAX_S)
        if herz is None:
            herz = self._get(T_CURRENT, max_arrival=ARRIVAL_MAX_S)
        frisch = ((age is not None and age <= BMS_MAX_AGE_S)
                  or (age is None and herz is not None))
        s = {
            "frisch": bool(frisch),
            "data_age": age,
            "cell_min": self._zahl(T_CELLMIN, "cell_min",
                                   max_arrival=ARRIVAL_MAX_S),
            "cell_diff": self._zahl(T_CELLDIFF, "cell_diff",
                                    max_arrival=ARRIVAL_MAX_S),
            "soc": self._zahl(T_SOC, "soc", max_arrival=ARRIVAL_MAX_S),
            "voltage": self._zahl(T_VOLT, "voltage",
                                  max_arrival=ARRIVAL_MAX_S),
            "current": self._zahl(T_CURRENT, "current",
                                  max_arrival=ARRIVAL_MAX_S),
            # Zustandstopics duerfen retained gelten, SOLANGE der
            # Herzschlag frisch ist — sie aendern sich selten, und
            # dataAge sagt ohnehin, wie alt die Daten wirklich sind.
            "alarm_uv": self._get(T_ALARM_UV, erlaube_retain=True),
            "online": self._get(T_ONLINE, erlaube_retain=True),
            "discharge_ok": self._get(T_DISCHARGE_OK, erlaube_retain=True),
        }
        # Victron: Ladezustand und PV-Leistung, Seriennummer unbekannt ->
        # nimm irgendeinen passenden Suffix.
        with self._lock:
            items = list(self._v.items())
        for suf, (val, ts, retain) in items:
            if suf.startswith("victron/") and suf.endswith("/CS"):
                s["cs"] = val
            elif suf.startswith("victron/") and suf.endswith("/PPV"):
                if isinstance(val, (int, float)):
                    s["ppv"] = float(val)
        return s

    def unbekannte_topics(self) -> list:
        """Alles, was unter battery/ ankommt — damit der Nutzer die ECHTEN
        Namen sieht, statt dass wir raten. Ein Schutz auf geratenen
        Topicnamen ist kein Schutz."""
        with self._lock:
            return sorted(self.gesehen)


# --- Home-Assistant-API -------------------------------------------------
class Ha:
    """Duenner Supervisor-Proxy-Client. Braucht `homeassistant_api: true`
    im Add-on-Manifest; SUPERVISOR_TOKEN steht im Container bereits."""

    def __init__(self, token=None, base=SUPERVISOR, opener=None):
        self.token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self.base = base
        self._opener = opener or urllib.request.urlopen

    def _req(self, pfad, daten=None, timeout=8):
        url = f"{self.base}{pfad}"
        body = json.dumps(daten).encode() if daten is not None else None
        r = urllib.request.Request(url, data=body, method="POST" if body else "GET")
        r.add_header("Authorization", f"Bearer {self.token}")
        r.add_header("Content-Type", "application/json")
        try:
            with self._opener(r, timeout=timeout) as resp:
                return resp.getcode(), json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception as e:                      # Timeout, DNS, Refused
            return 0, str(e)

    def state(self, entity):
        """(state, attrs, code). code 0 = nicht erreichbar, 404 = weg."""
        if not entity:
            return None, {}, 404
        code, data = self._req(f"/states/{entity}", timeout=5)
        if code == 200 and isinstance(data, dict):
            return data.get("state"), data.get("attributes") or {}, 200
        return None, {}, code

    def service(self, domain, service, daten):
        code, _ = self._req(f"/services/{domain}/{service}", daten)
        return code

    def notify(self, titel, text):
        self.service("persistent_notification", "create",
                     {"title": titel, "message": text,
                      "notification_id": "smartmeter_ac"})


# --- Der Automat --------------------------------------------------------
class AcGuard:
    """Zustandsautomat. Laeuft im eigenen Thread mit ~1 Hz.

    Der Regelzyklus (0,5 s) liest NUR den Snapshot aus gate() — ein
    haengender HTTP-Aufruf an HA oder an die Dose darf ihn nie aufhalten.

    Alle laufenden Dauern rechnen auf einer MONOTONEN Uhr; nur die
    neustartuebergreifenden Anker (Aus-Zeitpunkt, Anlauf, Hand-Freigabe)
    stehen als Wanduhr in der state.json — und die werden beim Laden auf
    0 <= Alter <= 48 h geprueft, sonst gilt "jetzt" (das verlaengert die
    Sperre, also die sichere Richtung)."""

    def __init__(self, ha: Ha, bms: Bms, brake, save, log,
                 dtu_meta=lambda: {}, dtu_power=lambda: None,
                 persist=None, min_limit_w=50,
                 wall=time.time, mono=time.monotonic):
        self.ha, self.bms = ha, bms
        self._brake, self._save, self._log = brake, save, log
        self._persist = persist or (lambda w: None)
        self._dtu_meta, self._dtu_power = dtu_meta, dtu_power
        self.min_limit_w = min_limit_w
        self._wall, self._mono = wall, mono

        self.state = "init"
        self.reason = ""
        self.block = ""
        self.fault = False
        self.fault_reason = ""
        self.automatik = AUTOMATIK_DEFAULT
        self.deadman = "fehlt" if not DEADMAN_NUMBER else "unbestaetigt"
        self.deadman_at = None
        self.errors = 0
        self.verify_degraded = False

        self._since = self._mono()
        self._low_since = None
        self._blind_since = None            # Wanduhr (ueberlebt Neustart)
        self._thr_acc: list = []            # [(mono, dauer)] gleitend
        self._last_switch = None            # mono der letzten EIGENEN Tat
        self._last_cmd_on = None            # was wir zuletzt kommandiert haben
        self._widerspruch_since = None
        self._unavail_since = None
        self._ha_on = None
        self._ha_poll = 0.0
        self._ha_name = None
        self._ka_ts = 0.0
        self._deadman_rest = None
        self._off_cmd_mono = None
        self._settle_start = None
        self._probe = 0.0                   # HTTP-Gegenprobe der BMS-Daten
        self._probe_ok_mono = None
        self._wall_ref = (self._wall(), self._mono())
        self._snap: dict = {"gate": "cap", "cap": min_limit_w,
                            "state": "init", "reason": "", "block": ""}

        # neustartuebergreifend
        self.off_ts = None
        self.anlauf_ts = None
        self.manual_until = None
        self.switches_today = 0
        self.day = None
        self.soc_off = None
        self.cell_off = None
        self.ah_since_off = 0.0
        self.soc_valid = True
        self.starts: list = []
        self.sauber_beendet = True
        self._rest_cell = None              # (mV, wall) Ruhe-Sample
        self._rest_since = None
        self._letzter_soc = None
        self._letzte_stromzeit = None
        self._freigabe_bis = None           # Hand-Override (Wanduhr)

    # -- Persistenz ------------------------------------------------------
    def load(self, st: dict):
        now = self._wall()

        def anker(key):
            v = st.get(key)
            if isinstance(v, (int, float)) and 0 <= now - v <= 48 * 3600:
                return v
            return None

        acs = st.get("acs")
        self.off_ts = anker("acofs")
        self.anlauf_ts = anker("acanl")
        self.soc_off = st.get("acsoc") if isinstance(
            st.get("acsoc"), (int, float)) else None
        self.cell_off = st.get("accell") if isinstance(
            st.get("accell"), (int, float)) else None
        self.ah_since_off = float(st.get("acah") or 0.0)
        self._blind_since = anker("acbl")
        self._ha_name = st.get("acent")
        self.reason = st.get("acr") or ""
        if isinstance(st.get("aca"), bool):
            self.automatik = st["aca"]      # persistierter Wert gewinnt
        mu = st.get("acmu")                 # selbstpruefend: Zukunft
        self.manual_until = mu if isinstance(mu, (int, float)) and mu > now else None
        heute = time.strftime("%Y-%m-%d", time.localtime(now))
        if st.get("acd") == heute:
            self.day = heute
            self.switches_today = int(st.get("acn") or 0)
        self.starts = [t for t in (st.get("acst") or [])
                       if isinstance(t, (int, float)) and 0 <= now - t <= 3600]
        self.sauber_beendet = bool(st.get("acx"))
        self.starts.append(now)
        self.starts = self.starts[-10:]
        # Startzustand: der Abgleich in tick() entscheidet endgueltig,
        # aber ein fehlendes acs heisst "wir wissen nichts" -> unbekannt.
        self._geladen = acs if acs in ZUSTAENDE else None

    def store(self, st: dict, bye: bool = False):
        st.update({
            "acs": self.state, "acr": self.reason,
            "acofs": self.off_ts, "acanl": self.anlauf_ts,
            "acmu": self.manual_until, "acsoc": self.soc_off,
            "accell": self.cell_off, "acah": round(self.ah_since_off, 3),
            "acn": self.switches_today, "acd": self.day,
            "aca": self.automatik, "acent": self._ha_name,
            "acbl": self._blind_since, "acst": self.starts[-10:],
            "acx": bool(bye),
        })

    # -- Schnittstelle zum Regler ---------------------------------------
    def gate(self) -> dict:
        return self._snap

    def on_command(self, suffix: str, payload: str):
        """MQTT-Befehle aus HA (Automatik-Schalter, Hand-Freigabe,
        Quittieren). Laeuft im MQTT-Thread — nur Zustand setzen."""
        p = (payload or "").strip()
        if suffix == "ac_automatik":
            self.automatik = p.upper() in ("ON", "1", "TRUE")
            self._log(f"AC-Automatik: {'EIN' if self.automatik else 'AUS'} "
                      f"(abschalten tut der Automat weiterhin)")
        elif suffix == "ac_freigabe_min":
            try:
                m = max(0.0, min(240.0, float(p)))
            except ValueError:
                return
            self._freigabe_bis = self._wall() + m * 60 if m > 0 else None
            self._log(f"AC-Hand-Freigabe fuer {m:.0f} min "
                      f"(Notaus-Bedingungen bleiben aktiv)")
        elif suffix == "ac_quittieren":
            self.fault = False
            self.fault_reason = ""
            if self.state == "stoerung":
                self._setze("unbekannt", "quittiert — Abgleich laeuft")

    # -- interne Helfer --------------------------------------------------
    #: Zustaende, die den Abschaltgrund festschreiben duerfen. In allen
    #: anderen bleibt stehen, WARUM der Wechselrichter zuletzt aus ging —
    #: sonst ueberschreibt die naechste Meldung ("Akku laedt") die einzige
    #: Information, die der Nutzer am Morgen danach sucht.
    GRUND_ZUSTAENDE = ("drossel", "aus_angefordert", "ac_aus",
                       "ac_aus_unbestaetigt", "manuell_aus", "stoerung")

    def _setze(self, neu, grund=""):
        if neu != self.state:
            self._log(f"AC: {self.state} -> {neu}"
                      + (f" ({grund})" if grund else ""))
            vorher = self.state
            self.state = neu
            self._since = self._mono()
            self._low_since = None      # Entprellung gehoert dem Zustand
            if neu == "aus_angefordert":
                # Eine Hand-Freigabe ueberlebt keinen Abschaltgrund.
                self._freigabe_bis = None
            if grund and (neu in self.GRUND_ZUSTAENDE
                          or vorher not in self.GRUND_ZUSTAENDE):
                self.reason = grund
            self._save()

    def _dauer(self):
        return self._mono() - self._since

    def _alarm(self, grund):
        if not self.fault or self.fault_reason != grund:
            self.fault, self.fault_reason = True, grund
            self._log(f"AC-STOERUNG: {grund}")
            try:
                self.ha.notify("Akku-Schutz", grund)
            except Exception:
                pass

    def _zeitsprung(self):
        """Wanduhr gegen monotone Uhr. Ein NTP-Sprung darf keine Sperre
        verkuerzen — Vorwaertssprung verschiebt die Anker mit, ein
        Rueckwaertssprung setzt sie auf jetzt."""
        w0, m0 = self._wall_ref
        w, m = self._wall(), self._mono()
        d = (w - w0) - (m - m0)
        self._wall_ref = (w, m)
        if abs(d) <= 30:
            return 0.0
        self._log(f"Zeitsprung {d:+.0f}s erkannt — Anker werden nachgezogen")
        for name in ("off_ts", "anlauf_ts"):
            v = getattr(self, name)
            if v is not None:
                setattr(self, name, v + d if d > 0 else w)
        if self._blind_since is not None:
            self._blind_since = self._blind_since + d if d > 0 else w
        return d

    # -- Wahrnehmung -----------------------------------------------------
    def _poll_ha(self):
        """Ist-Zustand der Dose. Die WAHRHEIT ueber 'ist die Dose an'
        steht in HA — state.json speichert nur Absicht und Uhren."""
        if self._mono() - self._ha_poll < POLL_S:
            return None
        self._ha_poll = self._mono()
        zustand, attrs, code = self.ha.state(SWITCH_ENTITY)
        if code == 404:
            self._alarm("Entitaet existiert nicht — ac_switch_entity pruefen")
            self._setze("stoerung", "Entitaet existiert nicht")
            return None
        if code == 401:
            self._alarm("homeassistant_api fehlt in config.yaml")
            return None
        if code != 200 or zustand in (None, "unavailable", "unknown"):
            self.errors += 1
            if self._unavail_since is None:
                self._unavail_since = self._mono()
            elif self._mono() - self._unavail_since > UNAVAIL_S:
                # Ehrlich werden: der letzte bekannte Zustand ist jetzt
                # eine Vermutung, keine Wahrheit. Sonst haelt "unbekannt"
                # nicht und der Automat pendelt zurueck nach init.
                self._ha_on = None
            return None
        self._unavail_since = None
        name = attrs.get("friendly_name")
        if name and self._ha_name and name != self._ha_name:
            self._alarm(f"Entitaet ausgetauscht ({self._ha_name} -> {name})")
        self._ha_name = name or self._ha_name
        an = (zustand == "on")
        vorher, self._ha_on = self._ha_on, an
        if vorher is None or vorher == an:
            return None
        # Flanke. Eigene Handlungen sind fuer 30 s "unsere" Flanke.
        eigen = (self._last_switch is not None
                 and self._mono() - self._last_switch < 30
                 and self._last_cmd_on == an)
        return None if eigen else ("on" if an else "off")

    def _bms_lesen(self) -> dict:
        s = self.bms.snapshot()
        now = self._wall()
        # Ah-Integral seit dem AC-Aus: die faelschungssichere Freigabe-
        # groesse. Eine Wolkenluecke kostet nur ihren eigenen Beitrag,
        # sie nullt nichts — anders als eine "30 Minuten am Stueck"-Regel,
        # die im deutschen Dezember nie erfuellt wird.
        i = s.get("current")
        if i is not None:
            i *= CURRENT_SIGN
            if self._letzte_stromzeit is not None:
                dt = max(0.0, min(60.0, now - self._letzte_stromzeit))
                if self.state in ("ac_aus", "freigabe_beobachtung",
                                  "aus_angefordert", "manuell_aus"):
                    self.ah_since_off += max(0.0, i) * dt / 3600.0
            self._letzte_stromzeit = now
            # Ruhe-Sample fuer die Zellspannung: nur bei kleinem Strom ist
            # die Klemmenspannung ein SoC-Mass. Unter Last luegt sie nach
            # unten, beim Laden nach oben.
            if abs(i) <= REST_I_A:
                if self._rest_since is None:
                    self._rest_since = now
                elif now - self._rest_since >= REST_S and s.get("cell_min"):
                    self._rest_cell = (s["cell_min"], now)
            else:
                self._rest_since = None
        # SoC nur als Veto, nie als Beweis: nach einer Schutzabschaltung
        # setzt das JK seinen Coulomb-Zaehler neu und springt.
        soc = s.get("soc")
        if soc is not None:
            if (self._letzter_soc is not None
                    and abs(soc - self._letzter_soc) > 3):
                self.soc_valid = False
            self._letzter_soc = soc
        if str(s.get("cs", "")).lower() == "float":
            self.soc_valid = True
        s["soc_valid"] = self.soc_valid
        s["cell_ruhe"] = (self._rest_cell[0] if self._rest_cell
                          and now - self._rest_cell[1] <= REST_MAX_AGE_S
                          else None)
        # FRISCH heisst: es liegt eine SCHUTZGROESSE vor. Ein Herzschlag
        # ohne Werte ist kein Frischebeweis — und die HTTP-Gegenprobe
        # liefert nur ein Lebenszeichen der Fusion, keine Zellspannung.
        # Sie darf deshalb Zeit kaufen (ein Mosquitto-Neustart soll keinen
        # vollen Akku abschalten), aber niemals "frisch" behaupten.
        if s.get("cell_min") is None and s.get("soc") is None:
            s["frisch"] = False
        if not s["frisch"]:
            self._gegenprobe()
        s["blind_s"] = 0.0
        if s["frisch"]:
            self._blind_since = None
        else:
            if self._blind_since is None:
                self._blind_since = now
            s["blind_s"] = max(0.0, now - self._blind_since)
        return s

    def _gegenprobe(self):
        """HTTP direkt an die OpenDTU — unabhaengig vom MQTT-Broker."""
        if not OPENDTU_URL or self._mono() - self._probe < 30:
            return
        self._probe = self._mono()
        try:
            with urllib.request.urlopen(
                    f"{OPENDTU_URL}/api/batterylivedata/status",
                    timeout=4) as r:
                d = json.loads(r.read().decode())
            age = d.get("data_age")
            if isinstance(age, (int, float)) and age <= BMS_MAX_AGE_S:
                self._probe_ok_mono = self._mono()
        except Exception:
            pass

    # -- Bedingungen -----------------------------------------------------
    def _notaus(self, s) -> str:
        """Sofort-Abschaltung. Der Zellwert braucht ZWEI Samples: eine 0
        aus einem Fehlframe erfuellt sonst jede Unterspannungsbedingung
        und oeffnet das Relais unter voller Last."""
        if s.get("alarm_uv") in (1, 1.0, "1", "true", "ON"):
            return "BMS meldet Unterspannungsalarm"
        if s.get("discharge_ok") in (0, 0.0, "0", "false", "OFF"):
            return "BMS hat die Entladung gesperrt"
        if s.get("online") in (0, 0.0, "0", "false", "OFF"):
            return "BMS meldet sich als offline"
        c = s.get("cell_min")
        if c is not None and c <= HARD_CELL_MV:
            self._hart = getattr(self, "_hart", 0) + 1
            if self._hart >= 2:
                return f"Zelle {c:.0f} mV unter Notgrenze {HARD_CELL_MV} mV"
        else:
            self._hart = 0
        return ""

    def _abschalten(self, s) -> str:
        c, soc = s.get("cell_min"), s.get("soc")
        if c is not None and c <= OFF_CELL_MV:
            return f"Zelle {c:.0f} mV <= {OFF_CELL_MV} mV"
        if soc is not None and soc <= OFF_SOC:
            return f"SoC {soc:.0f} % <= {OFF_SOC} %"
        return ""

    def _drosseln(self, s) -> str:
        c, soc = s.get("cell_min"), s.get("soc")
        if c is not None and c <= THROTTLE_CELL_MV:
            return f"Zelle {c:.0f} mV <= {THROTTLE_CELL_MV} mV"
        if soc is not None and soc <= THROTTLE_SOC:
            return f"SoC {soc:.0f} % <= {THROTTLE_SOC} %"
        return ""

    def _blindflug(self, s) -> str:
        """Fail-closed mit Augenmass: kurz blind -> drosseln, lange blind
        oder blind bei niedrigem SoC -> abschalten. Freigabe: nie.

        Die HTTP-Gegenprobe verschiebt nur die KURZE Frist: antwortet die
        Fusion noch, ist wahrscheinlich nur der Broker weg, und ein voller
        Akku soll das ueberleben. Die lange Frist kauft sie nicht frei —
        ohne Zellspannung wird irgendwann abgeschaltet, Punkt."""
        b = s.get("blind_s", 0.0)
        if b <= BLIND_THROTTLE_S:
            return ""
        soc = s.get("soc") if s.get("soc") is not None else self._letzter_soc
        if b > BLIND_OFF_S:
            return f"BMS seit {b/60:.0f} min blind"
        fusion_lebt = (self._probe_ok_mono is not None
                       and self._mono() - self._probe_ok_mono <= 60)
        if (b > BLIND_OFF_LOW_S and not fusion_lebt
                and (soc is None or soc <= 40)):
            return f"BMS blind bei zuletzt {soc if soc is not None else '?'} % SoC"
        return "drossel"

    def _freigabe(self, s):
        """(darf_ein, klartext). Freigabe braucht POSITIVEN Nachweis —
        nie die Abwesenheit von Gegenbeweisen.

        Zwei Klassen von Bedingungen, und der Unterschied ist wichtig:
        SCHUTZbedingungen (frische Daten, Zellspannung ueber der
        Abschaltgrenze, Schaltbudget, Sperrzeit) gelten IMMER. ERTRAGS-
        bedingungen (Zeitfenster, Mindest-Aus-Zeit, SoC-Zuwachs,
        Ladungsnachweis) darf die Hand-Freigabe ueberspringen — sonst
        haette der Nutzer im Winter keinen Weg ausser dem, den Schutz
        auszubauen. Was die Hand NICHT darf: einen leeren Akku zuschalten
        oder das Relais im Minutentakt takten lassen."""
        f, hart = [], []
        now = self._wall()
        hand = bool(self._freigabe_bis and now < self._freigabe_bis)
        if self._freigabe_bis and not hand:
            self._freigabe_bis = None        # abgelaufen: wegraeumen
        if not self.automatik and not hand:
            return False, "Automatik steht auf AUS (schaltet nur ab)"

        # --- Schutzbedingungen, immer ---
        if not s.get("frisch"):
            hart.append("keine frischen BMS-Daten")
        if time.gmtime(now).tm_year < 2026:
            hart.append("Uhr unsicher")
        if self.switches_today >= MAX_SWITCH_PER_DAY:
            hart.append(f"Tagesbudget {MAX_SWITCH_PER_DAY} Schaltungen erschoepft")
        if (self._last_switch is not None
                and self._mono() - self._last_switch < SWITCH_COOLDOWN_S):
            hart.append("Schalt-Sperrzeit laeuft")
        cr, cl = s.get("cell_ruhe"), s.get("cell_min")
        if hand:
            # Reserve ueber der Abschaltschwelle: sonst schaltet die Hand
            # ein, der Automat sofort wieder ab, und das im Minutentakt.
            reserve = OFF_CELL_MV + 60
            wert = cr if cr is not None else cl
            if wert is None:
                hart.append("keine Zellspannung")
            elif wert < reserve:
                hart.append(f"Zelle {wert:.0f} mV unter der Handschaltgrenze "
                            f"{reserve} mV — der Akku ist leer")
        elif cr is not None:
            if cr < ON_CELL_MV:
                f.append(f"Ruhespannung {cr:.0f} < {ON_CELL_MV} mV")
        elif cl is not None:
            # Ohne Ruhe-Sample gilt der strengere Wert unter Ladung: der
            # Victron zieht die Klemmenspannung sofort hoch.
            if cl < ON_CELL_MV + 80:
                f.append(f"Zelle {cl:.0f} < {ON_CELL_MV + 80} mV (kein Ruhewert)")
        else:
            f.append("keine Zellspannung")

        # --- Ertragsbedingungen ---
        soc = s.get("soc")
        kurz = getattr(self, "_kurzweg", False)
        soc_zaehlt = self.soc_valid and soc is not None
        if not hand:
            if soc_zaehlt:
                if soc < ON_SOC:
                    f.append(f"SoC {soc:.0f} < {ON_SOC} %")
                if not kurz and self.soc_off is not None:
                    if soc - self.soc_off < ON_DSOC:
                        f.append(f"nur +{soc - self.soc_off:.0f} von "
                                 f"{ON_DSOC} Punkten geladen")
                elif not kurz and soc < ON_SOC + ON_DSOC:
                    f.append(f"ohne Referenz erst ab {ON_SOC + ON_DSOC} % "
                             f"(jetzt {soc:.0f})")
            if not kurz:
                # LADUNGSNACHWEIS. Ohne glaubwuerdigen SoC ist er Pflicht:
                # ein Ah-Integral kann kein BMS-Reset faelschen, ein SoC
                # schon. Fehlt die Kapazitaet, tritt der Nachweis ueber den
                # Victron ODER eine erholte Ruhespannung an seine Stelle —
                # der Schutz darf nicht daran haengen, dass die
                # Victron-Topics ueberhaupt konfiguriert sind.
                ziel_ah = ON_AH or (0.35 * CAPACITY_AH if CAPACITY_AH else 0)
                if ziel_ah:
                    if self.ah_since_off < ziel_ah:
                        f.append(f"erst {self.ah_since_off:.1f} von "
                                 f"{ziel_ah:.1f} Ah nachgeladen")
                elif not (str(s.get("cs", "")).lower() in ("absorption", "float")
                          or (cr is not None and cr >= ON_CELL_MV + 40)):
                    f.append("kein Ladungsnachweis (weder Victron-Absorption/"
                             "Float noch erholte Ruhespannung)")
                elif not soc_zaehlt and cr is None:
                    f.append("SoC unglaubwuerdig und keine Ruhespannung — "
                             "kein belastbarer Nachweis")
                if self.off_ts is not None and now - self.off_ts < OFF_MIN_S:
                    f.append(f"Mindest-Aus-Zeit noch "
                             f"{(OFF_MIN_S - (now - self.off_ts))/60:.0f} min")
            d = s.get("cell_diff")
            if ON_DIFF_MAX_MV and d is not None and d > ON_DIFF_MAX_MV:
                f.append(f"Zell-Drift {d:.0f} > {ON_DIFF_MAX_MV} mV")
            # Zeitfenster — entfaellt bei vollem Akku (ein voller Akku darf
            # auch um 17 Uhr einspeisen).
            if not (soc_zaehlt and soc >= 80 and cr is not None):
                h = time.localtime(now).tm_hour
                if not (ON_EARLIEST_H <= h < ON_LATEST_H):
                    f.append(f"ausserhalb {ON_EARLIEST_H}-{ON_LATEST_H} Uhr")

        if hart:
            return False, "Freigabe gesperrt: " + "; ".join(hart)
        if hand:
            return True, "Hand-Freigabe aktiv"
        if f:
            return False, "Freigabe fehlt: " + "; ".join(f)
        return True, "alle Freigabebedingungen erfuellt"

    def _widerspruch(self, s) -> str:
        """'Aus' ist eine Behauptung. Diese Pruefung faengt klebendes
        Relais, einpolige N-Trennung, umgesteckte Verlaengerung und die
        schlicht falsche Steckdose in einem Zug ab."""
        gruende = []
        i = s.get("current")
        if i is not None and -i * CURRENT_SIGN > WIDERSPRUCH_A:
            gruende.append(f"BMS meldet {abs(i):.1f} A Entladung")
        m = self._dtu_meta() or {}
        if m.get("reachable") and (m.get("age_s", 999) < 60):
            gruende.append("OpenDTU erreicht den Inverter weiter")
        if not gruende:
            self._widerspruch_since = None
            return ""
        if self._widerspruch_since is None:
            self._widerspruch_since = self._mono()
            return ""
        if self._mono() - self._widerspruch_since < WIDERSPRUCH_S:
            return ""
        return " und ".join(gruende)

    # -- Handeln ---------------------------------------------------------
    def _schalte(self, an: bool) -> bool:
        code = self.ha.service("switch", "turn_on" if an else "turn_off",
                               {"entity_id": SWITCH_ENTITY})
        if code == 200:
            self._last_switch = self._mono()
            self._last_cmd_on = an
            # Den kommandierten Zustand SOFORT uebernehmen und bald neu
            # nachsehen: sonst gilt bis zum naechsten Poll (bis zu 10 s)
            # noch der alte Ist-Zustand, und der Anlauf haelt das eigene
            # Einschalten fuer eine fremde Abschaltung.
            self._ha_on = an
            self._ha_poll = self._mono() - POLL_S + 2
            if not an:
                self._off_cmd_mono = self._mono()
            return True
        self.errors += 1
        if code == 404:
            self._alarm("Entitaet existiert nicht — ac_switch_entity pruefen")
        elif code == 401:
            self._alarm("homeassistant_api fehlt in config.yaml")
        return False

    def _keepalive(self):
        """TOTMANN. Die Dose bekommt eine Auto-Off-Frist, die wir laufend
        nachtriggern. Faellt irgendetwas in der Kette aus — WLAN, Broker,
        HA, dieser Prozess —, schaltet das Relais von selbst ab. Das ist
        der einzige Schutz, der keinen zugestellten Befehl braucht."""
        if self.state not in ("normal", "drossel", "anlauf", "manuell_ein"):
            return
        if not DEADMAN_NUMBER or not DEADMAN_SWITCH:
            self.deadman = "fehlt"
            return
        if self._mono() - self._ka_ts < KEEPALIVE_S:
            return
        self._ka_ts = self._mono()
        ok = self.ha.service("number", "set_value",
                             {"entity_id": DEADMAN_NUMBER,
                              "value": max(1, int(DEADMAN_S / 60))}) == 200
        ok &= self.ha.service("switch", "turn_on",
                              {"entity_id": DEADMAN_SWITCH}) == 200
        # VERIFIZIEREN statt annehmen: eine Sicherheitseinstellung, die die
        # Software nicht nachpruefen kann, ist keine.
        if ok and DEADMAN_AT:
            wert, _, code = self.ha.state(DEADMAN_AT)
            if code == 200 and wert not in (None, "unknown", "unavailable"):
                self.deadman_at = wert
                self.deadman = "ok"
                return
        self.deadman = "unbestaetigt" if ok else "fehlt"

    def _bremsen(self, immer=False):
        """Limit auf Minimum — best effort, Fehler werden geschluckt. Ein
        Limitbefehl an einen wirklich toten HMS kostet eine Exception, ein
        unterlassener an einen lebenden kostet den Akku."""
        hart = self.state in ("drossel", "stoerung", "ac_aus_unbestaetigt",
                              "aus_angefordert", "init")
        selten = self.state in ("ac_aus", "manuell_aus", "getrennt",
                                "freigabe_beobachtung", "unbekannt")
        if not immer and self._snap.get("gate") == "frei":
            # Wer dem Regler die Freigabe gibt, darf ihm nicht gleichzeitig
            # ins Limit fahren — sonst laufen zwei Instanzen gegeneinander.
            return
        if not (immer or hart or selten):
            return
        if selten and not immer:
            if self._mono() - getattr(self, "_bremse_ts", -1e9) < 300:
                return
        self._bremse_ts = self._mono()
        try:
            self._brake()
        except Exception:
            pass

    def _tagesreset(self):
        heute = time.strftime("%Y-%m-%d", time.localtime(self._wall()))
        if self.day != heute:
            self.day, self.switches_today = heute, 0

    # -- Zustandsabgleich nach dem Start ---------------------------------
    def _abgleich(self, s):
        """Die Wahrheit steht in HA. state.json liefert nur die Absicht.
        Ein Kaltstart darf NIE nach manuell_aus fuehren — dieser Zustand
        entsteht ausschliesslich aus einer beobachteten Fremdflanke."""
        vor = getattr(self, "_geladen", None)
        # E-Kurz: war der letzte Zustand kein Schutzzustand, kostet ein
        # Blackout oder ein Add-on-Update keine 45 Minuten Ertrag.
        self._kurzweg = vor in ("normal", "drossel", "anlauf", "manuell_ein")
        if len([t for t in self.starts if self._wall() - t <= 900]) >= 3:
            self._alarm("Prozess startet staendig neu — Schutz drosselt")
            self._setze("drossel", "Prozess instabil")
            return
        if self._ha_on is None:
            if self._dauer() > 120:
                self._setze("unbekannt", "HA antwortet nicht")
            return
        self._geladen = None            # der Abgleich gilt genau einmal
        if self._ha_on:
            if vor in ("ac_aus", "freigabe_beobachtung", "ein_angefordert"):
                if not s.get("frisch") and self._dauer() < 120:
                    return                      # kurz auf Daten warten
                if not s.get("frisch") or self._abschalten(s) or self._notaus(s):
                    self._setze("aus_angefordert", "Abgleich: Dose an, Akku leer")
                else:
                    self._setze("manuell_ein", "Abgleich: von Hand eingeschaltet")
                    self.manual_until = self.manual_until or (
                        self._wall() + MANUAL_ON_MAX_S)
            elif vor == "anlauf" and self.anlauf_ts:
                self._setze("anlauf", "Abgleich: Anlauf laeuft weiter")
            else:
                self._setze("normal", "Abgleich: Dose an")
        else:
            # Dose aus. Kein Schutzgrund persistiert -> selbstheilend in
            # die Beobachtung, nicht in manuell_aus.
            self._setze("freigabe_beobachtung",
                        "Abgleich: Dose aus" + (" (Schutzgrund: " + self.reason + ")"
                                                if self.reason and not self._kurzweg else ""))

    # -- Der Takt --------------------------------------------------------
    def tick(self):
        if not enabled():
            return
        try:
            self._zeitsprung()
            self._tagesreset()
            flanke = self._poll_ha()
            s = self._bms_lesen()
            if self.state == "init":
                self._abgleich(s)
            else:
                self._schritt(s, flanke)
            self._bremsen()
            self._keepalive()
            self._snapshot(s)
        except Exception as e:              # der Automat darf nie sterben
            self.errors += 1
            self._log(f"AC-Automat Fehler: {e}")

    def _schritt(self, s, flanke):
        an_erwartet = self.state in ("normal", "drossel", "anlauf",
                                     "manuell_ein")
        notaus = self._notaus(s) if an_erwartet else ""
        blind = self._blindflug(s) if an_erwartet else ""
        if an_erwartet:
            if notaus:
                self._eilpfad = True
                self._setze("aus_angefordert", notaus)
                return
            if blind and blind != "drossel":
                self._setze("aus_angefordert", blind)
                return
            if flanke == "off":
                # Totmann-Trip oder Menschenhand? Beides ist "aus" — aber
                # nur die Hand soll den Automaten blockieren.
                trip = self._totmann_faellig()
                self._setze("ac_aus" if trip else "manuell_aus",
                            "Totmann ausgeloest (Steuerkette war weg)" if trip
                            else "von Hand ausgeschaltet")
                self.off_ts = self._wall()
                if trip:
                    self._kurzweg = True     # kein Schutzgrund -> E-Kurz
                return

        if self.state == "normal":
            if blind == "drossel":
                self._setze("drossel", "BMS blind")
            elif self._drosseln(s):
                if self._low_since is None:
                    self._low_since = self._mono()
                elif self._mono() - self._low_since >= OFF_TRIP_S:
                    self._setze("drossel", self._drosseln(s))
            else:
                self._low_since = None
            if self._unavail_since and self._mono() - self._unavail_since > UNAVAIL_S:
                self._setze("unbekannt", "Dose meldet sich nicht mehr")

        elif self.state == "drossel":
            self._thr_acc.append(self._mono())
            self._thr_acc = [t for t in self._thr_acc
                             if self._mono() - t <= THROTTLE_WINDOW_S]
            aus = self._abschalten(s)
            if aus:
                if self._low_since is None:
                    self._low_since = self._mono()
                elif self._mono() - self._low_since >= OFF_TRIP_S:
                    self._setze("aus_angefordert", aus)
            else:
                self._low_since = None
                if len(self._thr_acc) > THROTTLE_BUDGET_S:
                    self._setze("aus_angefordert",
                                "Drosselung wirkungslos — der HMS zieht weiter")
                elif not self._drosseln(s) and blind != "drossel" and self._dauer() > 60:
                    self._setze("normal", "Akku wieder ueber der Schwelle")

        elif self.state == "aus_angefordert":
            self._abschalt_ablauf(s)

        elif self.state in ("ac_aus", "freigabe_beobachtung"):
            w = self._widerspruch(s)
            if w:
                self._alarm(f"Dose meldet aus, aber: {w}")
                self._setze("ac_aus_unbestaetigt", w)
                return
            if flanke == "on":
                self.manual_until = self._wall() + MANUAL_ON_MAX_S
                self._setze("manuell_ein", "von Hand eingeschaltet")
                return
            # Laden erkennen: bevorzugt am Victron, aber ERSATZWEISE am
            # BMS-Ladestrom — sonst kommt der Wechselrichter nie wieder
            # hoch, wenn die Victron-Topics gar nicht konfiguriert sind.
            i = s.get("current")
            laedt = (str(s.get("cs", "")).lower() in ("bulk", "absorption", "float")
                     or (s.get("ppv") or 0) > CHARGE_PPV_W
                     or (i is not None and i * CURRENT_SIGN > 1.0))
            hand = bool(self._freigabe_bis
                        and self._wall() < self._freigabe_bis)
            if self.state == "ac_aus" and (laedt or hand):
                self._setze("freigabe_beobachtung",
                            "Hand-Freigabe" if hand else "Akku laedt")
            elif self.state == "freigabe_beobachtung":
                ok, txt = self._freigabe(s)
                self.block = txt
                if ok:
                    self._setze("ein_angefordert", txt)

        elif self.state == "ac_aus_unbestaetigt":
            w = self._widerspruch(s)
            if not w and self._ha_on is False and self._dauer() > 300:
                self.fault = False
                self._setze("ac_aus", "Widerspruch verschwunden")
            elif self._mono() - (self._last_switch or 0) > 60:
                self._schalte(False)

        elif self.state == "ein_angefordert":
            self._ka_ts = 0.0
            self._keepalive_vorbereiten()
            if self._schalte(True):
                self.anlauf_ts = self._wall()
                self.switches_today += 1
                self._kurzweg = False
                self._setze("anlauf", "eingeschaltet")
            elif self._dauer() > 60:
                self._setze("stoerung", "Einschalten fehlgeschlagen")

        elif self.state == "anlauf":
            # NUR auf eine Abfrage reagieren, die NACH dem eigenen
            # Einschaltbefehl stattgefunden hat.
            if self._ha_on is False and self._ha_poll > (self._last_switch or 0):
                self._setze("manuell_aus", "waehrend des Anlaufs abgeschaltet")
                return
            m = self._dtu_meta() or {}
            alt = self._wall() - (self.anlauf_ts or self._wall())
            if alt > START_BLIND_S and m.get("reachable"):
                self._setze("normal", "Inverter ist da")
            elif self._dauer() > START_TIMEOUT_S:
                # monoton, nicht ueber die Wanduhr: ein nach einem Neustart
                # uebernommener alter Anker wuerde sonst sofort ablaufen und
                # einen gesunden Wechselrichter abschalten.
                self._setze("stoerung", "Inverter kommt nicht hoch")

        elif self.state == "manuell_ein":
            if flanke == "off":
                self._setze("manuell_aus", "von Hand ausgeschaltet")
            elif self._abschalten(s) or notaus:
                self._setze("aus_angefordert",
                            self._abschalten(s) or notaus)
            elif self.manual_until and self._wall() > self.manual_until:
                self._setze("freigabe_beobachtung", "Hand-Fenster abgelaufen")

        elif self.state == "manuell_aus":
            w = self._widerspruch(s)
            if w:
                self._alarm(f"Dose meldet aus, aber: {w}")
                self._setze("ac_aus_unbestaetigt", w)
            elif flanke == "on":
                self.anlauf_ts = self._wall()
                self._setze("anlauf", "von Hand eingeschaltet")
            elif self._dauer() > MANUAL_OFF_MAX_S:
                # Kein ewiger Wartezustand: nach einem Tag ohne Mensch
                # entscheidet wieder der Automat (der schaltet nur ein,
                # wenn alle Freigabebedingungen erfuellt sind).
                self._setze("freigabe_beobachtung",
                            "Handschaltung laeuft nach 24 h aus")

        elif self.state == "getrennt":
            if self._ha_on is not None:
                self._setze("init", "Dose wieder da — Abgleich")

        elif self.state == "unbekannt":
            if self._ha_on is not None and self._unavail_since is None:
                self._setze("init", "Zustand wieder eindeutig")
            elif self._dauer() > 300:
                m = self._dtu_meta() or {}
                i = s.get("current")
                stromlos = (not m.get("reachable")
                            and (i is None or -i * CURRENT_SIGN < 1.0))
                self._setze("getrennt" if stromlos else "stoerung",
                            "Dose stromlos — vermutlich Stecker gezogen"
                            if stromlos else "Dose nicht ansprechbar")

        elif self.state == "stoerung":
            if self._ha_on is True and self._mono() - (self._last_switch or 0) > 60:
                self._schalte(False)
            elif (self._ha_on is False and not self._widerspruch(s)
                  and self._dauer() > FAULT_CLEAR_S):
                self._setze("ac_aus", "Stoerung ausgeheilt")

    def _totmann_faellig(self) -> bool:
        """War die geraeteseitige Auto-Off-Frist gerade faellig? Nur dann
        ist ein unerwartetes Aus ein Totmann-Trip und keine Handschaltung.
        Ohne lesbaren Zeitpunkt gilt: Hand (die sichere Annahme — sie
        blockiert den Automaten, statt ihn wieder einschalten zu lassen)."""
        if self.deadman != "ok" or not self.deadman_at:
            return False
        try:
            from datetime import datetime, timezone
            t = datetime.fromisoformat(
                str(self.deadman_at).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return abs(self._wall() - t.timestamp()) <= 60
        except Exception:
            return False

    def _keepalive_vorbereiten(self):
        """Totmann-Frist setzen, BEVOR eingeschaltet wird — sonst laeuft
        der Inverter im schlimmsten Fall ohne Rueckfallebene."""
        if DEADMAN_NUMBER:
            self.ha.service("number", "set_value",
                            {"entity_id": DEADMAN_NUMBER,
                             "value": max(1, int(DEADMAN_S / 60))})
        if DEADMAN_SWITCH:
            self.ha.service("switch", "turn_on",
                            {"entity_id": DEADMAN_SWITCH})

    def _abschalt_ablauf(self, s):
        """Reihenfolge: merken -> persistentes Limit -> bremsen ->
        absetzen lassen -> schalten -> quittieren."""
        if self._off_cmd_mono is None:
            if self.off_ts is None or self._dauer() < 1:
                # Persistentes Limit schreiben, BEVOR getrennt wird: der
                # HMS kommt sonst mit seinem Flash-Wert (im Zweifel 100 %)
                # hoch und zieht die ersten Sekunden alles aus dem gerade
                # erst halb geladenen Akku.
                try:
                    self._persist(START_LIMIT_W)
                except Exception:
                    pass
                self.off_ts = self._wall()
                self.soc_off = s.get("soc")
                self.cell_off = s.get("cell_min")
                self.ah_since_off = 0.0
                self._save()
            eil = getattr(self, "_eilpfad", False)
            p = self._dtu_power()
            zu_viel = p is not None and p > MAX_SWITCH_LOAD_W
            if not eil and zu_viel and self._dauer() < 120:
                self._bremsen(immer=True)
                return                      # nicht unter voller Last schalten
            if not eil and self._dauer() < SETTLE_S and (p or 0) > 300:
                self._bremsen(immer=True)
                return
            self._schalte(False)
            return
        # Quittung
        seit = self._mono() - self._off_cmd_mono
        m = self._dtu_meta() or {}
        i = s.get("current")
        zeuge = ((not m.get("reachable")) or m.get("age_s", 0) > 60
                 or (i is not None and -i * CURRENT_SIGN < 1.0))
        if self._ha_on is False and zeuge:
            self._fertig_aus()
        elif seit > OFF_CONFIRM_S:
            w = self._widerspruch(s)
            if w:
                self._alarm(f"Dose meldet aus, aber: {w}")
                self._setze("ac_aus_unbestaetigt", w)
            elif self._ha_on is False:
                self.verify_degraded = True
                self._fertig_aus()
            elif seit > OFF_CONFIRM_S + 60:
                self._setze("stoerung", "Dose reagiert nicht auf turn_off")
        elif seit > VERIFY_S and self._ha_on is not False:
            self._schalte(False)            # zweiter Versuch

    def _fertig_aus(self):
        self.switches_today += 1
        self._off_cmd_mono = None
        self._eilpfad = False
        self._setze("ac_aus", self.reason)

    # -- Snapshot fuer den Regler und HA ---------------------------------
    def _snapshot(self, s):
        if self.state in ("normal", "manuell_ein"):
            gate, cap = "frei", None
        elif self.state == "anlauf":
            gate, cap = "cap", START_LIMIT_W
        elif self.state in ("drossel", "init"):
            gate, cap = "cap", self.min_limit_w
        elif self.state == "unbekannt":
            gut = (s.get("frisch") and not self._drosseln(s)
                   and (s.get("soc") or 0) > 35)
            gate, cap = ("frei", None) if gut else ("cap", self.min_limit_w)
        elif self.state == "aus_angefordert":
            gate, cap = ("stumm", None) if self._off_cmd_mono else (
                "cap", self.min_limit_w)
        else:
            gate, cap = "stumm", None
        self._snap = {
            "gate": gate, "cap": cap, "state": self.state,
            "reason": self.reason, "block": self.block,
            "fault": self.fault, "fault_reason": self.fault_reason,
            "on": self._ha_on, "deadman": self.deadman,
            "deadman_at": self.deadman_at, "switches_today": self.switches_today,
            "off_ts": self.off_ts, "errors": self.errors,
            "verify_degraded": self.verify_degraded,
            "cell_min": s.get("cell_min"), "cell_diff": s.get("cell_diff"),
            "soc_bms": s.get("soc"), "soc_valid": self.soc_valid,
            "data_age": s.get("data_age"), "blind_s": s.get("blind_s"),
            "ah_since_off": round(self.ah_since_off, 2),
            "automatik": self.automatik,
        }


def start(guard: AcGuard, log):
    """Eigener Thread, 1 Hz. Der Regelzyklus darf nie auf HA warten."""
    def lauf():
        # Einmalig zeigen, welche BMS-Topics WIRKLICH ankommen — der
        # haeufigste Grund fuer einen Schutz, der nie ausloest, sind
        # geratene Topicnamen.
        gemeldet = False
        while True:
            guard.tick()
            if not gemeldet and guard._mono() - guard._since > 60:
                t = guard.bms.unbekannte_topics()
                log(f"BMS-Topics unter {BATT_PREFIX}battery/: "
                    + (", ".join(t) if t else "KEINE — Praefix/Verkabelung pruefen"))
                for name, topic in (("Zellspannung", T_CELLMIN),
                                    ("Zell-Drift", T_CELLDIFF),
                                    ("SoC", T_SOC), ("Strom", T_CURRENT)):
                    if topic not in t:
                        log(f"WARNUNG: {name}-Topic '{topic}' kam nie an — "
                            f"BATT_TOPIC_* anpassen")
                gemeldet = True
            time.sleep(1.0)

    th = threading.Thread(target=lauf, daemon=True, name="ac-guard")
    th.start()
    return th
