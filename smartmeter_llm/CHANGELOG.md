# Changelog

## 1.8.1

- GEMINI-QUOTA: Das Add-on lief in eine Dauerschleife aus HTTP 429 — alle
  ~45 s eine volle Rotation ueber alle Modelle und Keys, ~800 Fehlversuche
  pro Stunde, jeder davon eine HTTP-Anfrage mitten im 0,5-s-Regelzyklus.
- URSACHE war nicht die Rotation, sondern der Abstand des Kreuz-Checks: er
  zaehlte ZYKLEN (`CROSS_CHECK_EVERY=20`). Das ergab die dokumentierten
  "~5 min", solange ein Zyklus ~15 s dauerte. Mit `INTERVAL_S=0.5` dauert
  ein Zyklus unter einer Sekunde — aus 5 Minuten wurden 10-25 Sekunden und
  aus ~300 Calls/Tag mehrere tausend. Das Kontingent war damit vormittags
  verbraucht, und weil der Kreuz-Check den Cooldown ausdruecklich uebergeht,
  lief er danach ungebremst in die 429er.
- Der Kreuz-Check haelt jetzt einen ZEITLICHEN Mindestabstand:
  `CROSS_CHECK_S` (Standard 300 s = 288 Kreuz-Checks/Tag, wieder im
  dokumentierten Budget). Die alte Zyklen-Untergrenze bleibt zusaetzlich
  bestehen, es wird also nie haeufiger gefragt als vorher.
- QUOTA-BREMSE: Antworten `GEMINI_TRIES` (3) Kombinationen hintereinander
  mit 429, ist das kein Einzelfehler, sondern ein leeres Kontingent. Dann
  bricht die Rotation ab und pausiert — 5 min, danach verdoppelnd bis
  60 min; ein einziger Erfolg setzt alles zurueck. Waehrend der Pause geht
  keine einzige Anfrage mehr raus (auch nicht fuer den Kreuz-Check).
- Ein einzelner Aufruf verbrennt hoechstens `GEMINI_TRIES` Kombinationen
  statt aller zehn. Der Rotationsindex laeuft ueber Aufrufe hinweg weiter,
  es werden also weiterhin alle Kombinationen erreicht — nur nicht alle auf
  einmal im selben Regelzyklus.
- Am Regelverhalten aendert sich nichts: das lokale OCR ist der Primaerleser
  und liest waehrend der Pause unveraendert weiter. Gemini bleibt Berater.
- tests/test_gemini_quota.py deckt Abstand, Rotationsgrenze, Pause,
  Verdopplung, Ruecksetzung und den lokalen Weiterbetrieb ab.

## 1.8.0

- MEHR EIGENVERBRAUCH: Der Regler kannte unterhalb des Sustain-Floors nur
  zwei Antworten — Limit HALTEN (Ueberschuss ins Netz) oder Inverter
  SCHLAFEN legen (Netz zahlt). Bei 150 W Hauslast ist beides falsch: das
  eine verschenkt 275 W, das andere kauft 150 W. Genau das war das
  beobachtete Bild ("zu viel ins Netz" UND "Akku bei 50 % steht daneben").
- Die dritte Antwort steckte in den eigenen Logs: der HMS hat ein STABILES
  PLATEAU bei ~160 W. Landeplatz-Tabelle ueber 36 Tage / 96412 Kommandos
  (nur Befehle gewertet, die >= 60 s stehen blieben):
    Befehl  50 W    -> 98 % aus (37 W)
    Befehl 100-249W -> 72-83 % aus (zu tief, reisst ihn ganz ab)
    Befehl 250-399W -> 62-66 % LANDEN BEI 120-210 W (Median ~160 W)
    Befehl 400-449W -> 81 % bei 350-460 W (Median 422 W)
    ab 500 W        -> 85-97 % folgen sauber
  Das Plateau streut innerhalb einer Ruhelage nur 5,6 W und haengt NICHT an
  der Busspannung (48 V: 155 W, 51 V: 162 W, 55 V: 152 W).
- Neue Option `low_points` (Standard `300:160`): Arbeitspunkt-Leiter. Der
  Regler waehlt unterhalb des Floors den guenstigsten erreichbaren Punkt.
  Kosten = fehlende Watt (kauft das Netz) + ueberschuessige Watt
  (verschenkte Akku-Energie); bei vollem Akku entfaellt der zweite Term,
  dann wird wie bisher immer gehalten (1.7.23 bleibt gueltig). Daraus
  folgen die Schwellen von selbst: der 160-W-Punkt schlaegt den Schlaf ab
  80 W Bedarf und den Floor bis 292 W Bedarf.
- JEDER Punkt wird verifiziert: 25 s nach dem Befehl wird die AC-Leistung
  geprueft. Treffer -> die Erwartung zieht per EMA nach (Selbstkalibrierung).
  Fehlschlag -> zweiter Anlauf, danach 15 min Sperre und Rueckfall auf Floor
  oder Schlaf. Der schlechteste Fall ist damit EXAKT das alte Verhalten.
  Dazu 120 s Mindest-Standzeit und 20 W Hysterese auf der Kostendifferenz —
  Plateaus brauchen Ruhe, jedes Kommando stoert den MPPT.
- Neuer HA-Sensor "Arbeitspunkt". `low_points` leeren = alte Logik.
- NETZ-SOLLWERT FOLGT DEM LADESTAND, nicht mehr linear der Spannung. Bei
  LiFePO4 ist die Kennlinie zwischen 20 und 90 % fast flach: mit den
  Stuetzstellen 47,0/54,4 V las der Regler bei 52,7 V "77 % voll" und
  stellte das Ziel auf -34 W — Dauereinspeisung aus einem halb leeren
  Speicher. Ueber soc_estimate (lastkorrigiert) sind es 50 % und -14 W;
  20 W ueber 24 h sind ~0,5 kWh. Faellt die Schaetzung aus, gilt weiter die
  lineare Rechnung.
- ANTI-ZAPPEL auf dem Hoch-Pfad: 43 % aller "hoch"-Befehle wurden binnen
  30 s wieder zurueckgenommen (Median 68 W), und die Ausfluege ueber den
  Floor dauerten im Median 4 SEKUNDEN — kuerzer als die Totzeit des HMS.
  Diese 952 Kommandos in 7 Tagen konnten nichts bewirken, haben aber den
  MPPT aus seinem Arbeitspunkt geworfen. Jetzt muessen sich kleine Schritte
  (<= 150 W bei Fehler < 45 W) UP_CONFIRM_S (3 s) lang halten; grosse
  Lastspruenge gehen unveraendert sofort raus.
- TELEMETRIE-HERZSCHLAG: ctl_tick schrieb bisher nur rund um Limit-Befehle.
  Ausgerechnet die langen Schlafphasen (7 Tage: 61,9 h am Stueck) waren
  dadurch unbelegt — die teuerste Zeit war die unsichtbare. Jetzt geht alle
  CTL_HEARTBEAT_S (30 s) ein Tick raus (Feld "hb"), ~250 kB/Tag.
- Neue Werkzeuge:
  * `scripts/analyze_selfuse.py` — wo der Eigenverbrauch verloren geht,
    Landeplatz-Tabelle, Monte-Carlo der Ersparnis aus den echten Daten.
  * `scripts/probe_operating_points.py` — misst die Arbeitspunkte auf der
    eigenen Hardware aus (Treppe abfahren, jede Stufe stehen lassen) und
    druckt die fertige `low_points`-Zeile. Bricht ab, wenn ein zweiter
    Regler mitfunkt, und stellt das alte Limit immer wieder her.
  * `tests/test_low_points.py` — 20 Tests fuer Leiter, SoC-Ziel und
    Anti-Zappel.
- Analyse und offene Punkte (u.a.: 58 % des Netzbezugs entstehen, waehrend
  der HMS schon an seiner ~1400-W-Decke laeuft — das ist Hardware, kein
  Regelproblem): docs/eigenverbrauch.md
