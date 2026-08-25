# Eigenverbrauch: wo er verloren geht und was dagegen hilft

Analyse der eigenen Regler-Telemetrie (`training-data/control/`,
19.07.–24.08.2026: 96 412 Limit-Befehle, 576 088 Ticks). Alle Zahlen sind
mit `scripts/analyze_selfuse.py` reproduzierbar:

```bash
python3 scripts/analyze_selfuse.py --days 7
python3 scripts/analyze_selfuse.py --days 40 --p-hit 0.67
```

## Kurzfassung

Der Regler hatte unterhalb des Sustain-Floors nur zwei Antworten auf eine
kleine Hauslast: **Limit halten** (430 W, Ueberschuss geht ins Netz) oder
**Inverter schlafen legen** (37 W, das Netz zahlt den Rest). Bei 150 W
Hauslast ist beides falsch — und beides zusammen ist genau das beobachtete
Bild: „zu viel ins Netz" und „Akku bei 50 % steht daneben".

Die Daten zeigen aber, dass es eine dritte Antwort gibt: der HMS hat ein
**stabiles Plateau bei ~160 W**, das er von selbst ansteuert. Es deckt die
Grundlast fast exakt. Der Regler kannte es nur nicht.

| Ursache | Anteil | Gegenmittel | Status |
|---|---|---|---|
| Zwei-Wege-Entscheidung am Floor | 0,5–0,8 kWh/Tag | Arbeitspunkt-Leiter | **im Code (1.8.0)** |
| Sollwert linear ueber die Spannung statt ueber den Ladestand | ~0,2–0,5 kWh/Tag | SoC-Kennlinie | **im Code (1.8.0)** |
| 886 Limit-Befehle/Tag, 43 % davon binnen 30 s zurueckgenommen | indirekt (MPPT) | Bestaetigung fuer kleine Hoch-Schritte | **im Code (1.8.0)** |
| Haus will mehr als der HMS liefern kann (~1400 W Decke) | **58 % des Netzbezugs** | Hardware: mehr Eingaenge/Leistung | offen |

## 1. Was die Telemetrie sagt

### 1.1 Der Regler steht die meiste Zeit an einem der beiden Extreme

Letzte 7 Tage, Zeitanteile aus der lueckenlosen Kette der Limit-Ereignisse
rekonstruiert:

| Zustand | Zeit | Netzleistung im Mittel | HMS liefert |
|---|---|---|---|
| schlafend (Limit 50 W) | 51,7 h kurze + **61,9 h lange Abschnitte** | +56 W | 37 W |
| am Floor (430 W) | 30,6 h | −90 W | 425 W |
| regelnd (> 460 W) | 14,5 h | +687 W | — |

Die 61,9 h in „langen Abschnitten" sind der Kern des Problems: der Inverter
lag **stundenlang am Stueck** auf 50 W, waehrend das Haus einkaufte. Zwei
Beispiele aus dem Log: 20.08. 16:43 fuer 1,9 h bei +206 W am Zaehler,
19.08. 23:06 fuer 2,1 h bei +201 W.

### 1.2 Der Bedarf lag meistens genau zwischen den beiden Antworten

Bedarf = was das Haus vom HMS wollte (Netzleistung + HMS-Leistung),
gemessen in den belegten Fenstern der Schlaf-/Floor-Abschnitte:

| Bedarf | letzte 7 Tage | Bewertung |
|---|---|---|
| < 80 W | 15,1 h (18 %) | Schlafen ist richtig |
| **80–292 W** | **55,0 h (67 %)** | **beide Antworten falsch** |
| > 292 W | 12,2 h (15 %) | Floor halten ist richtig |

Die Schwellen sind keine Setzung, sondern folgen aus den Kosten (s. 2.1).

### 1.3 Der Inverter kann mehr als „430 W oder aus"

Landeplatz-Tabelle ueber alle 36 Tage — Befehl gegen die AC-Leistung
25–60 s spaeter, **nur fuer Befehle, die mindestens 60 s stehen blieben**:

| Befehl | n | landet aus (<60 W) | landet 120–210 W | landet 350–460 W | folgt sauber | Median |
|---|---|---|---|---|---|---|
| 50–99 W | 583 | **98 %** | 1 % | 0 % | — | 37 W |
| 100–249 W | 70 | 72–83 % | 0–24 % | 0–4 % | — | 0 W |
| **250–299 W** | 50 | 30 % | **66 %** | 2 % | — | **163 W** |
| **300–349 W** | 42 | 26 % | **64 %** | 0 % | — | **151 W** |
| **350–399 W** | 32 | 16 % | **62 %** | 9 % | — | **155 W** |
| 400–449 W | 959 | 1 % | 16 % | **81 %** | — | 422 W |
| ab 500 W | — | 0–2 % | 3–13 % | — | **85–97 %** | folgt |

Drei Befunde:

1. Ein Befehl um **300 W** landet in zwei von drei Faellen auf dem
   ~160-W-Plateau. Der Rest faellt aus — also auf das, was heute ohnehin
   passiert. Ein Versuch kann damit nichts verschlechtern.
2. Das Plateau ist **stabil**: Streuung innerhalb eines Plateaus 5,6 W
   (Median ueber 805 Plateaus), und es haengt **nicht an der Busspannung**
   (48 V → 155 W, 51 V → 162 W, 55 V → 152 W).
3. Ein Plateau zwischen 210 und 350 W gibt es **nicht**. Fuer einen Bedarf
   ueber ~292 W bleibt der Floor die richtige Antwort — die frueher
   vermuteten „Plateaus bei 320 W" waren Durchgangswerte auf dem Weg nach
   unten, keine Ruhelagen.

### 1.4 Der Regler funkt sich selbst ins Knie

886 Limit-Befehle/Tag. Davon:

- **43 % aller „hoch"-Befehle** werden binnen 30 s wieder auf den
  Ausgangswert zurueckgenommen (Median-Schrittweite 68 W).
- Die Ausfluege ueber den Floor dauern im **Median 4 Sekunden** — kuerzer
  als die gemessene Totzeit des HMS (6–8 s). Diese Befehle koennen also
  gar nichts bewirkt haben.
- 71 % aller Befehle mit Ziel unter 460 W werden binnen 15 s vom naechsten
  ueberholt.

Jeder Befehl ist eine Stoerung des MPPT. Genau das ist der Mechanismus,
der niedrige Arbeitspunkte instabil macht — der Regler zerstoert also die
Ruhe, die er fuer die 160-W-Stufe braucht.

### 1.5 Der groesste Einzelposten ist gar kein Regelproblem

In den belegten Fenstern der letzten 7 Tage wurden 5,69 kWh gekauft.
**3,30 kWh davon (58 %) flossen, waehrend der HMS bereits an seiner Decke
lief** (~1400 W bei Limit 1600 W, Median-Bezug in diesen Momenten 763 W).
Kein Regleralgorithmus holt das zurueck — dafuer braucht es mehr
Wechselrichterleistung (s. 4.1).

## 2. Was jetzt im Code steht (1.8.0)

### 2.1 Arbeitspunkt-Leiter statt Zwei-Wege-Entscheidung

Unterhalb des Floors waehlt der Regler den **guenstigsten erreichbaren
Arbeitspunkt**. Die Kostenfunktion ist der ganze Trick (`lp_cost`):

```
Kosten(Punkt) = fehlende Watt (kauft das Netz)
              + ueberschuessige Watt (verschenkte Akku-Energie)
```

Beide Terme zaehlen gleich, weil verschenkte Akku-Energie spaeter genau
den Netzbezug ersetzt haette, den sie jetzt nicht ersetzt. **Ausnahme:
voller Akku** — dann waere der Ueberschuss ohnehin abgeregelt, der zweite
Term entfaellt, und es wird wie bisher immer gehalten (Verhalten aus
1.7.23 bleibt).

