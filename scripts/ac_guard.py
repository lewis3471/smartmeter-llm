#!/usr/bin/env python3
"""AC-seitiger Tiefentladeschutz: eine Steckdose, ein Schalter.

WARUM ES DAS GIBT — ein Limit ist keine Abschaltung. In der Nacht vom
11. auf den 12.09.2026 stand das Limit ab 18:07 auf 50 W, und der
HMS-2000-4T zog trotzdem bis 03:36 durchgehend ~35 W aus dem Akku, bis
das BMS bei 41,5 V (2,6 V/Zelle) hart abschaltete. Vierte Nacht in
Folge. Still ist der HMS nur stromlos.

WARUM ES SO KLEIN SEIN DARF — der HMS versorgt seine Elektronik aus der
DC-Seite. Bei stromloser AC-Seite bleibt er ueber die DTU erreichbar und
meldet weiter die Akkuspannung (Beleg 12.09.2026, 15:22-16:58, Dose aus:
AC 1,4 V, DC 50,7 -> 52,1 V, reachable=on). Der Packspannungs-Waechter
(battery_guard in meter_reader.py) sieht also auch im Aus weiter. Dieses
Modul uebersetzt nur sein Urteil in die Dose:

    batt_hold True   ->  Dose AUS (vorher Limit persistent auf Minimum,
                         damit der HMS nach dem Einschalten mit 50 W
                         hochkommt und nicht mit dem letzten Tageswert)
    batt_hold False  ->  Dose EIN, fruehestens AC_OFF_MIN_S nach dem Aus

Kein BMS, kein Victron, keine Zellspannungen, kein Totmann, keine
Zustandsmaschine — genau EINE Option: ac_switch_entity. Die Dose gehoert
dem Waechter: schaltet jemand von Hand EIN, waehrend er haelt, nimmt er
das nach einer Minute zurueck. Schaltet jemand von Hand AUS, gilt das
wie ein eigenes Aus — die Mindest-Aus-Zeit laeuft ab diesem Moment. Wer
den Wechselrichter dauerhaft vom Netz will, leert ac_switch_entity oder
stoppt das Add-on.

Was dieser Schutz NICHT kann: stirbt das Add-on, waehrend die Dose EIN
ist, schaltet niemand ab. Das ist der Preis der Einfachheit. batt_hold
ueberlebt Neustarts in der state.json, der HA-Watchdog startet neu.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

SWITCH_ENTITY = os.environ.get("AC_SWITCH_ENTITY", "").strip()
# Mindest-Aus-Zeit. Unter Last sackt der Bus ein, ohne Last erholt er sich
# binnen Minuten um ~1 V — ohne Sperre waere die Freigabeschwelle kurz
# nach jedem Aus wieder erreicht und die Dose klapperte im
# Viertelstundentakt (am 11.09. tat der Limit-Waechter genau das, 14:00
# bis 18:07, und pumpte dabei die ganze Nachladung ins Haus). 30 min
# reichen, damit nur echte Nachladung freigibt.
OFF_MIN_S = float(os.environ.get("AC_OFF_MIN_S", "1800"))
POLL_S = 20.0            # Dosenzustand so oft aus HA lesen
ACT_MIN_S = 60.0         # nie zwei Schaltbefehle innerhalb einer Minute
UNAVAIL_WARN_S = 300.0   # nach so lange "unavailable" einmal laut werden
PERSIST_RETRY_S = 600.0  # Flash-Schreibzyklus im HMS: hoechstens alle 10 min
SUPERVISOR = "http://supervisor/core/api"


def enabled() -> bool:
    return bool(SWITCH_ENTITY)


class Ha:
    """Duenner Supervisor-Proxy-Client (homeassistant_api: true im
    Manifest; SUPERVISOR_TOKEN steht im Container bereits)."""

    def __init__(self, token=None, base=SUPERVISOR, opener=None):
        self.token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self.base = base
        self._opener = opener or urllib.request.urlopen

    def _req(self, pfad, daten=None, timeout=8):
        url = f"{self.base}{pfad}"
        body = json.dumps(daten).encode() if daten is not None else None
        r = urllib.request.Request(url, data=body,
                                   method="POST" if body else "GET")
        r.add_header("Authorization", f"Bearer {self.token}")
        r.add_header("Content-Type", "application/json")
        try:
            with self._opener(r, timeout=timeout) as resp:
                return resp.getcode(), json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception as e:                  # Timeout, DNS, Refused
            return 0, str(e)

    def state(self, entity):
        """Zustand der Entitaet als String, None wenn nicht lesbar."""
        code, data = self._req(f"/states/{entity}", timeout=5)
        if code == 200 and isinstance(data, dict):
            return data.get("state")
        return None

    def service(self, domain, service, daten):
        code, _ = self._req(f"/services/{domain}/{service}", daten)
        return code


class AcSwitch:
    """Haelt die Dose auf dem Zustand, den der Waechter vorgibt.

    want() wird aus dem Regelzyklus gerufen und setzt nur ein Flag;
    tick() laeuft im eigenen Thread (~1 Hz) und spricht mit HA. Der
    Regelzyklus (0,5 s) wartet nie auf die Steckdose.

    `on` ist der letzte SICHER gelesene Dosenzustand (None = seit dem
    Start noch nie gelesen). Ein WLAN-Aussetzer der Dose (HA meldet
    "unavailable") bewegt kein Relais — er loescht das Wissen also nicht,
    er sperrt nur Schaltbefehle, bis die Dose wieder lesbar ist.

    Zwei Asymmetrien mit Absicht: Ein AUS-Befehl gilt sofort als
    ausgefuehrt (sichere Richtung: der Regler schweigt lieber 20 s zu
    lange), ein EIN-Befehl erst, wenn HA "on" liest (sonst wuerde ein
    wirkungsloser Befehl beim naechsten Lesen als Hand-Aus gedeutet und
    kostete 30 min statt einer Minute Wiederholung)."""

    def __init__(self, ha, entity, log, persist=None, off_min_s=OFF_MIN_S,
                 wall=time.time, mono=time.monotonic):
        self.ha, self.entity, self._log = ha, entity, log
        self._persist = persist or (lambda: None)
        self.off_min_s = off_min_s
        self._wall, self._mono = wall, mono
        self._lock = threading.Lock()
        self.hold = None        # Urteil des Waechters; None = noch keins
        self.on = None          # Dose laut HA: True/False, None = nie gelesen
        self.off_wall = None    # Wanduhr des letzten Aus (ueberlebt Neustart)
        self._polled = -1e9
        self._acted = -1e9
        self._unavail_since = None
        self._persistiert = False   # Minimum fuer DIESE Halte-Episode im Flash
        self._persist_mono = -1e9   # ... und wann zuletzt (Wiederholung s.u.)
        self.errors = 0

    # --- Schnittstelle zum Regelzyklus ---------------------------------
    def want(self, hold: bool):
        with self._lock:
            hold = bool(hold)
            if not hold:
                # Neue Halte-Episode -> beim naechsten Aus wieder einmal
                # persistieren. Nur EINMAL je Episode: das ist ein
                # Flash-Schreibzyklus im Wechselrichter, und ein
                # Schaltkonflikt (Zeitplan, Automation, Gruppenschalter)
                # darf ihn nicht minuetlich wiederholen.
                self._persistiert = False
            self.hold = hold

    def load(self, st: dict):
        aco = st.get("aco")
        if isinstance(aco, (int, float)) and not isinstance(aco, bool):
            alter = self._wall() - aco
            # Nur plausible Anker uebernehmen: ein Zeitsprung darf die
            # Sperre weder auf Tage verlaengern noch in die Zukunft legen.
            if 0 <= alter <= 48 * 3600:
                self.off_wall = float(aco)

    def store(self, st: dict):
        if self.off_wall is not None:
            st["aco"] = self.off_wall

    def sperre_rest_s(self) -> float:
        if self.off_wall is None:
            return 0.0
        return max(0.0, self.off_min_s - (self._wall() - self.off_wall))

    def snapshot(self) -> dict:
        with self._lock:
            hold = self.hold
        on = self.on
        if on is None:
            text = "unbekannt"
        elif on:
            text = "ein"
        else:
            rest = self.sperre_rest_s()
            text = (f"aus (frei in {rest / 60:.0f} min)"
                    if rest > 0 and hold is False else "aus")
        if on is not None and self._unavail_since is not None:
            text += ", Dose nicht lesbar"
        return {"on": on, "hold": hold, "text": text}

    # --- Thread-Seite ---------------------------------------------------
    def _poll(self):
        s = self.ha.state(self.entity)
        now = self._mono()
        if s not in ("on", "off"):
            # Nicht lesbar: Wissen behalten, nur nicht schalten.
            if self._unavail_since is None:
                self._unavail_since = now
            elif now - self._unavail_since >= UNAVAIL_WARN_S:
                self._log(f"AC-Schutz: {self.entity} seit "
                          f"{(now - self._unavail_since) / 60:.0f} min nicht "
                          f"lesbar ({s!r}) — Dose bleibt, wie sie ist")
                self._unavail_since = now      # nur alle 5 min wiederholen
            return
        self._unavail_since = None
        vorher, self.on = self.on, (s == "on")
        if self.on:
            if vorher is not True:
                # Bestaetigt EIN: der alte Anker ist Geschichte. Sonst
                # koennte er nach einem Absturz ein spaeteres, noch nicht
                # gesichertes Aus ueberdecken.
                self.off_wall = None
        elif vorher is True or (vorher is None and self.off_wall is None):
            # Ein Aus, das nicht von uns kam (Hand, Zeitplan), oder ein
            # Aus, dessen Zeitpunkt wir nach dem Start nicht kennen: gilt
            # als frisch, die Sperre laeuft ab jetzt. Das ist die sichere
            # Richtung — sonst schaltete ein Neustart die Dose sofort ein,
            # obwohl jemand sie vor einer Minute bewusst gezogen hat.
            self.off_wall = self._wall()

    def _schalte(self, an: bool) -> bool:
        self._acted = self._mono()
        if not an:
            # Sperre zaehlt ab dem BEFEHL, nicht ab der Antwort: die Tapo
            # oeffnet das Relais auch dann, wenn ihre Antwort erst nach
            # unserem Timeout kommt — und dann darf die naechste Freigabe
            # nicht sofort wieder einschalten. Blieb die Dose in Wahrheit
            # an, ist der Anker harmlos (bei EIN wird er nie befragt).
            self.off_wall = self._wall()
        code = self.ha.service("switch", "turn_on" if an else "turn_off",
                               {"entity_id": self.entity})
        if code == 200:
            if not an:
                self.on = False         # sichere Richtung: sofort glauben
            self.errors = 0
            self._log(f"AC-Schutz: Dose {'EIN' if an else 'AUS'} befohlen "
                      f"({self.entity})")
            return True
        self.errors += 1
        self._log(f"AC-Schutz: {'Ein' if an else 'Aus'}schalten ohne "
                  f"Bestaetigung (HTTP {code}), Versuch {self.errors} — "
                  f"naechste Lesung entscheidet")
        return False

    def tick(self):
        now = self._mono()
        # Erst LESEN, dann urteilen: der Regelzyklus braucht den
        # Dosenzustand vom ersten Takt an (er schweigt, solange die Dose
        # aus oder unbekannt ist), auch wenn der Waechter noch kein
        # Urteil hat.
        if now - self._polled >= POLL_S:
            self._polled = now
            self._poll()
        with self._lock:
            hold = self.hold
        if hold is None:                # der Waechter hat noch nicht geurteilt
            return
        if self.on is None or self._unavail_since is not None:
            return                      # nie bzw. gerade nicht lesbar
        if now - self._acted < ACT_MIN_S:
            return
        if hold and self.on:
            # Die DTU bestaetigt nur das EINREIHEN des Befehls, nicht die
            # Ankunft im HMS — ein per Funk verlorener Persist darf die
            # Episode nicht fuer erledigt erklaeren. Deshalb: einmal je
            # Episode, und bei jedem weiteren Aus fruehestens nach
            # PERSIST_RETRY_S noch einmal.
            if (not self._persistiert
                    or now - self._persist_mono >= PERSIST_RETRY_S):
                try:
                    self._persist()     # Limit persistent auf Minimum
                    self._persistiert = True
                    self._persist_mono = now
                except Exception as e:
                    self._log(f"AC-Schutz: Minimum persistieren "
                              f"fehlgeschlagen ({e}) — schalte trotzdem ab")
            self._schalte(False)
        elif not hold and not self.on:
            if self.sperre_rest_s() > 0:
                return
            self._schalte(True)


def start(sw: AcSwitch, log):
    def lauf():
        while True:
            try:
                sw.tick()
            except Exception as e:
                log(f"AC-Schutz: {e}")
            time.sleep(1.0)
    threading.Thread(target=lauf, name="ac-schalter", daemon=True).start()
