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

**Stufe 2 — Steckdose** (`ac_switch_entity`): trennt den Wechselrichter
vom Netz, sobald Stufe 1 hält, und schaltet ihn wieder zu, sobald sie
freigibt (frühestens 30 min nach dem Aus). **Nötig, weil ein Limit keine
Abschaltung ist**: mit Limit 50 W zog der HMS in der Nacht 11./12.09.2026
durchgehend ~35 W, bis das BMS bei 41,5 V hart abschaltete. Still ist er
nur stromlos. Leer = Stufe 2 aus.

Mehr ist nicht einzustellen. Der HMS meldet seine DC-Spannung auch
stromlos weiter (über die DTU), Stufe 1 sieht also auch im Aus — eine
zweite Spannungsquelle braucht es nicht.

Damit Stufe 2 schalten darf, hat das Add-on `homeassistant_api: true`.
Es ruft ausschließlich `switch.turn_on` / `switch.turn_off` und liest den
Zustand der Entität. Neuer Sensor in HA: `Wechselrichter-Steckdose`
(`ein`, `aus`, `aus (frei in N min)`, `unbekannt`).

Vor dem Aus wird das Limit persistent auf 50 W gesetzt, damit der HMS
nach dem Einschalten sanft hochkommt. Schaltet jemand von Hand ein,
während Stufe 1 hält, nimmt das Add-on das binnen einer Minute zurück;
ein Aus von Hand gilt wie ein eigenes Aus (30 min Sperre, danach folgt
die Dose wieder Stufe 1). Wer den Wechselrichter länger vom Netz will,
leert `ac_switch_entity`.

Grenze: kein Totmann. Stirbt das Add-on bei eingeschalteter Dose,
schaltet niemand ab. Details: `docs/ac-tiefentladeschutz.md`.
