# smartmeter-llm

Nulleinspeisung & Stromverbrauchs-Logging: Eine ESP32-Cam fotografiert alle 90 s das
LCD des Stromzählers, **Gemini Flash** liest den Zählerstand und die aktuelle Leistung
per Vision-API aus, ein Plausibilitätsfilter verwirft Ausreißer, Home Assistant loggt
die Werte und **OpenDTU** regelt den Hoymiles-Inverter so nach, dass ~0 W (genauer:
+50 W Bezug) am Netzanschluss anliegen.

## Architektur

```
┌──────────────┐  Snapshot   ┌──────────────────┐  JSON {"kwh","w"}
│  ESP32-Cam   │────────────▶│  meter_reader.py │◀──────────────────┐
│ am Zähler-LCD│   (JPEG)    │   (Docker, NUC)  │    Gemini Flash   │
└──────────────┘             └────────┬─────────┘   (Free Tier)     │
                                      │ ▲                           │
                     MQTT             │ │ Plausibilitätsfilter      │
              smartmeter/kwh,w,status │ │ + Regler (±200 W/Zyklus)  │
                                      ▼ │                           │
┌──────────────┐             ┌──────────────────┐    HTTP API   ┌───┴────────┐
│Home Assistant│◀────────────│   MQTT Broker    │   /api/limit  │  OpenDTU   │
│ Energie-Dash │             └──────────────────┘──────────────▶│ + Hoymiles │
└──────────────┘                                                └────────────┘
```

Alle 90 Sekunden (Zyklus):

1. **Snapshot** von der ESP32-Cam holen
2. **Gemini** liest das LCD — Prompt: `Lies das LCD. Antworte nur: {"kwh":int,"w":int}`
3. **Plausibilitätsfilter** (s.u.) — bei Verwurf: letzten Wert halten
4. Werte per **MQTT** an Home Assistant (Logging, Energie-Dashboard)
5. **Regler**: neues Inverter-Limit = PV-Leistung + Netz-Leistung − 50 W Ziel-Bezug,
   gedeckelt auf ±200 W pro Zyklus, non-persistent an OpenDTU

## Komponenten

| Komponente | Adresse | Zugang |
|---|---|---|
| ESP32-Cam (ESPHome) | `192.168.178.58` — Device `esp32-cam-electricity-meter` | Snapshot: `http://192.168.178.58:8080/snapshot` |
| OpenDTU | `http://192.168.178.42` | siehe `.env` (`OPENDTU_USER`/`OPENDTU_PASS`) |
| Home Assistant | NUC | MQTT-Broker (Mosquitto-Add-on) |
| Gemini API | `generativelanguage.googleapis.com` | API-Key in `.env`, Modell `gemini-3.1-flash-lite` |

**Alle Credentials liegen in `.env`** (gitignored, Vorlage: [`.env.example`](.env.example)).

## Gemini Free Tier

- 1.000 Requests/Tag gratis → 90 s-Takt = ~960 Calls/Tag ✅ (**Kosten: 0 €**)
- Trade-off: Google darf Free-Tier-Inhalte zur Produktverbesserung nutzen
- Temperature 0, Thinking aus (`thinkingBudget: 0`) — Denk-Tokens kosten und bringen
  beim LCD-Ablesen nichts
- Prompt minimal halten (eine Zeile), Antwort ist reines JSON

## Plausibilitätsfilter (Pflicht bei probabilistischem Sensor)

Implementiert in [`scripts/meter_reader.py`](scripts/meter_reader.py):

- `|W| > 20 kW` → verwerfen
- `|ΔW|` gegenüber letzter Lesung `> 5 kW` (`MAX_JUMP_W`) → verwerfen
- Zählerstand muss **monoton steigen**; Rückwärtssprung oder `> +10 kWh` → verwerfen
- Kaputtes JSON / Timeout → letzten Wert halten
- **3 Fehler in Folge → Failsafe**: Inverter auf 200 W (`FAILSAFE_LIMIT_W`) statt Vollgas
- Quervergleich PV-Leistung (aus OpenDTU-Livedata) vs. Zählersaldo steckt implizit im
  Regler — das Limit folgt nie schneller als ±200 W/Zyklus

## Regelung

Bewusst träge ausgelegt (bei 90 s-Takt pendelt sonst nichts ein):

- Zielwert **+50 W Netzbezug**, nicht exakt 0 W (`TARGET_GRID_W`)
- Limit-Änderung max. **±200 W pro Zyklus** (`MAX_STEP_W`)
- Hysterese 25 W — kleinere Korrekturen werden nicht gesendet (`HYSTERESIS_W`)
- Limit wird **non-persistent** gesetzt (`limit_type: 0`) — schont den Flash von
  DTU und Inverter
- Grenzen: `MIN_LIMIT_W` (50 W) bis `MAX_LIMIT_W` (1500 W, an HMS-Modell anpassen)

## Setup

### 1. ESP32-Cam (ESPHome)

Das Script holt das Bild über die **ESPHome Native API** (Port 6053, verschlüsselt):
`ESPHOME_HOST` + `ESPHOME_API_KEY` (Base64-Key aus dem ESPHome Builder) in `.env`.
Ablauf pro Zyklus: Blitz-LED an → Belichtung einpendeln lassen (5 Warm-up-Frames,
die Auto-Exposure passt sich nur während laufender Aufnahmen an!) → letzten Frame
nutzen → LED aus. Test: `.venv/bin/python scripts/fetch_snapshot_esphome.py test.jpg`

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
docker compose logs -f   # kwh=35698 w=-1151 limit=1100 ...
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