Daraus folgen die Schwellen von selbst: der 160-W-Punkt schlaegt den
Schlaf ab 80 W Bedarf und den Floor bis 292 W Bedarf.

Bei 150 W Hauslast:

| Antwort | Kosten |
|---|---|
| schlafen (37 W) | 113 W Netzbezug |
| Floor halten (425 W) | 275 W verschenkt |
| **Arbeitspunkt (160 W)** | **10 W** |

**Jeder Punkt wird verifiziert.** 25 s nach dem Befehl wird die
AC-Leistung geprueft: Treffer → die Erwartung zieht per EMA nach (der
Punkt kalibriert sich selbst). Fehlschlag → ein zweiter Anlauf, danach
wird der Punkt 15 min gesperrt und der Regler faellt auf Floor oder Schlaf
zurueck. **Der schlechteste Fall ist damit exakt das alte Verhalten**,
plus ~25 s Anlauf.

Dazu Ruhe: Mindest-Standzeit 120 s pro Punkt, Hysterese von 20 W auf der
Kostendifferenz, Entscheidung auf dem 12-s-Median des Bedarfs. Im Test mit
der echten Zappellast (180 ↔ 266 W im Sekundentakt) bleibt es bei
**einem** Befehl in 320 s.

**Der MPPT-Kick behaelt Vorrang.** Liefert der Inverter weit weniger als
sein Limit, waehrend das Haus kauft (der Fall, fuer den der Kick
geschrieben wurde: „178 W bei Limit 420"), uebernimmt die Leiter gar nicht
erst, und ein angefangener Kick laeuft immer zu Ende. Sonst haette die
Leiter den Klemmwert einfach als Arbeitspunkt dazugelernt und der Inverter
waere unten geblieben.

Abschalten: Add-on-Option `low_points` leeren → exakt die alte Logik.

### 2.2 Netz-Sollwert folgt dem Ladestand, nicht der Spannung

Die Interpolation zwischen `target_grid_w` (leer) und
`target_grid_full_w` (voll) lief linear ueber die Packspannung. Bei
LiFePO4 ist die Kennlinie zwischen 20 und 90 % nahezu flach — mit den
Stuetzstellen 47,0/54,4 V las der Regler bei **52,7 V Busspannung „77 %
voll"** und stellte das Ziel auf **−34 W**, also Dauer-Einspeisung aus
einem halb leeren Speicher. Dieselbe Spannung ergibt ueber die
Ruhespannungskennlinie (`soc_estimate`, lastkorrigiert) **50 %** und damit
**−14 W**. 20 W ueber 24 h sind ~0,5 kWh.

Die Schaetzung geht **geglaettet** (5 min) ins Ziel: ueber die
Lastkorrektur haengt sie an der Ausgangsleistung, und ohne Glaettung
wanderte das Ziel im Sekundentakt mit der Last — eine schwache, aber
unnoetige Mitkopplung. Ein Ladestand aendert sich in Minuten, nicht in
Sekunden. Faellt die Schaetzung ganz aus, gilt weiter die alte lineare
Rechnung.

### 2.3 Kleine Hoch-Schritte brauchen eine Bestaetigung

Ein Fehler bis `UP_FAST_W` (150 W) muss `UP_CONFIRM_S` (3 s) anhalten,
bevor der Befehl rausgeht. Grosse Lastspruenge bleiben **sofort und
ungebremst** — die Politik „kein Cent Netzbezug, wenn die Sonne liefern
kann" gilt unveraendert. Kosten pro unterdruecktem Zappler: hoechstens
0,13 Wh.

Massstab ist bewusst der Fehler, nicht die Schrittweite: wegen
`wanted = PV + Fehler` ist `wanted − Limit` immer kleiner oder gleich dem
Fehler, eine zusaetzliche Schrittbedingung waere also nie bindend — und
liesse genau die 68-W-Zappler durch, um die es geht.

### 2.4 Telemetrie-Herzschlag

`ctl_tick` schrieb bisher nur rund um Limit-Befehle. Ausgerechnet die
langen ruhigen Abschnitte (61,9 h in 7 Tagen) waren dadurch unbelegt — die
teuerste Zeit war die unsichtbare. Jetzt geht alle 30 s ein Tick raus
(`hb: 1`), ~250 kB/Tag zusaetzlich. Damit ist die naechste Auswertung
exakt statt korridorbreit.

### Erwarteter Effekt

Monte-Carlo ueber die echten Abschnitte, **mit** der gemessenen
Fehlschlagquote (33 %), zwei Anlaeufen und 15 min Sperre:

| Zeitraum | Netzbezug | verschenkte Akku-Energie | Summe |
|---|---|---|---|
| letzte 7 Tage | −1,4 bis −2,9 kWh | −2,2 bis −2,6 kWh | **0,51–0,78 kWh/Tag** |
| gesamte 36 Tage | −1,5 bis −4,7 kWh | −3,8 bis −4,2 kWh | 0,15–0,24 kWh/Tag |

Der Korridor kommt daher, dass die Netzleistung waehrend der langen
Schlafphasen nur an ihrem Ende belegt ist (2.4 behebt das). Die letzten
7 Tage sind der relevante Zeitraum — dort liegt der Anteil „Bedarf im
Fenster" bei 67 %.

## 3. Naechster Schritt: die Leiter auf der eigenen Hardware ausmessen

Der Wert `300:160` stammt aus Kommandos, die der Regler **nebenbei**
erzeugt hat, nie aus einem geplanten Versuch. `scripts/probe_operating_points.py`
holt das nach — Treppe abfahren, jede Stufe stehen lassen, messen:

```bash
# Add-on in HA stoppen, dann auf einer Maschine mit .env:
set -a; . .env; set +a
python3 scripts/probe_operating_points.py --yes --hold 300 --repeat 3 \
    --out /tmp/probe.jsonl
# Zusatzfrage: haengt der Landeplatz am Anfahrweg?
python3 scripts/probe_operating_points.py --yes --from-below
```

Das Skript bricht ab, wenn ihm jemand ins Limit funkt (Add-on laeuft noch,
OpenDTU-DPL aktiv), stellt am Ende immer das alte Limit wieder her und
druckt die fertige `low_points`-Zeile. Am besten nachts: konstante Last,
keine Wolken.

Zwei Fragen, die nur der Versuch beantworten kann:

- **Haelt das 160-W-Plateau ueber Stunden?** Im Log gibt es kein einziges
  Beispiel, weil der Regler nie so lange die Finger stillgehalten hat
  (laengste beobachtete Ruhe: 275 s).
- **Bestimmt der Anfahrweg den Landeplatz?** Die 20 auswertbaren Faelle
  „von unten angefahren" landeten alle bei ~156 W. Falls sich das
  bestaetigt, ist der Weg Schlaf → 300 W verlaesslicher als Floor → 300 W,
  und der Regler kann das gezielt so fahren.

## 4. Was der Code nicht loesen kann

### 4.1 Die 1400-W-Decke (der groesste Posten)

58 % des Netzbezugs entstehen, waehrend der HMS schon am Anschlag laeuft.
Gemessen liefert er maximal ~1400 W (Befehl 1600 W → 1382–1420 W), obwohl
auf dem Typenschild 2000 W stehen. Das ist die Signatur **nicht belegter
Eingaenge**: der HMS-2000-4T teilt seine Leistung auf vier MPPT-Eingaenge
auf, und was an einem fehlenden Eingang haengt, kann er nicht liefern.

Zu pruefen (OpenDTU-Live-Ansicht, DC-Kacheln): wie viele Eingaenge tragen
den Bus wirklich? Jeder zusaetzlich angeschlossene Eingang hebt die Decke
— mit eigener DC-Sicherung und Aderquerschnitt nach `akku-bms-anschluss.md`
Schritt 6. Das ist die mit Abstand wirksamste Einzelmassnahme, wenn die
Zahl unter vier liegt.

Danach: `max_limit_w` von 1600 hochsetzen und den Akku-Entladestrom im
BMS gegenpruefen (2000 W bei 51 V sind ~40 A).

### 4.2 Der Deye koennte die Grundlast uebernehmen

Seit 1.7.43 ist der Deye ueber Register 0x0028 in **1-%-Schritten**
regelbar (600 W Nennleistung → 6 W Aufloesung), die Leistung folgt in
2–3 Minuten. Genau das Profil, das eine Grundlast braucht — und genau das,
was der HMS nicht kann.

Haengt der Deye am Akku-Bus statt an eigenen Modulen, deckt er 30–600 W
fein dosiert, waehrend der HMS die schnelle, grobe Regelung ab 430 W
uebernimmt. Damit verschwindet das Problem aus 1.1/1.2 vollstaendig statt
naeherungsweise.

**Vorher pruefen — das ist kein Detail:** die MPPT-Eingangsspannung des
SUN600G3 im Datenblatt gegen den Bus halten. 16S LiFePO4 laeuft bis
57–58 V in der Absorption; liegt das obere MPPT-Ende bei 54 V, ist der
Akku-Bus **nicht** ohne Weiteres zulaessig, auch wenn die absolute
Maximalspannung hoeher liegt. Ohne diese Pruefung nicht anschliessen.

Solange der Deye an eigenen Modulen haengt, gilt: **ihn zu drosseln bringt
nichts.** Seine Energie ist nicht speicherbar; abgeregelt oder verschenkt
ist wirtschaftlich dasselbe (Einspeisung bringt 0 ct). Der Schieberegler
in HA bleibt ein Handgriff fuer Sonderfaelle, kein Regelziel.

### 4.3 Ueberschuss bei vollem Akku verbrauchen

4,2 % der Zeit steht der Bus ueber 54,4 V, also faktisch voll — dann
regelt der Victron ab und die Sonne wird gar nicht erst geerntet. Das ist
der einzige Zustand, in dem Energie wirklich gratis ist. Eine schaltbare
Last (Warmwasser, Waschmaschine, Auto) genau dann zu starten, ist die
sauberste Form von „mehr selbst verbrauchen" — und braucht nur eine
HA-Automation auf `sensor.smartmeter_akku_ladestand` + `Netzleistung`,
keinen Eingriff in den Regler.

Wichtiger noch: 21,8 % der Zeit liegt der Bus **unter 51,2 V**. Der
Speicher ist eher zu klein als zu voll — jede Wattstunde, die er ans Netz
abgibt, fehlt nachts wirklich. Das ist die Rechtfertigung dafuer, den
Ueberschuss-Term in `lp_cost` voll zu gewichten.

## 5. Was ausdruecklich NICHT hilft

**Takten (PWM) zwischen Floor und Schlaf.** Naheliegend, aber die
Kostenfunktion ist in der Einschaltdauer linear:
`d · (Floor − Bedarf) + (1 − d) · Bedarf`. Ein Minimum liegt deshalb immer
an einem der Raender — Takten ist nie besser als die bessere der beiden
Antworten, kostet aber Schaltspiele und MPPT-Ruhe. Nur ein echter dritter
Arbeitspunkt hilft, und genau den gibt es (1.3).

**Den Floor einfach senken.** Unter 250 W faellt der Inverter in 72–83 %
der Faelle ganz aus (1.3). Ohne Verifikation waere ein niedrigerer Floor
schlicht eine unzuverlaessige Abschaltung.

**Wolken- oder PV-Prognose.** Am HMS haengt ausschliesslich der Akku-Bus;
die Prognose gehoert an den Victron, nicht an den Regler (siehe auch
`regler-v2-plan.md`, wo dasselbe schon fuer die Sekundenregelung
ausgeschlossen wurde).