- Aus dem Review vor dem Merge, alle mit Test abgedeckt:
  * MPPT-KICK BEHAELT VORRANG. Klemmt der Inverter (liefert weit weniger
    als sein Limit, waehrend das Haus kauft), uebernimmt die Leiter gar
    nicht erst — sonst haette sie den Klemmwert als Arbeitspunkt
    dazugelernt und der Inverter waere unten geblieben. Ein angefangener
    Kick laeuft immer zu Ende (sonst fehlte das kick_result, aus dem die
    Eskalationstreppe kalibriert ist). Geprueft wird am ROHEN Fehler: die
    Pending-Kompensation zieht den kompensierten Fehler waehrend eines
    Kicks weit ins Negative.
  * pv_hist und up_since werden beim Wechsel in die Leiter zurueckgesetzt.
    Sonst galt eine alte Historie spaeter als "seit STUCK_S flach" (Kick
    nach 5 s statt 25 s) und ein alter Zeitstempel als bereits bestaetigt.
  * Anti-Zappel misst am FEHLER statt an der Schrittweite. Wegen
    wanted = PV + Fehler ist wanted - Limit immer <= Fehler, die
    Schrittbedingung war also nie bindend — und liess genau die
    68-W-Zappler durch, um die es geht.
  * Ladestand fuers Ziel wird ueber 5 min geglaettet: er haengt ueber die
    Lastkorrektur an der Ausgangsleistung, ohne Glaettung wanderte das
    Ziel im Sekundentakt mit der Last (schwache Mitkopplung).
  * Herzschlag-Ticks wurden doppelt geschrieben (sofort UND spaeter aus
    dem Ringpuffer). Sie sind jetzt mit "hb" markiert und werden beim
    Nachschreiben uebersprungen.
  * probe_operating_points bricht ab, wenn das aktuelle Limit nicht
    auslesbar ist (sonst waere der Inverter am Ende auf der letzten
    Messstufe stehen geblieben) — oder nimmt --restore <Watt>; der
    Restore versucht es jetzt dreimal.

## 1.7.43

- DER DEYE IST REGELBAR. Register 0x0028 („Active Power Regulation",
  0-100 %) laesst sich schreiben — aber NUR ueber den AT-Kanal:
  `AT+INVDATA=<laenge>,<modbus-hex>` auf Port 8899, mit Slave 01.
  Modbus ueber Solarman V5 und ueber die rohe Bruecke im
  throughput-Modus werden beide stillschweigend ignoriert (Funktion
  0x06 wie 0x10, Slave 1 wie 0xAA).
- Fallstrick, der mich zuerst zur falschen Schlussfolgerung fuehrte:
  Mehrere AT-Kommandos auf EINER Verbindung liefern die Antworten um
  einen Befehl VERSETZT zurueck. Deshalb: ein Kommando pro Verbindung.
  Der von Community-Werkzeugen genutzte Port 48899 ist auf dieser
  Firmware geschlossen — der AT-Kanal auf 8899 aber nicht.
- Neue HA-Entitaet **„Deye Leistungsbegrenzung"** (number, 0-100 %):
  Schieberegler, der direkt auf den Wechselrichter durchschlaegt.
  Gelesen wird der Istwert mit, gesetzt ueber `<topic>/deye_limit/set`.
- Gemessen: Der Registerwert steht sofort, die LEISTUNG folgt nach
  2-3 Minuten (82 W -> 55 W bei 5 % Limit). Fuer eine Regelung heisst
  das: langsamer aeusserer Kreis fuer den Deye, waehrend der HMS die
  schnelle Feinregelung uebernimmt.
- Damit ist der Deye grundsaetzlich auch am Akku einsetzbar — die
  Regelbarkeit war die offene Bedingung dafuer.

## 1.7.42

- BUGFIX (wichtig): Die V5-Antworten wurden nicht der Anfrage zugeordnet.
  Der Logger liefert bei schnell aufeinanderfolgenden Verbindungen die
  Antwort der VORHERIGEN Abfrage — nachgewiesen, als das Limit-Register
  den Leistungswert meldete (2680 statt 100). Jetzt wird das Echo-Byte
  geprueft (V5 echot die Anfrage-Sequenz im UNTEREN Byte, das obere ist
  der eigene Zaehler des Loggers).
- Modusunabhaengiges Lesen: erst Solarman V5 (Logger-Modus `cmd`,
  Slave 1), sonst rohes Modbus RTU (Modus `throughput`, Slave 0xAA),
  sonst HTML-Statusseite. Neue Option `deye_slave` (Standard 170).
- Auf dem rohen Bus ermittelt: Der SUN600G3 antwortet auf Modbus-Adresse
  **0xAA (170)**, nicht auf 1. Im throughput-Modus liegt ausserdem der
  Eigenverkehr des Loggers auf dem Bus, deshalb wird jede Antwort per
  Modbus-CRC validiert — ohne das wurden fremde Frames als eigene
  Lesung uebernommen (0-Werte, Phantom-50,00 Hz).
- ERGEBNIS DER MESSREIHE: Auch im throughput-Modus mit direktem
  Buszugriff aktualisieren sich die Werte NICHT schneller (50,00 Hz und
  172,0 W ueber 70 s CRC-validiert eingefroren). Der Engpass liegt also
  nicht im Logger-Cache, sondern tiefer. Schnelleres Pollen bringt
  nichts — der Regler bleibt bei der Netzleistung als Echtzeitquelle.

## 1.7.41

- Deye-Auslesung laeuft jetzt primaer ueber MODBUS (Solarman V5, Port
  8899) statt HTML-Scraping; die Statusseite bleibt als Fallback. Neue
  Option `deye_logger_sn` (Seriennummer des WLAN-Loggers) schaltet den
  Modbus-Pfad frei. Ein Request holt alle drei Werte (0x003C..0x0056).
- Grund: Der Logger hat einen versteckten transparenten Modus
  (`yz_tmode=throughput` unter /hide_set_edit.html). Darin cached er
  nicht mehr, sondern wird zur Seriell-zu-TCP-Bruecke — Modbus geht dann
  DIREKT an den Wechselrichter und ist echtzeitfaehig (DEYE_POLL_S=5).
  In diesem Modus liefert die HTML-Statusseite keine Werte mehr, deshalb
  muss Modbus der primaere Pfad sein. Umschaltung und Restore-Werte sind
  in docs/EINSTELLUNGEN.md dokumentiert.
- Cache-Nachweis verschaerft: die Netzfrequenz (0x004F) stand eine Minute
  lang bitgenau auf 49,89 Hz — im echten Verbundnetz unmoeglich. Damit
  ist belegt, dass auch Modbus im Modus `cmd` nur den Cache liest.
- Vollstaendige Registerkarte des SUN600G3 gesichert (182 Register):
  0x0010 Nennleistung 6000 (=600,0 W), 0x0028 Wirkleistungsbegrenzung
  in Prozent (steht auf 100), 0x003C Ertrag heute, 0x003E/0x003F Ertrag
  gesamt (32 bit), 0x0049 Netzspannung, 0x004F Frequenz, 0x0056
  AC-Leistung, 0x0096-0x0099 VDE-Netzschutzgrenzen (275,0/183,0 V,
  51,50/47,50 Hz — duerfen NIE veraendert werden).
- Das AT-Interface auf Port 8899 ist eine Sackgasse: es antwortet auf
  jedes Kommando mit V5-Binaerframes, `AT+YZCMPVER=...` beim Verbinden
  ist nur ein Begruessungsbanner.

## 1.7.40

- ZWEITE QUELLE SICHTBAR: Der Deye-Balkonwechselrichter (SUN600G3) wird
  jetzt lokal ausgelesen — ohne Cloud, ueber die Statusseite des
  Solarman-WLAN-Loggers. Neue Sensoren: Deye Leistung, Ertrag heute,
  Ertrag gesamt (letzterer als Solarquelle fuers Energie-Dashboard
  geeignet). Aktivierung ueber `deye_host` (leer = aus).
- Der Poll laeuft in einem eigenen Thread: eine haengende Anfrage am
  Logger darf den 0,5s-Regelzyklus niemals aufhalten. Antwortet der
  Logger 10 Poll-Intervalle lang nicht, wird nichts gemeldet statt ein
  Altwert.
- GEMESSEN und dokumentiert: Der Logger fragt den Wechselrichter intern
  nur alle ~5 Minuten ab (57 Proben im 5s-Takt = genau eine Aenderung).
  Die Web-Statusseite und das Modbus-Interface (Solarman V5, Port 8899)
  liefern nachweislich denselben Cache-Wert — Modbus ist also NICHT
  schneller, und schnelleres Pollen erzeugt nur Fehlversuche auf einer
  WLAN-Strecke mit ~10% Aussetzern. Deshalb fliesst der Wert bewusst
  NICHT in die Regelung ein: die sieht die Deye-Leistung ohnehin sofort
  in der gemessenen Netzleistung, und ein 5 Minuten alter Wert im
  0,5s-Regelkreis waere aktiv schaedlich.
- Register-Karte des SUN600G3 nebenbei ermittelt (fuer spaetere Nutzung):
  0x003C Ertrag heute (x0,1 kWh), 0x003F Ertrag gesamt (x0,1 kWh),
  0x0056 AC-Leistung (x0,1 W), 0x0049 Netzspannung (x0,1 V),
  0x004F Frequenz (x0,01 Hz), 0x003B-0x0040 Seriennummer als ASCII.

## 1.7.39

- LETZTE ANGRIFFSRUNDE (5, gegen 1.7.38): 16 Befunde, 12 real — alle
  gefixt bzw. dokumentiert. Die Gutfall-Matrix meldet: alle 11
  Standard-Szenarien unter 1 h, keine Invarianten-Verletzung.
