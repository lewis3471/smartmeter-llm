# Einstellungen — was bedeutet was

Kurzreferenz für die Add-on-Konfiguration in Home Assistant.
**Neue Optionen werden von HA automatisch mit ihrem Default ergänzt —
bestehende Einstellungen bleiben erhalten, es geht nie etwas verloren.**

## Die drei Zahlen, die oft verwechselt werden

Sie sitzen an verschiedenen Stellen der Kette:

```
   Stromzähler                        Wechselrichter
   (was das Netz sieht)               (was wir befehlen)
        │                                    │
   target_grid_w  ────► Regler ────►  Limit in Watt
   target_grid_full_w                  ├── sustain_floor_w  (Untergrenze im Normalbetrieb)
                                       └── min_limit_w      (absolute Notbremse)
```

### `target_grid_w` / `target_grid_full_w` — das ZIEL am Zähler

Was am Stromzähler stehen soll. **Negativ = einspeisen, positiv = beziehen.**

Mit Akku wandert das Ziel automatisch mit dem Ladezustand:

| Ladestand | Ziel | warum |
|---|---|---|
| leer (`batt_low_v`) | `target_grid_w` = **+20 W** | Speicher schonen, lieber ein paar Watt kaufen |
| ~50 % | −15 W | dazwischen interpoliert |
| voll (`batt_high_v`) | `target_grid_full_w` = **−50 W** | Akku kann nichts mehr aufnehmen, Überschuss darf raus |

Interpoliert wird seit 1.8.0 über den **Ladestand**, nicht mehr linear über
die Spannung: bei LiFePO4 ist die Kennlinie zwischen 20 und 90 % fast flach,
weshalb die lineare Rechnung den Speicher zu voll las (52,7 V galten als
77 % statt 50 %) und das Ziel auf −34 W statt −14 W stellte — Dauereinspeisung
aus einem halb leeren Akku.

Ohne konfigurierten Akku (`batt_strings` leer) gilt immer `target_grid_w`.

### `sustain_floor_w` — die Untergrenze am Wechselrichter (430 W)

**Keine Wunsch-Einstellung, sondern eine Hardware-Eigenschaft.** Gemessen an
929 Limit-Befehlen: Der HMS folgt einem Limit unter ~500 W nur unzuverlässig
(bei 300 W: 44 %, bei 450 W: 90 %, ab 500 W: 99,7 %). Er *kann* wenig Leistung
liefern — er findet per Befehl nur nicht dorthin, sondern fällt auf ~157 W.

Deshalb: Rechnet der Regler ein Ziel-Limit unter 430 W aus, **sendet er es
nicht**, sondern lässt das Limit stehen. Der Wechselrichter bleibt in Ruhe und
liefert weiter. Preis: etwas Überschuss (bei 390 W Nachtlast ~40 W).

`0` schaltet das ab (dann regelt er wie früher bis `min_limit_w` runter — und
schläft nachts ständig ein).

**Und wenn eine zweite Quelle einspeist (Deye, viel Sonne)?** Dann sinkt der
Bedarf am HMS unter den Floor. Halten würde den Überschuss ins Netz schicken,
Abschalten lässt das Netz die Restlast decken. Der Regler wählt automatisch das
Günstigere — Kipppunkt ist **Floor/2 (215 W)**:

| gewünschte HMS-Leistung | Entscheidung | warum |
|---|---|---|
| über 258 W | Limit auf 430 halten | Überschuss < Ersparnis |
| unter 215 W | HMS schlafen legen | Fremdquelle deckt es günstiger |
| dazwischen | bleibt beim Vorherigen | Hysterese gegen Flattern |

Ausnahme: **Bei vollem Akku wird immer gehalten** — der Überschuss wäre sonst
ohnehin abgeregelt, Einspeisen kostet dann nichts.

### `low_points` — die Arbeitspunkt-Leiter (ab 1.8.0)

Halten oder Abschalten sind bei 150 W Hauslast **beide** falsch: das eine
verschenkt 275 W an das Netz, das andere kauft 150 W. Der HMS hat aber ein
drittes, stabiles Plateau bei ~160 W, das er von selbst ansteuert — ein Befehl
um 300 W landet in zwei von drei Fällen dort (gemessen an 36 Tagen
Telemetrie, Details in [eigenverbrauch.md](eigenverbrauch.md)).

`low_points` ist die Liste dieser Punkte als `<Befehl>:<erwartete Leistung>`,
mehrere durch Komma getrennt. Standard: `300:160`.

Der Regler wählt unterhalb des Floors den **günstigsten** Punkt — fehlende
Watt kauft das Netz, überschüssige verschenken Akku-Energie, beides zählt
gleich (bei vollem Akku nur der erste Term). Bei 150 W Bedarf:

| Antwort | Kosten |
|---|---|
| schlafen (37 W) | 113 W Netzbezug |
| Floor halten (425 W) | 275 W verschenkt |
| **Arbeitspunkt (160 W)** | **10 W** |

