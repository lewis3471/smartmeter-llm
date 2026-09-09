# smartmeter-llm

Nulleinspeisung & Stromverbrauchs-Logging im **Sekundentakt**: Eine ESP32-Cam
(Dauerverbindung, LED gedimmt an) fotografiert das LCD des Stromzählers alle
~0,5 s, ein **lokales kNN-OCR** liest Zählerstand und Leistung in <10 ms
(Gemini nur noch als Kreuz-Check/Fallback), ein Plausibilitätsfilter verwirft
Ausreißer, Home Assistant loggt per MQTT-Discovery, und ein asymmetrischer
Regler steuert den Hoymiles-Inverter über **OpenDTU**: bei Netzbezug sofort
und ungebremst hochregeln, bei Über-Einspeisung sanft senken — Ziel −50 W
(leichte Einspeisung), kein Cent Netzbezug, wenn die Sonne liefern kann.

## Architektur

```
┌──────────────┐ ESPHome API ┌──────────────────┐  Kreuz-Check ~5min
│  ESP32-Cam   │────────────▶│  meter_reader.py │◀ ─ ─ ─ ─ ─ ─ ─ ─ ─┐
│ am Zähler-LCD│ (~0,5s/Frame│  lokales kNN-OCR │    Gemini Flash    │
│ LED dauerhaft│  LED-Steuer)│  (<10ms, c≥0.85) │    (Fallback)      │
└──────────────┘             └────────┬─────────┘                    │
                                      │ ▲ Plausibilitätsfilter,      │
                     MQTT             │ │ Median-3, Re-Baseline,     │
              smartmeter/kwh,w,status │ │ asym. Regler (v3)          │
                                      ▼ │                            │
┌──────────────┐             ┌──────────────────┐    HTTP API   ┌────┴───────┐
│Home Assistant│◀────────────│   MQTT Broker    │   /api/limit  │  OpenDTU   │
│ Energie-Dash │  Discovery  └──────────────────┘──────────────▶│ + Hoymiles │
└──────────────┘                                                └────────────┘
```

Jeder Zyklus (~0,4–0,5 s, `INTERVAL_S=0`):

1. **Frame** über die persistente ESPHome-Verbindung (Belichtung bleibt
   eingependelt, kein Warm-up)
2. **Lokales OCR** liest `{"kwh","w"}`; bei Confidence < `OCR_MIN_CONF`,
   Lesefehler oder als Kreuz-Check (alle `CROSS_CHECK_EVERY` Zyklen) fragt
   der Hybrid-Modus **Gemini** — Abweichungen werden Trainingsdaten
3. **Plausibilitätsfilter** + Median-3 — bei Verwurf: letzten Wert halten;
   hartnäckig konsistente „unplausible" Werte heilt die **Re-Baseline**
   (Gemini-Verifikation)
4. **MQTT** an Home Assistant (Auto-Discovery, 4 Sensoren)
5. **Regler v3** (asymmetrisch, absolut): `Limit = PV + Netz − Ziel` —
   hoch sofort in einem Befehl, runter nur bei echter Über-Einspeisung
   mit Totzeit-Guard

## Komponenten

| Komponente | Adresse | Zugang |
|---|---|---|
| ESP32-Cam (ESPHome) | `192.168.178.58` — Device `esp32-cam-electricity-meter` | Native API Port 6053, Key in `.env` |
| OpenDTU | `http://192.168.178.42` | siehe `.env` (`OPENDTU_USER`/`OPENDTU_PASS`) |
| Home Assistant | NUC (`192.168.178.64`) | MQTT-Broker (Mosquitto-Add-on), Zugang via `.env` bzw. automatisch im Add-on |
| Gemini API (nur Fallback) | `generativelanguage.googleapis.com` | Keys in `.env`, Rotation über Modelle+Keys |

**Alle Credentials liegen in `.env`** (gitignored, Vorlage: [`.env.example`](.env.example)).