- Ankerloser Re-Baseline (Stand verloren) bekam den fehlenden
  AUFWAERTS-Anker: nach >= 6 h ohne akzeptierte Lesung (leeres
  Physik-Fenster) setzten 2 Gemini-Bestaetigungen jeden Stand bis
  99999 (+64108 im Repro) — jetzt gilt derselbe Deckel wie im
  Notausweg (letzter Stand/Boden + Physik seit der aelteren Uhr).
- Breite Aufwaerts-Basis (> 25 kWh ueberm Boden) braucht einen
  Zeugen: Gemini exakt, oder bei totem Gemini Segment-Marge >= 0,8
  plus 4 Lesungen ueber >= 5 min (statt 2 zeugenloser Frames in
  einem bis zu 1800 kWh breiten Fenster).
- Basis-Senkungszweig hat jetzt Zeugen-Trennung (war der einzige
  Heilpfad, in dem Gemini seinen eigenen Kandidaten bestaetigen
  konnte: -1 je Watchdog-Freigabe, -40 kWh in 10 h).
- SENKUNGS-BUDGET: hoechstens 3 kWh Heilung nach unten je 24 h —
  auch ein systematisch mitluegender Zeuge kann keine -1-Ratsche
  mehr fahren (-30 kWh in 1,75 h im Repro).
- Gemini-only-Betrieb (kein lokales OCR) bleibt heilbar: die
  Zeugen-Trennung gilt nur, wenn es zwei Quellen gibt — sonst fror
  jede Luecke >= 2 kWh den Kanal permanent ein.
- Notausweg-Uhr gegen Uhrenspruenge gehaertet (zusaetzlich >= 20
  reale Fehlversuche noetig); Korruptur-Heilung behaelt kwh_hist
  und kwh_ts; Plateau-Anker alle 5 min statt 20; Warnung bei
  INTERVAL_S > 60 s (Dauerlast > 20 kWh/h ueberholt sonst den Leser).
- Dokumentiert (EINSTELLUNGEN.md): Zaehlerwechsel / festgefahrener
  Stand — state.json loeschen ist der einzige vorgesehene Weg nach
  unten; HA-Statistik danach manuell korrigieren.
- Tests: 42 gruen (5 neue Runde-5-Replays), 48h-Fuzz ueber 9 Seeds.
  Damit sind ueber 5 Runden 52 verifizierte Befunde geschlossen und
  als Regressionstests verewigt.

## 1.7.38

- FINALE KONVERGENZ-RUNDE (gegen 1.7.37): 18 Befunde, 13 real — alle
  gefixt. Gemeinsame Wurzel fast aller Treffer: ZEIT-ANKER UND UHREN,
  DIE NEUSTARTS NICHT UEBERLEBEN.
- kwh_lost bindet jetzt BEIDE Pfade: der ankerlose Re-Baseline kannte
  die Monotonie-Schranke nicht — kNN und Gemini verlieren im Schatten
  dieselbe letzte Ziffer (35891 -> 3589), zwei Gemini-Bestaetigungen
  desselben Fehlers senkten den Stand um -32302. Unter den letzten
  echten Stand minus 1 geht es NIE, mit keinem Zeugen der Welt.
- Plateau-Anker: kwh_hist setzt auch bei unveraendertem Wert alle
  20 min einen frischen Eintrag; die erste Schleife persistiert sofort
  (last_write_ts=0) und SIGTERM sichert den State. Vorher alterte auf
  einem Zaehler-Plateau (Sonnentag) + Neustart-Sturm der Platten-ts
  unbegrenzt und oeffnete den Deckel (+100 kWh nach 4 h Sturm).
- Notausweg-Uhr (_gemini_err_since/_gemini_ok_ts), cycle und seg_warn
  werden persistiert: jeder Neustart < 6 h schloss sonst den Deadlock-
  Ausweg fuer immer, und die Watchdog-Reifung (~14 min Laufzeit)
  begann je Prozess bei Null.
- Zeugen-Bestaetigungsfenster 30 min -> 6 h: Quota-Tage liefern
  Gemini-Erfolge > 30 min auseinander — die 2. Bestaetigung kam nie
  ins Fenster, ein sporadischer Gemini heilte weiterhin nie. Der
  Kandidat muss dank neuem Zaehler-Verfall trotzdem durchgehend
  gelesen sein: rb_counts und kwh_pend verfallen jetzt (15/10 min) —
  4 ueber Tage verstreute Geisterframes sind kein Konsens mehr.
- Riesenspruenge (> 25 kWh = 1 h Physik) brauchen ZWEI exakte
  Bestaetigungen — der 72h-Blindflug-Deckel erlaubt bis +1800 in
  einem Schritt, so ein Schritt bekommt keinen Einzel-Zeugen mehr.
- Basis-Fenster/Notausweg rechnen ab der AELTEREN Uhr (Freigabe ODER
  letzte akzeptierte Lesung): ein echter 72h-Ausfall vor der Freigabe
  war vergessen, die Heilung hing 4,2 h — jetzt Minuten.
- Notausweg-Segment-Marge 1,0 -> 0,8 (die kalibrierte REJECT_MARGIN):
  eine schwache, aber richtige Stuetze blockierte die Heilung ewig.
- Zukunfts-Eintraege in kwh_hist (NTP-Rueckwaertskorrektur) verfallen
  jetzt; Physik-Fenster prueft vor dem Re-Baseline (keine verbrannte
  Quota fuer Kandidaten, die das Fenster ohnehin verwirft); stale
  Konsens-Reste werden aufgeraeumt (kein ewiger 30s-Persist-Takt).
- Tests: 37 gruen, alle Runde-4-Angriffe und -Gutfaelle als Replays,
  48h-Fuzz mit Neustarts ueber 9 Seeds.

## 1.7.37

- 2. ANGRIFFSRUNDE (gegen 1.7.36, inkl. "Anwalt des Gutfalls"): 16
  Befunde, 13 real bestaetigt — alle 13 gefixt. Die wichtigste Lehre:
  Deckel duerfen nur PHYSIK kodieren, nie Gewohnheit.
- Physik-Rate 5 -> 25 kWh/h (Hausanschluss 3x35A), Fenster prueft
  weiter kumulativ: der alte 5er-Deckel (aus p99=2,2 abgeleitet) fror
  bei einer 11-kW-Wallbox-Nachtladung die Regelung 5,9 h im Failsafe
  ein — ausgerechnet in der teuersten Stunde. 358914 braeuchte auch
  mit 25 noch 538 Tage.
- Physik-Fenster prueft jetzt VOR allen Seiteneffekten: ein vom
  Fenster verworfener Frame hatte vorher bereits Boden/kwh_lost
  gepoppt, rb_counts geleert und Gemini-Aufrufe verbrannt — danach
  war der Stand zeugenlos frei setzbar.
- Notausweg (local_escape) mit harten Grenzen: nie unter kwh_lost-1
  (nur ein rein abgeleiteter Korruptur-Boden darf unterschritten
  werden), Physik-Deckel gilt auch hier, Zaehler verfallen nach
  30 min Pause, und "Gemini tot" heisst jetzt ">= 6 h ohne einen
  einzigen ERFOLG" (vorher setzte jede sporadische Antwort die Uhr
  zurueck). Er war sonst eine zeugenlose Tuer: -800/-32302 und
  +4000/+60000 kWh im Repro.
- Flakiger Gemini heilt wieder: Zeugen-Bestaetigungen akkumulieren
  ueber Zyklen (30-min-Fenster), Zeugen-FEHLER loeschen den
  4x/180s-Konsens nicht mehr (nur ein WIDERSPRUCH tut das). Vorher
  mussten 2 Bestaetigungen im selben Zyklus fallen — ein sporadisch
  erreichbarer Gemini heilte NIE, schlechter als ein ganz toter.
- Konsens-Zaehler (rb_counts, base_pend, esc_counts, rb_confirm)
  werden persistiert: Neustarts alle < 3 min (Supervisor-Watchdog-
  Schleife) machten jede Ausfall-Heilung strukturell unmoeglich.
- state.json atomar (tmp + fsync + os.replace): ein SIGTERM mitten im
  Write hinterliess leeres JSON und loeschte Boden + Fenster still.
- ts-Klemme entfernt (sie ERZEUGTE ein Loch: nachgehende Boot-Uhr +
  NTP-Sprung -> Deckel +360 kWh offen); stattdessen werden Zeit-
  ABSTAENDE an der Verwendungsstelle auf >= 0 geklemmt — immer die
  strengere Richtung.
