# Smartmeter LLM Nulleinspeisung

ESP32-Cam fotografiert das Stromzähler-LCD, lokales kNN-OCR liest Zählerstand
und Leistung (Gemini als Fallback/Kreuz-Check), ein asymmetrischer Regler
steuert den Hoymiles-Inverter über OpenDTU: sofort hochregeln bei Netzbezug,
sanft senken bei Über-Einspeisung.

## Konfiguration

Pflichtfelder: `esphome_api_key` (ESPHome Builder → Gerät → API-Schlüssel),
`opendtu_pass`, `inverter_serial`. Für den Hybrid-Modus zusätzlich
`gemini_api_keys` (Komma-Liste, Rotation bei Quota).

MQTT-Zugang wird automatisch vom Mosquitto-Add-on bezogen; die Sensoren
melden sich per MQTT-Discovery selbst in Home Assistant an.

Details: https://github.com/lewis3471/smartmeter-llm

## Tiefentladeschutz (Akku am Inverter)

Zwei Stufen, die zusammenarbeiten:

**Stufe 1 — Limit-Wächter** (`batt_strings`, z. B. `1,4`): senkt das
Gesamtlimit, wenn die Bus-Spannung unter `batt_low_v` fällt. Bleibt als
Rückfallebene aktiv, auch ohne Stufe 2.

**Stufe 2 — AC-Trennung** (`ac_switch_entity`): trennt den Wechselrichter
über eine schaltbare Steckdose vom Netz. **Nötig, weil ein Limit keine
Abschaltung ist**: das kleinste ansteuerbare Limit des HMS ist 50 W, und
unter ~500 W folgt er einem Limitbefehl nur zu 25–67 % — er fällt in einen
Attraktor bei ~157 W und entlädt den Akku weiter. Leer = Stufe 2 aus,
Verhalten wie vorher.

Damit Stufe 2 die Steckdose schalten darf, hat das Add-on
`homeassistant_api: true`. Es ruft ausschließlich `switch.turn_on/off`,
`number.set_value` und liest Entitätszustände.

### Die wichtigsten Optionen

| Option | Bedeutung |
|---|---|
| `ac_switch_entity` | Schalt-Entität der Steckdose. **Leer = Feature aus** |
| `ac_power_entity` | Leistungssensor der Dose (P110/P115). Optional — Indiz, nie Beweis |
| `ac_deadman_*_entity` | `switch.<dose>_auto_off_enabled`, `number.<dose>_auto_off_minutes`, `sensor.<dose>_auto_off_at`. **Totmann:** die Dose schaltet nach `ac_deadman_s` von selbst ab, das Add-on triggert sie laufend nach. Fällt WLAN, HA oder das Add-on aus, ist die Ruhelage AUS. Leer = kein Totmann, dauerhafte Störungsmeldung |
| `batt_mqtt_prefix` | MQTT-Präfix von OpenDTU-on-Battery (Standard `solar/`) |
| `batt_capacity_ah` | Kapazität in Ah. 0 = die Freigabe stützt sich ersatzweise auf „Victron war in Absorption/Float" |
| `batt_current_charge_positive` | Vorzeichen von `battery/current` beim Laden. Falsch gesetzt heißt: die Freigabe kommt nie oder sofort |
| `ac_off_cell_mv` / `ac_off_soc` | Abschalten. **Zellspannung, nicht Packspannung** — das BMS schützt auf die schwächste Zelle |
| `ac_hard_cell_mv` | Notaus ohne Absetzzeit (Standard 2900 mV, 300 mV über dem JK-Werks-UVP) |
| `ac_on_cell_mv` / `ac_on_soc` | Freigabe. Die Zellspannung wird als **Ruhespannung** verlangt (nur bei kleinem Strom gemessen) — beim Laden zieht der Victron sie sonst sofort über die Schwelle, obwohl der Akku leer ist |
| `ac_off_min_s` / `ac_max_switch_per_day` | Mindest-Aus-Zeit und Tagesbudget gegen Flattern |
| `ac_automatik` | AUS = der Automat schaltet nur noch **ab**, nie ein |

### Neue Entitäten in Home Assistant

`AC-Schutz Zustand`, `AC-Schutz Grund`, `AC-Freigabe blockiert durch`
(Klartext, warum gerade nicht eingeschaltet wird), `Wechselrichter am Netz`,
`AC-Schutz Störung`, `AC-Totmann` (+ Fälligkeit), `AC-Schaltungen heute`,
`Zellspannung Minimum`, `Zell-Drift`, `Akku-Ladestand (BMS)`,
`BMS-Datenalter`, `Nachgeladen seit AC-Aus`, dazu die Bedienelemente
`AC-Automatik` (Schalter), `AC-Hand-Freigabe` (0–240 min) und
`AC-Störung quittieren` (Knopf).

**Einrichtung und Inbetriebnahme in sieben Phasen — bitte der Reihe nach:**
`docs/ac-tiefentladeschutz.md` im Repository. Ohne Phase 1 (die echten
MQTT-Topicnamen des BMS ermitteln) und Phase 2 (liefert das BMS noch Daten,
wenn der Wechselrichter stromlos ist?) steht der Schutz auf Annahmen.