## NUC feedback loop: Fehlerbilder → Git → retrained model

The reader now writes an evidence event (JSON error record and the exact JPEG)
for every rejected reading, including LCD segment tests and the recurring
`35770` misread. Gemini-confirmed local/Gemini differences remain in
`samples/disagreements/` and are the labelled data used for training. Thus the
same digit classifier learns each position of the LCD grid; it is not trying to
learn the full meter value as one image.

Run the feedback worker on a normal, writable clone on the NUC (not inside the
Home Assistant add-on container). It copies new evidence into the ignored local
`training-data/` directory, retrains after 10 or more labelled disagreements,
commits the evidence and both model copies, and pushes. The meter loop only
writes files, so a slow or offline Git remote can never delay control.

```bash
git clone git@github.com:lewis3471/smartmeter-llm.git /opt/smartmeter-llm
cd /opt/smartmeter-llm
cp .env.example .env  # set SAVE_SAMPLES_DIR=/opt/smartmeter-llm/samples
sudo cp scripts/nuc-feedback-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nuc-feedback-sync.timer
```

Use a repository-scoped SSH deploy key with write access, installed for the
NUC account (for example `~/.ssh/id_ed25519`); do **not** put a Git token in
`.env`, add-on configuration, logs, or this repository. The timer runs every
30 seconds. For a manual run:

```bash
python3 scripts/nuc_feedback_sync.py --samples /opt/smartmeter-llm/samples --push
```

`training-data/` is deliberately gitignored: it may contain meter images. The
worker stages it explicitly, so it is versioned only by its dedicated commits.
This gives the NUC and every checked-out deployment the same retrained
`model.npz` without accidentally committing arbitrary local sample folders.

### CI validation (read-only, no commit-back)

A GitHub Actions workflow ([`.github/workflows/ocr-validate.yml`](.github/workflows/ocr-validate.yml))
runs on every `ocr: sync evidence*` commit to `main`. It retrains the model on
the evidence already in the commit and publishes the holdout accuracy as a run
summary + commit annotation, failing the check below the configured floor
(default `0.95`). It is **validation only**: it has `permissions: contents:
read` and never commits or pushes `model.npz`, so the NUC worker stays the
single writer of the model. This surfaces bad labels and training regressions
without racing the local trainer. Trigger it manually from the Actions tab to
set a custom `min_cell_acc` floor.

### Home Assistant OS add-on (no SSH / sudo on the NUC)

Version 1.4.4 can run the same worker inside the add-on. Add a **write-enabled
repository deploy key** in GitHub, then paste the private key only in the
add-on's YAML configuration under `git_deploy_key`. It is written with mode
`0600` below `/data` and is never logged or committed. Set:

```yaml
save_samples: true
git_sync_enabled: true
git_repository: git@github.com:lewis3471/smartmeter-llm.git
git_branch: main
git_deploy_key_base64: paste-one-line-base64-key-here
git_sync_interval_s: 30
```

On first start the add-on clones the repository into its persistent `/data`,
then uploads evidence and retrained models itself. No HA host shell access is
needed.

Create the one-line Base64 value on a trusted Mac with
`base64 < ~/.ssh/smartmeter_ha_deploy | tr -d '\n' | pbcopy`, then paste it
into `git_deploy_key_base64`. This avoids Home Assistant configuration fields
altering the OpenSSH key's required line breaks.

## Leseweg: lokales OCR zuerst, Gemini als Berater

Primärleser ist das **lokale kNN-OCR** (siehe unten) — kostenlos, <10 ms,
keine Rate-Limits, dadurch der Sekundentakt. Gemini (`-latest`-Aliasse mit
Fallback-Rotation über Modelle und mehrere API-Keys bei 429/503) dient nur
noch als:

- **Kreuz-Check** alle `CROSS_CHECK_EVERY` Zyklen (~5 min)
- **Fallback** bei niedriger OCR-Confidence (gedrosselt via
  `GEMINI_COOLDOWN_S`, dunkle Bilder werden gar nicht erst gesendet)
- **Label-Quelle**: Jede bestätigte Lesung und jede Abweichung wird
  Trainingsmaterial — das OCR verbessert sich selbst

Free-Tier-Budget: ~300–500 Calls/Tag, weit unter den Limits. Kosten: 0 €.

## Plausibilitätsfilter & Selbstheilung

Implementiert in [`scripts/meter_reader.py`](scripts/meter_reader.py):

- `|W| > 20 kW`, `|ΔW| > MAX_JUMP_W`, LCD-Segmenttest (888888, lokal
  erkannt), `kwh ≤ 0` → verwerfen
- Zählerstand **monoton steigend**, max. +2 kWh Sprung
- **Median-3** als Regler-Eingang: einzelne Übergangs-Frames regeln nicht
- **Re-Baseline**: dieselbe „unplausible" kWh 4× in Folge → Gemini
  verifiziert (2 Versuche, mit Cooldown) → bei Bestätigung neuer Stand.
  Heilt vergiftete Zustände, statt dauerhaft zu blockieren
- **Dunkelbild-Erkennung**: LED aus (z.B. fremde Automation) → Reassert
  in ~4 s, kein Gemini-Call für schwarze Bilder
- `FAILSAFE_AFTER` Fehler in Folge → `FAILSAFE_LIMIT_W`

## Regelung (v3: asymmetrisch, absolut)

Design-Prinzip: Die Messung ist vertrauenswürdig (OCR sekündlich, PV
sekündlich) und die Kosten sind asymmetrisch — Netzbezug kostet Geld,
Einspeisung nicht. Details: [docs/regler-v2-plan.md](docs/regler-v2-plan.md)

- `wanted = PV + Netzleistung − TARGET_GRID_W` — das physikalisch korrekte
  Limit direkt aus der Messung, kein Herantasten
- **Hoch: sofort, ungebremst, ein Befehl** — auch bei Wolken bleibt das
  Limit auf Bedarfsniveau vorpositioniert (Sonnenrückkehr deckt ohne Anlauf)
- **Runter: nur bei echter Über-Einspeisung** (unter `TARGET − DEADBAND_W`),
  mit Totzeit-Guard `LATENCY_S` (~8 s: OpenDTU-Funk + MPPT + LCD + Median)
- Limit non-persistent (`limit_type: 0`) — schont den Flash von DTU/Inverter
- Grenzen: `MIN_LIMIT_W` bis `MAX_LIMIT_W` (2000 W beim HMS-2000-4T)
- Gemessene Reaktionskette: Erkennung ≤1 s + Funk/MPPT 2–6 s

## Setup

### 1. ESP32-Cam (ESPHome)

Das Script holt Bilder über die **ESPHome Native API** (Port 6053, verschlüsselt):
`ESPHOME_HOST` + `ESPHOME_API_KEY` (Base64-Key aus dem ESPHome Builder) in `.env`.

- `CAM_MODE=continuous` (Standard): persistente Verbindung, LED dauerhaft
  gedimmt an (`LED_BRIGHTNESS`, 45 %), Belichtung bleibt eingependelt →
  ~0,5 s pro Frame, Sekundentakt möglich. Chip-Temperatur dabei ~62 °C (ok);
  Sensor via `homeassistant/esphome-camera-additions.yaml`
- `CAM_MODE=flash`: LED pro Zyklus an/aus mit Warm-up-Frames — für lange
  Intervalle (die Auto-Exposure adaptiert nur während laufender Aufnahmen!)

Test: `.venv/bin/python scripts/fetch_snapshot_esphome.py test.jpg`

Fallback: `esp32_camera_web_server` (Port 8080, mode snapshot) im ESPHome-YAML
aktivieren und `CAM_SNAPSHOT_URL` nutzen — dann entfällt aber die LED-Steuerung.