- W-Kanal regelt bei kWh-Veto weiter (w_salvage): ein blockierter
  Zaehlerstand ist kein blinder Zaehler — vorher fiel die Regelung
  bei jedem kWh-Tor-Stau in den Failsafe (Wallbox: 5,9 h bei 200 W).
- Tests: 30 gruen, alle Runde-3-Angriffe als Replays + Gutfall-
  Regressionen (Wallbox 11 kW, flakiger Gemini, Neustart-Sturm,
  NTP-Sprung), 48h-Fuzz mit Neustarts ueber 9 Seeds.

## 1.7.36

- ADVERSARIALER ANGRIFF AUF 1.7.35 (32 Agenten, 3 Angreifer + Einzel-
  Verifikation): 29 Befunde, 14 real bestaetigt — alle 14 gefixt:
- Boden ueberlebt Neustarts: state.json persistiert jetzt Boden,
  verlorenen Stand (kwh_lost) und das 6h-Physik-Fenster. Vorher
  loeschte ein Neustart nach Watchdog-Freigabe den Boden ({"kwh": null}
  -> leerer State) und der Stand war mit ZWEI Lesungen frei waehlbar.
- Kumulatives 6h-Physik-Fenster (kwh_hist): jeder Anstieg muss gegen
  JEDEN akzeptierten Stand der letzten 6 h unter Rate x Zeit + 1
  bleiben. Toetet die +3-Ratsche (60 kWh/h trotz Einzel-Deckel) und
  die Watchdog-Pumpe. Einzel-Deckel ohne konstanten Schlupf (+2 weg);
  Seg-Heilpfad ohne Cloud nur noch fuer +1.
- Zeugen-Reinheit: gemini_normalize ENTFERNT — es wusch 6-stelligen
  Geistermuell in gueltige Staende (585870 -> 58587) und entzog dem
  Struktur-Deckel seine Faelle. Die Nachkomma-Signatur zaehlt nur noch
  beim Zeugen-VERGLEICH (witness_match: 358914 bestaetigt exakt 35891).
- Kanaltrennung dicht: W-Gruende ("Sprung +8675 W") stossen den
  kWh-Re-Baseline nicht mehr an (Teilstring-Falle); nach Seg-Arbiter
  und Re-Baseline wird die W-Pruefung des Frames NACHGEHOLT (der
  kWh-Kurzschluss in plausible() hatte sie verdeckt).
- Seg-Arbiter respektiert den Struktur-Deckel (bei Stand 99999 kein
  100000 mehr moeglich — war der einzige I3-Bypass).
- Watchdog-Tuer: Senkungen unter den verlorenen Stand brauchen auch
  im Basis-Fenster den Gemini-Zeugen; Fenster ohne +2-Schlupf
  ([Stand-1, Stand+1] frisch). Watchdog laeuft nicht mehr ohne
  lokalen Leser (gab sonst im Gemini-Modus alle 600 Zyklen frei).
- Deadlock geloest: vergifteter Boden + Gemini >= 6 h DURCHGEHEND
  ausgefallen (Widerspruch zaehlt nicht) -> enger lokaler Notausweg
  (4x ueber 10 min + deutliche Segment-Marge, local_escape-Event).
  Vorher: 72 h Failsafe trotz einiger lokaler Verfahren.
- Zeitstempel plausibilisiert: Zukunft -> geklemmt, Blindflug-Deckel
  72 h, Persist alle 15 min (ts war sonst der letzte ZAEHLER-TICK und
  blaehte den Deckel nach Neustart um Stunden auf).
- Kaltstart (kein Anker): 4 Lesungen ueber 60 s je Kandidat;
  Basis-/Kandidaten-Zaehler je Wert (strenge Alternation 35891/35892
  blockierte sonst fuer immer).
- Tests: 24 Tests, ALLE 14 Angriffe als Replays, strikte Invarianten
  (I2 ohne Nachbildung der Produktiv-Formel — die alte Assertion hatte
  den +2-Fehler geerbt und war blind), 48h-Fuzz mit Neustarts ueber
  9 Seeds, Legitimitaets-Regression (echtes Ticken blockiert nie).

## 1.7.35

- DER 358914-VORFALL (28.07. ~09:40): Der Stand sprang von 35891 auf
  358914 (+323.023 kWh). Ursache-Forensik: Gemini liest die
  NACHKOMMASTELLE der kWh-Zeile mit (35891.4 -> "358914", im Takt der
  Zehntel hochtickend: 358911/358913/358914). Wenn das lokale OCR kurz
  unlesbar war, wurde Geminis Fehllesung selbst zum Kandidaten — und im
  Re-Baseline hat dann GEMINI GEMINI bestaetigt. Aufwaerts gab es keine
  Schranke ("Ausfall-Heilung"), also wurde +323.023 akzeptiert.
- STRUKTUR-DECKEL (KWH_ABS_MAX=99999): Das Display hat 6 Vorkomma-
  Stellen mit fuehrender Null — ein Stand > 99999 ist strukturell
  unmoeglich und wird ueberall verworfen, egal wie viele Zeugen ihn
  bestaetigen. Faengt die ganze Klasse (358914, 585870, 880080).
- ZEUGEN-TRENNUNG: Nur lokale OCR-Lesungen duerfen Re-Baseline-
  Kandidaten werden; Gemini bleibt reiner Zeuge und bestaetigt nie
  wieder sich selbst. Bei OCR-Abweichung gewinnt ausserdem die
  KONFIDENTE lokale Lesung — Gemini ueberstimmt sie nicht mehr.
- PHYSIK-DECKEL AUFWAERTS: Re-Baseline nach oben ist auf (Zeit seit
  letzter akzeptierter Lesung) x 5 kWh/h begrenzt (gemessen p99:
  2,2 kWh/h). Echte Ausfall-Heilung skaliert mit (10 h -> +52 kWh
  erlaubt), ein Geistersprung nie. Dafuer wird der Zeitstempel jetzt
  in state.json mitpersistiert.
- WIR HABEN ZEIT: Re-Baseline-Kandidaten muessen >= 4x konsistent UEBER
  >= 3 MINUTEN gelesen werden, bevor ueberhaupt ein Zeuge gefragt wird.
  Gemini-Bestaetigung nur noch EXAKT (vorher +-2). Ohne Anker (Stand
  verloren) braucht es ZWEI exakte Bestaetigungen auf frischen Snapshots.
  Frische Basis nach Stand-Verlust: Fenster [Boden, Physik-Deckel] und
  zwei uebereinstimmende Lesungen.
- SELBSTHEILUNG DES VERGIFTETEN STANDS: Beim Start wird ein strukturell
  unmoeglicher state.json-Stand (358914) verworfen und 35891 als Boden
  gesetzt — die Kamera stellt den echten Stand nach dem Update
  automatisch wieder her. Kein Handgriff noetig.
- Gemini-Nachkommastellen-Fix (358914 -> 35891) + Prompt-Verbot der
  Nachkommastelle; 8er-Segmenttest bleibt als 888888 erkennbar.
- ALLE kWh-Tore in guard_kwh() gebuendelt und mit tests/test_kwh_gates.py
  abgedeckt: Replays aller Vorfaelle (26.07., 28.07. frueh, 28.07.
  358914) + 48h-Adversarial-Fuzz ueber 9 Seeds, in dem Gemini und
  Segment-Dekoder JEDEN Muell bestaetigen — die Invarianten (nie -1
  ohne exakten Gemini, nie mehr als +1 bzw. Physik-Deckel, nie >99999,
  nie Selbstbestaetigung, nie ohne 3-Minuten-Konsistenz) halten.
- 226 vergiftete 35850-Segmentlabels (07:00-09:11) in Quarantaene.

## 1.7.34

- Heilspielraum von 2 auf 1 kWh verengt (KWH_HEAL_MAX=1). Begruendung:
  ein Aufwaertsschritt braucht zwei konsistente Lesungen von exakt
  Stand+1, eine Geisterlesung kann den Stand also hoechstens um +1
  vergiften — mehr Spielraum nach unten braucht es nie. Senkungen um 1
  weiterhin nur mit Gemini-Bestaetigung; alles darueber bleibt absolut
  verboten. Greift die -1-Heilung je faelschlich, korrigiert der normale
  +1-Pfad binnen Minuten, weil der echte Zaehler dann +1 voraus ist

## 1.7.33

- MONOTONIE-INVARIANTE, endgueltig: Der Zaehlerstand kann nie wieder um
  mehr als KWH_HEAL_MAX (2 kWh) sinken — egal wie viele Zeugen das Bild
  bestaetigen. Am 28.07. 07:04 fiel der Stand von 35890 auf 35850: der
  Morgenschatten loescht Segment B der 9 physisch aus, kNN UND Segment-
  Dekoder lasen deshalb uebereinstimmend 35850, und der Seg-Heilpfad aus
  1.7.25 oeffnete die Tuer. Lehre: dieselbe Optik ist kein unabhaengiger
  Zeuge; der einzige unbestechliche Zeuge ist die Physik.