25 s nach dem Befehl wird geprüft, ob der Inverter wirklich dort gelandet ist.
Ja → die Erwartung zieht nach (der Punkt kalibriert sich selbst). Nein → ein
zweiter Anlauf, danach ist der Punkt 15 min gesperrt und es gilt wieder Floor
oder Schlaf. **Schlimmstenfalls verhält sich der Regler also wie vorher.**

Ein Punkt bleibt mindestens 120 s stehen (Plateaus brauchen Ruhe), und in HA
zeigt der Sensor **„Arbeitspunkt"**, welcher gerade gefahren wird.

Leerer Wert = aus, dann gilt exakt die alte Zwei-Wege-Logik. Eigene Punkte
ausmessen: `scripts/probe_operating_points.py --yes` (Add-on vorher stoppen).

### `min_limit_w` — die Notbremse (50 W, fest verdrahtet)

Tiefstes Limit, das überhaupt gesendet werden darf. Greift nur noch beim
Akku-Schutz (Tiefentladung) und im Failsafe.

## Beispiel: ein Regelzyklus

Angenommen Ziel −20 W, Wechselrichter liefert 300 W, Zähler zeigt +200 W:

1. Fehler = 200 − (−20) = **220 W zu wenig**
2. Nötiges Limit = 300 + 220 = **520 W** → über dem Floor → wird gesendet.

Nachts dagegen: Ziel +20 W, Wechselrichter liefert 430 W, Zähler zeigt −10 W.

1. Fehler = −10 − 20 = **−30 W zu viel**
2. Nötiges Limit = 430 − 30 = **400 W** → **unter dem Floor** → wird *nicht*
   gesendet, Limit bleibt auf 430. (Ohne diese Regel würde der Befehl den
   MPPT aushebeln und der Wechselrichter fiele auf 157 W — dann zieht das
   Haus 230 W aus dem Netz statt 40 W einzuspeisen.)

## Weitere Optionen

| Option | Bedeutung |
|---|---|
| `deadband_w` (15) | Totzone ums Ziel — darunter wird gar nicht geregelt |
| `latency_s` (0) | zusätzliche Wartezeit vor Runter-Korrekturen; 0 = aus, der Smith-Predictor bremst bereits |
| `max_limit_w` (2000) | maximales Limit |
| `failsafe_limit_w` (51) | Limit, wenn das OCR mehrfach hintereinander ausfällt |
| `batt_strings` | belegte Wechselrichter-Eingänge am Akku-Bus, z. B. `1,2,4` — leer = kein Akku-Schutz |
| `batt_low_v` (51,2) / `batt_high_v` (54,4) | Akku-Schwellen (16S LiFePO4: 3,20 / 3,40 V pro Zelle). Stützstellen für das Netz-Ziel — dazwischen wird seit 1.8.0 über den **Ladestand** interpoliert (LiFePO4-Ruhespannungskennlinie), nicht mehr linear über die Spannung |
| `deye_host` | IP des Deye-WLAN-Loggers (z. B. `192.168.178.26`) — leer = aus |
| `deye_user` / `deye_pass` | Login der Logger-Weboberflaeche (Standard `admin` / `admin`) |
| `low_points` (`300:160`) | Arbeitspunkt-Leiter unterhalb des Floors, siehe oben — leer = aus |
| `log_level` (error) | `all` / `error` / `none` |
| `save_samples` (true) | Bilder + Labels sammeln (Grundlage fürs Retraining) |
| `git_*` | Evidence-Sync ins Repo |

## Zweite Quelle: Deye-Balkonwechselrichter

Ist `deye_host` gesetzt, liest das Add-on den Deye **lokal** aus (keine
Cloud) und legt drei Sensoren an: aktuelle Leistung, Ertrag heute, Ertrag
gesamt. Der Gesamtertrag eignet sich direkt als Solarquelle im
HA-Energie-Dashboard.

**Wichtig zur Aktualisierungsrate:** Der WLAN-Logger fragt den
Wechselrichter intern nur alle **~5 Minuten** ab — gemessen am 28.07. mit
57 Proben im 5-Sekunden-Takt: genau eine Wertänderung. Der Beweis ist die
Netzfrequenz: sie stand eine Minute lang bitgenau auf 49,89 Hz, was im
echten Netz unmöglich ist. Web-Statusseite und Modbus (Port 8899) liefern
denselben Cache-Wert; häufigeres Abfragen bringt keine Information.

### Der Deye lässt sich drosseln — über `AT+INVDATA`

**Register 0x0028 = „Active Power Regulation" in Prozent** (0–100).
Beschreibbar, verifiziert am 28.07.

