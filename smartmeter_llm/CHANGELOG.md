# Changelog

## 1.7.27

- Klemm-Erkennung war praktisch blind: sie verlangte, dass sich die
  Leistung um weniger als 25 W bewegt. Der Attraktor wackelt aber um
  +-50 W, also galt der Inverter als "in Bewegung" und der Kick feuerte
  nie — in der Nacht 25./26.07. hing er 511 s am Stueck bei 109-174 W
  unter einem Limit von 430 W, ohne einen einzigen Kick. Fortschritt wird
  jetzt relativ zur Luecke gemessen (weniger als ein Viertel geschlossen =
  geklemmt). Diese Klemmer waren 30% des naechtlichen Netzbezugs

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