- Vier Schichten: (1) Senkungen > 2 kWh sind in rebaseline hart verboten
  (monotonic_veto-Event); (2) Senkungen <= 2 kWh brauchen zwingend Gemini
  als bild-fremden Zeugen — der Segment-Dekoder darf Senkungen nicht mehr
  allein freigeben; (3) die Watchdog-Freigabe hinterlaesst einen Boden,
  unter dem keine neue Basis akzeptiert wird; (4) Notbremse direkt vor dem
  State-Update, falls je wieder ein Pfad an der Plausibilitaet vorbeifuehrt.
- Aufwaerts-Heilung bleibt voll funktionsfaehig: der aktuell vergiftete
  Stand (35850) korrigiert sich nach dem Update binnen Sekunden selbst
  auf den echten Zaehlerstand. 6 Szenarien getestet, inkl. "Gemini luegt mit".

## 1.7.32

- Beim Start stehen die tatsaechlich wirksamen Akku-Schwellen im Log
  (Abschalt- und Freigabespannung, auch je Zelle) sowie Netz-Ziel, Floor
  und Maximallimit. Ohne das war nicht erkennbar, dass batt_low_v noch auf
  dem Default 51,2 V stand, obwohl 47 V gewollt waren — der Waechter
  schaltete dadurch schon bei einem normalen Lastsprung ab (26.07. 21:29:
  781 W Limit, Bus sackt unter 51,2 V, Abschaltung auf 50 W)

## 1.7.31

- Neuer HA-Sensor "Akku-Ladestand (geschaetzt)": rechnet die Packspannung
  ueber die LiFePO4-Ruhespannungskennlinie in Prozent um und korrigiert
  dabei den Spannungsabfall unter Last (Strom aus der Inverter-Leistung,
  Innenwiderstand ueber BATT_RI_MOHM, Default 10 mOhm). Mit
  batt_capacity_kwh kommt zusaetzlich ein kWh-Sensor dazu
- Der Wert ist ausdruecklich eine Schaetzung: zwischen 20 und 90 % ist die
  Kennlinie fast flach, dort liegen 50 % der Kapazitaet in rund 1,3 V
  Packspannung. Exakt wird es erst mit dem Coulomb-Zaehler des BMS.
  Interaktive Kennlinie: docs/lifepo4-soc.html

## 1.7.30

- DAS eigentliche Loch hinter dem 26.07.-Kollaps: die Plausibilitaets-
  pruefung lief als lineare Kette mit fruehen returns, und der W-Heilpfad
  gab bei Erfolg direkt None zurueck — womit die kWh-Pruefung schlicht
  UEBERSPRUNGEN wurde. Ablauf: W flatterte wegen einer 3->9-Fehllesung
  (+6000 W), nach vier konsistenten Lesungen griff die W-Re-Baseline, und
  im selben Atemzug rutschte ein um 20 kWh zu niedriger Zaehlerstand
  ungeprueft durch. In drei Stufen: 35881 -> 35861 -> 35801. Weder die
  Rueckwaerts-Sperre noch MAX_KWH_STEP wurden dabei je ausgewertet.
- plausible() ist jetzt in zwei unabhaengige Kanaele getrennt
  (_plausible_kwh und _plausible_w). Ein Heilpfad kann immer nur seinen
  EIGENEN Kanal freigeben; beide muessen zustimmen, damit eine Lesung
  akzeptiert wird

## 1.7.29

- Akku-Spannung wird pro Regelzyklus in die Telemetrie geschrieben (Feld
  "bv"), auch ohne aktiven Waechter. Damit laesst sich die Entladekurve
  einer Nacht auswerten und unterscheiden, ob der Speicher schlicht nicht
  voll war oder eine Zelle einbricht
- Floor-Entscheidung geglaettet: Schlafen/Halten wird jetzt am Median der
  letzten 12 s entschieden statt am Momentanwert. Die Hauslast zappelt
  (26.07. 03:48-03:49: 180 W <-> 266 W im Sekundentakt) und lief dabei
  staendig ueber beide Schwellen — daraus wurden drei Limit-Wechsel in 25
  Sekunden. Replay derselben Lastfolge: 1 statt 6 Wechsel

## 1.7.28

- Klemm-Erkennung richtig gestellt: 1.7.27 ging von einem wackelnden
  Attraktor aus — die Rohdaten zeigen das Gegenteil. Im Attraktor steht
  die Leistung wie festgenagelt (26.07. 01:21-01:29: pv = 178,3 W ueber
  7,5 Minuten, Schwankung 0,2 W), waehrend echtes Nachfuehren staendig
  zappelt. Erkannt wird jetzt genau das: bleibt die Leistung STUCK_S lang
  innerhalb von 8 W und mehr als STUCK_GAP_W unter dem Limit, ist er
  geklemmt. Die alte Logik (Fortschritt seit Fensterbeginn) verrechnete
  das Einschwingen VOR dem Klemmen als Fortschritt und feuerte deshalb
  nie. Replay der echten Episode: Kick nach 27 s; normales Nachfuehren
  loest keinen Fehlalarm aus

## 1.7.27

- Klemm-Erkennung angefasst (Annahme war falsch, siehe 1.7.28)

## 1.7.26

- Gemini war seit dem Wechsel auf die 3.x-Modelle KOMPLETT TOT (HTTP 400):
  die neuen Modelle lehnen thinkingConfig/thinkingBudget=0 ab. Damit fehlte
  der einzige vom lokalen OCR unabhaengige Zeuge — das ist der Grund, warum
  der falsche Zaehlerstand am 26.07. stundenlang unentdeckt blieb. Ohne den
  Parameter liest Gemini den Frame sofort korrekt. Zusaetzlich: HTTP 400
  loest jetzt einen Retry ohne generationConfig aus und rotiert danach,
  statt den Aufruf hart abzubrechen
- Die 9000-W-Lesungen aufgeklaert: echte Werte 3075/3143/3078 W, das kNN
  las die fuehrende 3 als 9 (Gemini und Segment-Dekoder waren sich einig).
  Frames korrekt gelabelt und nachtrainiert — liest jetzt 3075/3143/3078
- W-Re-Baseline verlangt eine zweite Meinung: vier konsistente Lesungen
  allein reichen nicht, denn ein systematischer Lesefehler IST konsistent.
  Erst Gemini, sonst der Segment-Dekoder muss das neue Niveau bestaetigen

## 1.7.25

- KRITISCH: Der Zaehlerstand konnte sich still vergiften und NICHT mehr
  heilen (26.07.): das kNN las 35881 konstant als 35801 (Ziffer 8 -> 0 an
  Slot 4) mit Confidence 0,91 — also ohne Gemini-Rueckfrage. Weil die
  Fehllesung konsistent war, gab es kein Fehlersignal.
- Der Schiedsrichter machte es schlimmer: score_candidates konnte nur
  zwischen den vorgelegten Kandidaten waehlen und hatte kein "keiner von
  beiden". Auf die Frage [35801, 35802] antwortete er brav 35801 und
  zementierte damit den falschen Stand. Zusaetzlich verglich er nur die
  ABWEICHENDEN Stellen — bei 35801/35802 also nur die letzte Ziffer,
  waehrend der falsche gemeinsame Praefix unsichtbar blieb.
  Jetzt: alle sechs Stellen werden geprueft und gegen die ungebundene
  Lesung gehalten; passt keiner der Kandidaten (Abstand > 0,8 in
  Log-Likelihood), wird abgelehnt. An 180 Frames kalibriert: 99,4% der
  falschen Fenster abgelehnt, 0,6% Fehlalarm.
- Lokaler Heilpfad ohne Cloud: widerlegt der Segment-Dekoder den
  gespeicherten Stand UND bestaetigt den neuen, wird re-baselined — auch
  wenn Gemini ausfaellt (was am 26.07. gleichzeitig passierte)
- Segment-Watchdog: alle 200 Zyklen prueft der unabhaengige Dekoder, ob
  das Bild den akzeptierten Stand ueberhaupt stuetzt. Drei Widersprueche
  in Folge geben den Stand frei — gegen genau den stillen Dauerfehler
- Modell auf den korrigierten Frames nachtrainiert; 3 vergiftete Labels
  (35801) quarantaeniert

## 1.7.24

- KRITISCH: Akku-Waechter klemmte das Limit dauerhaft auf 50 W, waehrend
  das Haus Netz zog (25.07. 08:43). Zwei Ursachen, beide erst mit dem
  Voll-Akku-Aufbau wirksam:
  1. Er loeste ohne Entprellung aus — unter Last (20 A) sackt der Bus
     kurz ein, das reichte zum Abschalten trotz vollem Akku
  2. Er kam nie wieder heraus: der Cap wurde als (pv - Akku-Anteil)
     gerechnet, was bei ausschliesslich akkugespeisten Strings ~0 ergibt,
     und die Freigabe haing an batt_high_v (54,4 V) — mit gedrosseltem
     Inverter praktisch unerreichbar
