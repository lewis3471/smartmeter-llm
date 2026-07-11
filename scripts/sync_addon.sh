#!/bin/sh
# Kopiert den aktuellen Code in den Add-on-Ordner (HA baut nur aus dem
# Add-on-Verzeichnis — es muss self-contained sein).
# Nach Code-Aenderungen ausfuehren und version in config.yaml bumpen!
set -e
cd "$(dirname "$0")/.."
cp scripts/meter_reader.py smartmeter_llm/meter_reader.py
cp scripts/feedback.py smartmeter_llm/feedback.py
mkdir -p smartmeter_llm/ocr
cp scripts/ocr/*.py scripts/ocr/model.npz smartmeter_llm/ocr/
echo "Add-on synchronisiert. Nicht vergessen: version in smartmeter_llm/config.yaml erhoehen!"