Hinweis EasyMeter ESY11: Das LCD zeigt zyklisch auch den Segmenttest (888888) —
der Plausibilitätsfilter verwirft solche Lesungen automatisch.

### 2. OpenDTU

- Inverter-**Seriennummer** aus dem OpenDTU-WebUI (`http://192.168.178.42`) in
  `.env` → `INVERTER_SERIAL` eintragen
- In den Inverter-Einstellungen muss „Limit steuern" erlaubt sein

Limit-API (macht das Script automatisch):

```bash
curl -u "admin:PASSWORT" http://192.168.178.42/api/limit/config \
  -d 'data={"serial":"SERIENNUMMER","limit_type":0,"limit_value":800}'
```

### 3. MQTT & Home Assistant

- Mosquitto-Add-on: MQTT-User anlegen, in `.env` eintragen (`MQTT_*`)
- [`homeassistant/packages/smartmeter.yaml`](homeassistant/packages/smartmeter.yaml)
  nach `/config/packages/` kopieren (Packages in `configuration.yaml` aktivieren)
- Energie-Dashboard: `sensor.stromzahler_zahlerstand` als Netzbezug eintragen
- Enthält eine Automation, die bei Failsafe/Ausfall eine Benachrichtigung schickt

### 4a. Als Home-Assistant-Add-on (empfohlen auf HAOS)

Dieses Repo ist ein **HA-Add-on-Repository** (`repository.yaml` +
[`smartmeter_llm/`](smartmeter_llm/config.yaml)). Installation:

1. HA: Einstellungen → Add-ons → Add-on Store → ⋮ → **Repositories** →
   `https://github.com/lewis3471/smartmeter-llm` hinzufügen
2. „Smartmeter LLM Nulleinspeisung" erscheint im Store → Installieren
3. Konfiguration ausfüllen (ESPHome-API-Key, OpenDTU-Passwort, Gemini-Keys —
   Rest ist vorbelegt) → Starten. MQTT kommt automatisch vom Mosquitto-Add-on
4. **Updates**: Code ändern → `scripts/sync_addon.sh` → `version` in
   `smartmeter_llm/config.yaml` erhöhen → committen/pushen. HA zeigt dann
   einen Update-Knopf am Add-on

Hinweis: Das OCR-Modell (`model.npz`) wird mitverteilt; nach einem
Retraining ebenfalls sync + Versions-Bump.

### 4b. Alternativ: Docker auf beliebigem Host

```bash
cp .env.example .env   # Werte eintragen (bzw. vorhandene .env nutzen)
touch state.json
docker compose up -d --build
docker compose logs -f   # kwh=35708 w=-52 pv=1456 limit=1503 [local c=0.97]
```

Erst ohne `INVERTER_SERIAL` laufen lassen → nur Lesen + MQTT-Logging, keine
Regelung. Wenn die Werte ein paar Tage stimmen, Serial eintragen und die
Nulleinspeisung scharf schalten.

## Lokales OCR (READER_MODE)

Ab Branch `feature/ocr` kann das LCD **lokal** gelesen werden — ohne Cloud-Call,
beliebig schnelles Intervall, keine Rate-Limits:

- `READER_MODE=hybrid` (empfohlen): lokales OCR liest; bei Confidence
  < `OCR_MIN_CONF`, Lesefehler oder jedem `CROSS_CHECK_EVERY`-ten Zyklus wird
  Gemini gefragt. Abweichungen landen in `samples/disagreements/` als neue
  Trainingsfälle.
- `READER_MODE=local`: nur OCR, kein Gemini (kein API-Key nötig)
- `READER_MODE=gemini`: nur Cloud (Ausgangszustand)