- Waechter neu geschrieben: Spannung muss BATT_TRIP_S (15 s) unter
  batt_low_v liegen, dann wird der Inverter abgeschaltet; Freigabe bei
  batt_low_v + BATT_RECOVER_V (1,5 V), BATT_RELEASE_S gehalten. Die
  "Sonnen-Probe" ist entfallen — sie ergab nur Sinn, solange Solar direkt
  am Inverter haengen sollte
- Leere Inverter-Eingaenge (Spannung ~0) werden ignoriert: stand ein
  unbelegter String in batt_strings, zwang seine 0,6 V den Waechter
  dauerhaft in den Schutz

## 1.7.23

- Sustain-Floor entscheidet jetzt zwischen HALTEN und ABSCHALTEN. Speist
  eine zweite Quelle ein (Deye, viel Sonne), sinkt der Bedarf am HMS unter
  den Floor — Halten schickt dann Akku-Energie ins Netz. Kosten sind
  (Floor - Bedarf) beim Halten gegen (Bedarf) beim Abschalten, der
  Kipppunkt liegt exakt bei Floor/2 (215 W). Darunter wird der Inverter
  schlafen gelegt, darueber gehalten; Hysterese-Band bis 0,6*Floor gegen
  Flattern. Bei vollem Akku wird immer gehalten (Ueberschuss waere ohnehin
  abgeregelt, Einspeisen kostet dann nichts)

## 1.7.22

- Akku-abhaengiger Netz-Sollwert: das Ziel wandert linear mit der
  Akku-Spannung zwischen target_grid_w (leerer Akku, Default +20 W —
  lieber ein paar Watt ziehen als den Speicher verheizen) und
  target_grid_full_w (voller Akku, Default -50 W — dann darf ruhig etwas
  ins Netz laufen, der Ueberschuss waere sonst ohnehin abgeregelt).
  Stuetzstellen sind batt_low_v/batt_high_v; ohne konfigurierten Akku
  gilt target_grid_w unveraendert

## 1.7.21

- Sustain-Floor (sustain_floor_w, Default 430W): Ziele unterhalb der
  ansteuerbaren Grenze werden nicht mehr angesteuert. Messung an 929
  Limit-Kommandos: der HMS folgt einem Limit unter 500W nur zu 25-90%
  (ab 500W: 99,7%) — er faellt beim Kommando in einen Attraktor bei
  ~157W. Halten kann er niedrige Leistung sehr wohl (Plateaus bei 157W,
  320W, 424W ueber Minuten), er findet nur nicht per Kommando dorthin.
  Nachts jagte der Regler deshalb ein unerreichbares Ziel (Last ~390W)
  und warf den Inverter mit 1667 Limitwechseln in 5,2h staendig aus dem
  Tritt. Simulation ueber die echte Lastkurve: Netzbezug 1,65 -> 0,32
  kWh/Nacht. Der Akku-Waechter behaelt Vorrang (leerer Akku schlaegt
  Ueberschuss-Einspeisung); 0 schaltet den Floor ab

## 1.7.20

- MPPT-Kick datenbasiert beschleunigt: Auswertung von 160 kick_result-
  Events (111 echte Aufwacher) zeigt, dass der Inverter bei ~157W median
  schlaeft und die noetige Sprunghoehe unabhaengig von Basis-Limit und
  Schlaftiefe ist. +100W weckt nur 48%, +400W aber 94% beim ersten
  Versuch. Kick-Treppe von (100,200,400,800) auf (400,800) verkuerzt —
  spart ~20-30s pro Aufwachen, der kurze Puls ist mit Akku folgenlos

## 1.7.19

- FIX: config.yaml-Version hing seit 1.7.13 fest (ab 1.7.14 stumm per sed
  gebumpt, das bei Nichttreffer nichts tut) — HA sah keine neue Version,
  obwohl der Code aktuell war. Version zieht jetzt wieder; dieses Update
  enthaelt alles aus 1.7.14-1.7.18 (Segment-Schiedsrichter als
  Hypothesentest, MAX_KWH_STEP=1, Label-Audit, Kamm-Pose-Refinement)

## 1.7.18

- Extractor: Pose-Refinement per Kamm-Korrelation (Schritt 4). Der
  Template-Anker (Suchradius 25px) verrutschte bei ~19% der Frames oder
  rastete auf Nachbarstrukturen ein — die 6 immer beleuchteten kWh-Ziffern
  bilden dagegen einen periodischen Tinten-Kamm, dessen Phase (dx +-45,
  dy +-12, PITCH mitgefittet) ein robustes Alignment liefert. Gemessen am
  zeitlichen Holdout bei sonst identischem Setup: Zell-Accuracy 0.907 ->
  0.974, kWh-Zeile 78% -> 91%. Kosten ~130ms/Frame (9% des 1,4s-Zyklus).
- Modell auf der neuen Extraktion neu trainiert; die 8 juengsten
  seg-Frames (2 davon las das alte Modell falsch) jetzt 8/8 korrekt

## 1.7.17