Der Weg dorthin ist nicht offensichtlich. Modbus über Solarman V5 wird
ignoriert, und über die rohe Brücke im `throughput`-Modus ebenfalls —
beides schlägt fehl, egal ob Funktion 0x06 oder 0x10, egal ob Slave 1
oder 0xAA. Was funktioniert, ist der **AT-Kanal auf Port 8899**: Der
Logger begrüßt jede Verbindung mit `AT+YZCMPVER=…` und nimmt danach
AT-Kommandos an. `AT+INVDATA=<länge>,<modbus-hex>` reicht einen
RTU-Frame an den Wechselrichter **durch** — mit **Slave 01**.

Wichtig: **ein Kommando pro Verbindung.** Mehrere hintereinander liefern
die Antworten um einen Befehl versetzt zurück (das hat mich beim ersten
Anlauf in die Irre geführt). Und der Port **48899**, den die
Community-Werkzeuge nutzen, ist auf dieser Firmware geschlossen.

Gemessen: Der Registerwert steht sofort, die Leistung folgt nach
**2–3 Minuten** (82 W → 55 W bei 5 %). Für die Regelung heißt das:
langsamer äußerer Kreis, während der HMS die schnelle Feinregelung macht.

In HA erscheint dadurch ein Schieberegler **„Deye Leistungsbegrenzung"**
(0–100 %), der direkt auf den Wechselrichter durchschlägt.

### Echtzeit: Logger auf `throughput` umstellen

Der Logger hat einen versteckten transparenten Modus, Feld **`yz_tmode`**
(`cmd` ↔ `throughput`) auf `hide_set_edit.html`.

**Achtung, die Seite ist im Browser unbrauchbar**, wenn man sie direkt
aufruft: Ihre Beschriftungen kommen per JavaScript aus dem umgebenden
Frameset, allein geöffnet sieht man nur leere Auswahlfelder.

Umschalten geht deshalb per Kommandozeile — und **nur mit vollständigen
Headern**. Ohne `Referer` und `Content-Type` quittiert der Logger den
POST mit HTTP 200 und verwirft ihn stillschweigend (das kostete beim
ersten Versuch eine halbe Stunde):

```bash
IP=192.168.178.26
H=(-u admin:admin -H "Content-Type: application/x-www-form-urlencoded" -H "Origin: http://$IP")
curl -s "${H[@]}" -H "Referer: http://$IP/hide_set_edit.html" -X POST -d "yz_tmode=cmd" "http://$IP/do_cmd.html"
curl -s "${H[@]}" -H "Referer: http://$IP/restart.html" -X POST -d "HF_PROCESS_CMD=RESTART" "http://$IP/success.html"
```

Für die Gegenrichtung `yz_tmode=throughput` setzen. Der Logger ist danach
~20 s weg. Kontrolle: `curl -s -u admin:admin http://$IP/hide_set_edit.html
| grep yz_tmode`.

Dann hört der Logger auf, selbst zu pollen und zu cachen, und wird zur
reinen Seriell-zu-TCP-Brücke — Modbus-Anfragen gehen direkt an den
Wechselrichter, beliebig schnell. Danach `deye_logger_sn` setzen (der
Modbus-Pfad wird Pflicht) und `DEYE_POLL_S` auf 5 stellen.

**Konsequenzen:** Die HTML-Statusseite zeigt dann keine Werte mehr, und
die Solarman-Cloud-Anbindung entfällt (lokal wird ja gelesen).
Zurückstellen jederzeit über dieselbe Seite. Ausgangswerte für den
Restore: `yz_tmode=cmd`, UART `9600 / 8 / none / 1 / NFC`,
Netz `TCP / SERVER / Port 8899 / Timeout 300`, `inv_tp=21510:Deye`.

Der Wert fließt bewusst **nicht in die Regelung** ein — die sieht die
Deye-Leistung ohnehin sofort in der gemessenen Netzleistung. Er dient der
Anzeige und der Energiebilanz.

## Zählerwechsel oder festgefahrener Zählerstand

Der Zählerstand darf sich konstruktionsbedingt **nie** nennenswert senken —
das schützt vor Fehllesungen, blockiert aber auch zwei legitime Fälle:
der Netzbetreiber tauscht den Zähler (neuer Stand beginnt niedriger),
oder ein alter, falsch zu hoher Stand hat sich festgefahren.

Der **einzige** vorgesehene Weg in beiden Fällen:

1. Add-on stoppen
2. Die Datei `/data/state.json` im Add-on-Container löschen
   (Terminal-Add-on: `rm /addon_configs/*_smartmeter_llm/state.json`
   bzw. über den Datei-Pfad, den das Log beim Start als STATE_FILE nennt)
3. Add-on starten — die Kamera setzt den Stand neu
   (braucht 4 übereinstimmende Lesungen über ≥ 60 s)

Danach in Home Assistant die Langzeitstatistik des Sensors korrigieren
(Entwicklerwerkzeuge → Statistiken → Ausreißer anpassen), sonst verbucht
`total_increasing` den Sprung als Verbrauch.

Alles Weitere (Kamera, OCR-Schwellen, Regler-Feinheiten wie Kick und
Smith-Predictor) ist im Code fest verdrahtet und braucht keine Pflege.
