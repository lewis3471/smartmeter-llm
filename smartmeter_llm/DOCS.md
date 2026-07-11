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