- Schiedsrichter entscheidet per Hypothesentest statt per offenem Lesen:
  er kennt die beiden einzig moeglichen Kandidaten (Stand, Stand+1) und
  vergleicht nur noch deren Segmentmuster. Messung an 1294 Frames:
  offenes Lesen ist in der rechten Schattenzone prinzipiell nicht sicher
  (Slot 5 braeuchte conf>=3.9 — schaffen 11% der Frames), der
  Zweiwege-Test irrt in der gefaehrlichen Richtung ("+1" statt "kein
  Zuwachs") bei Marge>=6 nur in 0.4% der Frames; mit den zwei
  geforderten konsistenten Lesungen bleibt ~1:60000
- Offenes Lesen bleibt als VETO: liest der Dekoder selbstbewusst etwas
  ausserhalb des Fensters, ist der gespeicherte Stand vermutlich
  veraltet -> schweigen, Re-Baseline mit Gemini uebernimmt

## 1.7.16

- Der Segment-Schiedsrichter darf jetzt schweigen: seine Zell-Konfidenzen
  wurden bisher gelesen und ignoriert. Auf 403 gelabelten Frames gemessen
  trennt die schwaechste Zell-Konfidenz die Ghost-Fehllesungen sauber
  (0.03-0.09) von korrekten Lesungen (3-20x hoeher) — Schwelle
  SEG_MIN_CONF=0.8 hebt die Treffsicherheit von 76% auf 95% bei 60%
  Abdeckung. Unsichere Frames und erkannte Segmenttests (alles 8er)
  fuehren zum Schweigen statt zu einer geratenen Ziffer; der Regler
  faellt dann auf Re-Baseline/Gemini zurueck wie vorher

## 1.7.15

- KORREKTUR zu 1.7.14 (das die Fehlerrichtung falsch annahm): Der Zaehler
  kann bei ~1,4-s-Zyklus NIE um mehr als 1 kWh steigen. Die alte Toleranz
  +2 in plausible() war das Loch, durch das die Ghost-Fehllesung des
  Segment-Dekoders passte (Phantom-Segmente machen in der rechten
  Schattenzone aus der letzten "1" eine "3"). 24.07. 00:04 wurde so 35873
  akzeptiert, obwohl kNN UND Gemini 35871 lasen -> 2 Stunden lang wurde
  jede korrekte Lesung als "ruecklaeufig" verworfen (das gemeldete
  Springen), bis die Re-Baseline den Stand heilte
- MAX_KWH_STEP=1 in plausible() und im Schiedsrichter-Fenster. Der
  Schiedsrichter bestaetigt "kein Zuwachs" sofort (konservativ, kann
  nichts vergiften), ein +1 erst nach zwei konsistenten Lesungen. Seg-
  Lesungen setzen KEINE Untergrenze mehr (sie koennen ghost-inflatiert
  sein) — Untergrenze ist allein der akzeptierte Stand
- Label-Korrektur: die drei 35871-Labels waren richtig (in 1.7.14
  faelschlich quarantaeniert, jetzt zurueck), die drei 35873-Labels sind
  die Fehllesungen und liegen in quarantine/

## 1.7.13

- Disk-Diaet: events/ (Diagnose-Frames, 93% des 1,2-GB-Korpus, 71k
  Dateien) unterliegt jetzt Retention — max 10 Tage und 300 Dateien/Tag
  (Failsafe-Stuerme schrieben tausende identische Frames); Roh-Evidence
  45 Tage, control-Logs 14 Tage; auto/ (kuratierte Labels) unbegrenzt.
  Laeuft in make retrain (scripts/compact_corpus.py)
- NUC begrenzt sich selbst: .git > 1 GB -> automatischer Re-Clone
  (shallow+blobless, --depth 50). Einmaliges Update raeumt die
  aktuellen 3,7 GB auf ~0,4 GB ab

## 1.7.12

- KRITISCH: W-Sprungfilter hatte keinen Heilpfad (seit v1) — ein einmal
  akzeptierter Extremwert (Geister-8443 beim Erststart, 23.07. 07:30)
  liess JEDE echte Lesung als "Sprung >5000W" abprallen: Dauer-Failsafe
  bis zum Neustart. Jetzt: 4 konsistente Lesungen auf neuem Niveau
  re-baselinen den W-Stand (wie beim Vorzeichen-Flip-Guard)
- Retrain-Alarm zaehlt Failsafe-EINTRITTE statt Zyklen im Failsafe
  (Grund-Sensor zeigte 3000+ statt 1)

## 1.7.11

- Erststart-Loch geschlossen: die allererste W-Lesung nach einem Neustart
  hatte keinen Sprungfilter-Vergleichswert und wurde bedingungslos
  akzeptiert (23.07. 07:30: Geister-8 machte aus 443 W einen 8443-W-Spike
  in HA). |W| > 1000 braucht jetzt direkt nach dem Start eine zweite
  konsistente Lesung (+-20%)

## 1.7.10

- HA-Sensor "OCR Retrain faellig" (+ Grund): rollierende 6h-Zaehler auf
  dem NUC — Seg-Schiedsrichter-Einsaetze (>=3), Failsafes (>=2),
  Disagreements (>=20). Meldet, WANN sich ein Retrain lohnt; trainiert
  wird weiterhin bewusst auf der Trainings-Maschine
- make retrain: Pull -> Konsens-Labels -> Geometrie-Audit -> Training ->
  Holdout-Gate (>=0.90, sonst kein Push) -> Push mit Rebase-Retry

## 1.7.9

- Rollover-Schiedsrichter: verwirft die Plausibilitaet eine kWh-Lesung
  (ruecklaeufig/Sprung), prueft ein deterministischer 7-Segment-Dekoder
  denselben Frame — ganz ohne Trainingsdaten, dadurch immun gegen das
  "neue Ziffer an neuer Position"-Problem (gemessen: 96-97% an den
  kritischen Slots, wo das kNN auf 5-66% faellt). Bestaetigt er den
  erwarteten Zaehlerstand, wird die Lesung akzeptiert statt Failsafe,
  und der Frame landet als Trainingslabel in samples/seg/ (max. 1/min),
  das der Sync vollstaendig committet — Retraining fuettert sich beim
  Rollover kuenftig selbst

## 1.7.8

- Label-Hygiene (Befund des Auto-Train-Reviews): Sync promotet KEINE
  rohen Gemini-Labels mehr nach training-data/auto/ — Labels entstehen
  nur noch per Konsens-Labeler auf der Trainings-Maschine
- Konsens-Labeler: kWh-Aera-Fenster dynamisch aus den juengsten Labels
  (hartkodiertes Fenster braeche beim Zaehler-Rollover); widersprochene
  Labels wandern nach training-data/quarantine/ statt geloescht zu
  werden; Training schliesst quarantine/ aus

## 1.7.7

- kWh-Poison-Schutz: Zaehlerstand-Erhoehungen werden erst nach 2
  uebereinstimmenden Lesungen uebernommen — am 21.07. vergiftete EINE
  Fehl-Lesung (35853 statt 35851, exakt an der +2-Grenze) den Stand
  und blockierte 50 min lang alles als "rueckläufig" (Failsafe)
- Re-Baseline zaehlt Konsens JE KANDIDAT: eingestreute Dunkel-Fehl-
  Lesungen resetteten den Zaehler und verzoegerten die Heilung um Stunden
- Event-Speicherung gedrosselt: 5 Frames je Fehlergrund/Tag, dann jeder
  50. (vorher 2300+ Frames/Tag Segmenttest/Rueckläufig-Sturm ins Repo)

## 1.7.6

- Gemini-Modellnamen werden normalisiert ("flash-latest" ->
  "gemini-flash-latest") — die 1.7.5-Changelog-Kurzformen waren als
  Optionswert ungueltig (404-Sturm); jetzt funktionieren beide Formen
- Lastreduktion (Netz/DTU): OpenDTU-Livedata 2,5s gecacht statt jeden
  Regelzyklus gepollt; Limit-Sends min. 2s Abstand (RF-Queue!); MQTT
  drosselt w/limit auf alle 5s, kwh/status weiter sofort bei Aenderung

## 1.7.5

- Gemini: 404-Modelle fliegen fuer den Rest des Tages aus der Rotation
  (die 2.5er sind aus dem Free-Tier verschwunden und verbrannten bei
  jedem Fallback 4 sinnlose Requests) — heilt auch alte Modell-Listen
  in gespeicherten Optionen ohne Konfig-Aenderung
- Default-Modellkette aktualisiert: flash-lite-latest, flash-latest,
  3.1-flash-lite, 3.5-flash, 2.0-flash-lite (gegen die Live-API geprueft)

## 1.7.4

- Stuck-Detection nur noch mit Kick-Spielraum: stand das Limit schon am
  Anschlag (max_limit/Akku-Cap), war "pv unter Limit" Quellenbegrenzung,
  kein Klemmen — 3 der ersten 5 kick_results waren solche Fehlalarme

## 1.7.3

- Startzeile las die in 1.7.0 entfernte Option reader_mode ("Modus null")
  — nutzt jetzt den fest verdrahteten Wert

## 1.7.2

- MPPT-Kick als Eskalationstreppe: +100/+200/+400/+800 W ueber dem
  Klemm-Limit, je 10 s gehalten, statt sofortiger Verdopplung. Der
  loesende Schritt wird als kick_result-Event in die Telemetrie
  geschrieben — damit vermessen wir die Loese-Schwelle des HMS und
  koennen den Kick spaeter datenbasiert auf einen Schritt verkuerzen
- BUGFIX (seit 1.6.4): Send-Logzeile crashte, sobald die Pending-
  Kompensation aktiv war (float im :+d-Format) — das Limit ging zwar an
  die DTU, aber der Regler-State behielt den alten Wert (State-Drift,
  moeglicher Zappel-Verstaerker). Log gefixt, Fehler wieder ganzzahlig

## 1.7.1

- MPPT-Stuck-Kick: klemmt der HMS an der Batterie weit unter dem Limit
  (taeglich beobachtet: 178 W bei Limit 420, kleine Schritte wirkungslos,
  grosser Sprung loest), erkennt der Regler das (Bezug + Limit >150 W
  ueber Ist + 25 s keine Bewegung) und ueberzieht das Limit einmal
  kraeftig (2x Soll, gedeckelt) — der normale Runter-Pfad holt es danach
  zurueck. Cooldown 180 s, damit ein quellenbegrenzter Inverter (Wolke,
  Akku leer) keinen Kick-Loop erzeugt

## 1.7.0

- Options-Grossputz: 18 tote/nie angefasste Optionen entfernt
  (reader_mode, ocr_min_conf, cross_check_every, gemini_cooldown_s,
  cam_mode, led_brightness, cam_frames, interval_s, control_every,
  min_limit_w, failsafe_after, max_jump_w, auto_train_hour,
  pending_theta_s, pending_tau_s, min_step_w, batt_max_drain_w,
  batt_release_s) — Werte sind jetzt fest verdrahtet bzw. Code-Defaults
- Neue Defaults: latency_s 0 (Smith-Predictor bremst), target_grid_w -20,
  batt_low_v/high_v 51.2/54.4 (16S LiFePO4), failsafe_limit_w 51
- HINWEIS: Meckert HA nach dem Update ueber unbekannte Optionen, einmal
  die Add-on-Konfiguration oeffnen und speichern — das raeumt alte
  Schluessel weg

## 1.6.5

- Pending-Kompensation v2: Schritte klingen mit der gemessenen
  Sprungantwort ab (voll bis theta=4s, dann exp(-t/tau), tau=2.5s) statt
  hart nach 5s zu verfallen — 1.6.4 liess die Kompensation genau dann
  fallen, wenn die Wirkung erst halb angekommen war (Telemetrie 20.07.:
  Umkehrungen 6x seltener, aber Restschwinger 123W statt 105W)
- WICHTIG fuer bestehende Installationen: Add-on-Option latency_s
  pruefen — Telemetrie zeigt runter-Sends im 1,2s-Abstand, die Option
  steht dort offenbar auf ~1 statt 8 (Default). Auf 8 stellen!

## 1.6.4

- Anti-Pendel v2 (Pending-Kompensation): Limit-Schritte der letzten
  pending_s (~Totzeit) werden vom Regelfehler abgezogen — das Stale-Echo
  des eigenen Schritts kann kein Nachpumpen mehr ausloesen, echte
  Lastspruenge reagieren unveraendert sofort. Ersetzt min_send_gap_s/
  urgent_error_w aus 1.6.3: deren Notbremse (error>100) war abends
  praktisch immer aktiv (Fehler-Median 180 W) und hebelte die Sperre aus
- min_step_w (15 W): Mikro-Limit-Aenderungen werden nicht mehr gefunkt —
  heute waren 940 von 1908 Sends Schritte unter 20 W

## 1.6.3

- Anti-Pendel: Sende-Sperrzeit min_send_gap_s (Default 5 s ~ gemessene
  HMS-Totzeit) gilt jetzt auch fuer hoch — Telemetrie zeigte 783 Sends/Tag
  mit Median-Abstand 3,9 s, davon 205x hoch-auf-hoch bevor der erste
  Schritt messbar war, plus 164 Richtungswechsel (Schwingweite median
  58 W). Notbremse: ab urgent_error_w (100 W) Netzbezug feuert der Regler
  sofort, Sperrzeit egal

## 1.6.2

- Regler-Telemetrie: jeder Limit-Send + Leistungsverlauf (Inverter-AC)
  ±45 s drumherum als JSONL unter samples/control/, wird per Git-Sync
  nach training-data/control/ committet. Grundlage fuer die FOPDT-
  Analyse der HMS-Totzeit (scripts/analyze_latency.py) und das Tuning
  von LATENCY_S — Ziel: das +/-Pendeln bei traegem HMS beenden

## 1.6.1

- Akku-Waechter: Freigabe erst, wenn die Bus-Spannung batt_release_s
  (Default 300 s) durchgehend ueber batt_high_v lag — die Victron-
  Ladespannung liegt beim Laden sofort ueber der Schwelle, obwohl der
  Akku noch leer ist (verhindert Hold/Frei-Pendeln)

## 1.6.0

- Akku-Waechter: batt_strings (z.B. "1,4") schuetzt Akku-Strings vor
  Tiefentladung — unter batt_low_v wird das Gesamtlimit adaptiv gesenkt,
  bis die gemessene Entnahme ~0 W ist; ab batt_high_v wieder frei
  (Hysterese). Neue HA-Sensoren: Akku-Spannung, Akku-Schutz aktiv.
  OpenDTU-on-Battery: Dynamic Power Limiter deaktivieren!
- Gemini-Prompt mit Kontext und bekannten Edge-Cases (6-stelliger
  Zaehlerstand — nie trunkieren, Minuszeichen, Segmenttest, Dunkel-Frame)

## 1.5.1

- Gemini-Label-Bug behoben: Gemini trunkiert kWh gelegentlich auf 4 Stellen
  ("3574" statt 35741) — 123 vergiftete Auto-Labels repariert (98 per
  Modell-Konsens) bzw. geloescht; valid_label() verwirft kWh < 10000
- Segmenttest wird lokal auch bei 8er-dominierten Fehl-Lesungen erkannt
  (halbiert die Gemini-Fallback-Calls auf Segmenttest-Frames)
- Modell neu trainiert (829 Samples, inkl. Abend-Evidence bis 16.07.)

## 1.5.0

- NUC trainiert nicht mehr: der Feedback-Sync sammelt und committet nur
  noch Evidence. Gemini-Labels sind fehlerbehaftet — trainiert wird erst
  nach Label-Audit (scripts/ocr/relabel.py: Vorzeichen-Korrektur per
  Geometrie, strittige W-Labels -> kWh-only)
- Option umbenannt: retrain_hour -> auto_train_hour (Default -1 = aus;
  alte Env-Variable RETRAIN_HOUR wird als Fallback noch gelesen)
- Modell neu trainiert auf auditiertem Datensatz (8 Vorzeichen korrigiert,
  26 strittige W-Labels neutralisiert)

## 1.4.15

- state_write_s-Option entfernt: das kWh-Feld wird immer bei Aenderung
  geschrieben (wenige Bytes, wenige Male am Tag) — ein Aus-Schalter
  schuf nur stale-State-Risiko

## 1.4.14

- state.json persistiert nur noch das kWh-Feld und nur bei Aenderung
  (wenige Winz-Writes/Tag, nie wieder stale Zusatz-Felder)
- Re-Baseline: Gemini-Cooldown resettet den Bestaetigungs-Zaehler nicht
  mehr — Heilung eines veralteten kWh-Stands greift im ersten freien Slot

## 1.4.13

- Retraining-Schwelle zaehlt jetzt den RUECKSTAND seit dem letzten
  Training (committeter Marker training-data/.trained-at) statt nur die
  Labels eines Sync-Laufs — vorher konnte sich der Rueckstand unsichtbar
  stapeln und das Modell wurde nie trainiert/gepusht. Erster Sync nach
  diesem Update trainiert sofort (Marker fehlt -> voller Rueckstand).

## 1.4.12

- Minus-Erkennung: Geometrie-Veto in eindeutigen Zonen (Masse nur im
  Mittelband = Minus, ratio>0.75 / <0.3), dazwischen kNN
- Label-Audit beim Training: W-Zeilen, deren Gemini-Label der Minus-
  Geometrie widerspricht (verschlucktes Vorzeichen!), fliegen aus dem
  Training — die Flip-Fehler waren zum Teil antrainierte Label-Fehler
- Modell auf auditiertem Datensatz neu trainiert

## 1.4.11

- Vorzeichen-Flip-Guard: Toleranz auf +-20% (min. 40 W) — faengt auch
  +350/-360-Flips mit Messrauschen dazwischen

## 1.4.10

- Vorzeichen-Flip-Guard: w-Lesungen mit gleichem Betrag und umgekehrtem
  Vorzeichen (+360/-360-Gezappel) werden verworfen; erst 4 konsistente
  Lesungen akzeptieren einen echten Nulldurchgang
- Feedback-Repo migriert sich selbst auf Blobless-Clone (kein manuelles
  Loeschen von /data/feedback-repo noetig)

## 1.4.9

- NUC-Runtime nutzt das git-gesyncte Modell aus dem Feedback-Checkout
  (`MODEL_FILE`) und laedt es bei Aenderung im laufenden Betrieb neu —
  Retraining wirkt sofort, nicht erst beim naechsten Release

## 1.4.8

- NUC-Clone als Blobless-Clone (`--filter=blob:none`): lokale Groesse bleibt
  ~konstant, History-Blobs liegen nur auf GitHub (bestehenden Clone einmal
  loeschen: Add-on stoppen, `/data/feedback-repo` entfernen, starten)
- Retrain-Commits enthalten nur noch EIN Modell (halbierte History-Rate);
  die Add-on-Kopie wird beim Release gebaut

## 1.4.7

- KRITISCH: training-data/ stand in .gitignore — Evidence wurde nie
  committet ("Push ok" ohne Commit), aber lokal geprunt. Gitignore
  bereinigt; Prune läuft jetzt nur noch, wenn training-data nachweislich
  vollständig committet ist. Unkommittete Evidence im /data-Checkout wird
  vom nächsten Sync-Lauf automatisch nachcommittet.

## 1.4.6

- Sync-Intervall default 300s (Commit-Hygiene: keine Mini-Commits alle 30s)

## 1.4.5

- OCR: Shift-Augmentierung — Ziffern generalisieren über alle LCD-Positionen
  (behebt 1→7-Fehllesungen nach Zähler-Rollover, z.B. 35710→35770)
- Feedback-Sync: nur Disagreements/Events + jedes 20. Routine-Sample werden
  committet; lokale Dateien werden erst nach erfolgreichem Push gelöscht
- Deploy-Key: nur noch `git_deploy_key_base64` (Mehrzeilen-Keys brechen im
  HA-Options-UI)
- Sync-Logs mit Zeitstempeln
- Modell als float16 (8,7 MB statt 15,7 MB)

## 1.4.4

- Deploy-Key als Base64-Feld für HAOS

## 1.4.2

- HAOS-nativer Feedback-Worker: Evidence → Git, Retraining, Modell-Push

## 1.4.1

- Positions-bewusstes OCR (Slot-Präferenz mit Fallback), Event-Outbox

## 1.4.0

- `interval_s` als Kommazahl (0,5-s-Takt), `state_write_s`-Schreibdrossel

## 1.3.0

- `log_level` (all/error/none), Samples & Retraining im Add-on default aus

## 1.2.1

- Add-on im Store unsichtbar: ungültiges watchdog-Feld entfernt, build.yaml

## 1.2.0

- Nächtliches Auto-Retraining mit Hot-Reload des Modells

## 1.1.0

- Erstes Add-on-Release: lokales OCR, Hybrid-Modus, Regler v3, MQTT-Discovery