Funktionsweise: feste Kameraposition → Ziffern liegen an bekannten
Pixelpositionen ([scripts/ocr/extractor.py](scripts/ocr/extractor.py), Grid in
`config.json` überschreibbar). Ein Anker-Patch (kWh-Label) kompensiert
Kameradrift per Template-Matching. Jede Ziffernzelle wird per
kNN (Kosinus, k=3) gegen die gesammelten, Gemini-gelabelten Samples
klassifiziert. Klassen: `0-9`, `-`, leer.

**Training/Retraining** (nach neuen Samples, besonders Disagreements):

```bash
.venv/bin/python scripts/ocr/train.py   # -> scripts/ocr/model.npz + Report
```

Der Report zeigt Zellen-Accuracy, End-to-End-Quote und legt Fehlbilder in
`scripts/ocr/train_fails/` ab. Falsch gelabelte Samples (Gemini-Fehler)
werden per kWh-Median-Check automatisch aussortiert.

**Weg zu 5-s-Intervall**: Lokal ist das Lesen in <10 ms erledigt — der
Flaschenhals ist die Kamera-Belichtung (~5,5 s LED + Warm-up-Frames, weil die
Auto-Exposure nur bei laufender Aufnahme adaptiert). Dafür im ESPHome-YAML
feste Belichtung setzen (`aec_mode: manual` + kalibrierter `aec_value`), dann
reicht 1 Frame und die LED ist nur ~1 s an. Danach `INTERVAL_S=5` setzen und
`MAX_STEP_W` entsprechend verkleinern (Regelung wird 30× schneller!).

## MQTT-Topics

| Topic | Inhalt |
|---|---|
| `smartmeter/kwh` | Zählerstand (kWh, monoton) |
| `smartmeter/w` | Momentanleistung (W, negativ = Einspeisung) |
| `smartmeter/limit_w` | aktuell gesetztes Inverter-Limit |
| `smartmeter/status` | `ok` / `retry` / `failsafe` / `error` |

## Sicherheit

- `.env` und `state.json` sind gitignored — **niemals committen**
- ⚠️ Der Gemini-API-Key wurde im Klartext (Chat) geteilt → im
  [Google AI Studio](https://aistudio.google.com/apikey) **rotieren** und den neuen
  Key nur in `.env` ablegen
- OpenDTU-Passwort bei Gelegenheit ändern (WebUI → Settings → Security)

## Akku-Wächter (Victron an einzelnen Strings)

Hängt an einzelnen Inverter-Eingängen ein Akku (z.B. Victron-Laderegler an
String 1+4, Solar an String 2), schützt der Wächter vor Tiefentladung:
`BATT_STRINGS=1,4`, unter `BATT_LOW_V` (36 V) wird das Gesamtlimit adaptiv
gesenkt, bis die gemessene Entnahme aus den Akku-Strings ~0 W ist
(der HMS kann nicht pro String limitieren — der Wächter regelt deshalb
per Feedback auf die OpenDTU-Livedaten). Eine Sonnen-Probe hebt das Cap
langsam wieder an; ab `BATT_HIGH_V` (38 V) sind die Akku-Strings wieder
frei. Akku-Spannung und Schutz-Status erscheinen als eigene HA-Sensoren.

**Wichtig:** In OpenDTU-on-Battery den Dynamic Power Limiter deaktivieren —
zwei Regler am selben Limit arbeiten gegeneinander.

## Retraining

Der NUC trainiert nie selbst — er meldet per HA-Sensor **„OCR Retrain
fällig"** (mit Grund), wenn sich ein Training lohnt (Seg-Schiedsrichter-
Einsätze, Failsafes oder viele Disagreements). Dann auf der
Trainings-Maschine:

```bash
make retrain
```

Das zieht die Evidence, erzeugt Konsens-Labels, auditiert Vorzeichen,
trainiert, prüft das Holdout-Gate (≥ 0,90) und pusht — der NUC übernimmt
das Modell beim nächsten Sync automatisch per Hot-Reload.
