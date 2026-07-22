#!/bin/bash
# Ein-Kommando-Retrain auf der Trainings-Maschine:  make retrain
# Pull -> Konsens-Labels -> Geometrie-Audit -> Training -> Holdout-Gate
# -> Push. Der NUC laedt das Modell beim naechsten Sync per Hot-Reload.
# Ausgeloest wird das bewusst von Hand — der HA-Sensor "OCR Retrain
# faellig" sagt, WANN es sich lohnt (Autonomie wuerde sich vergiften,
# siehe CHANGELOG 1.7.8).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
GATE=0.90

echo "== Pull =="
git pull --rebase origin main

echo "== Konsens-Labels =="
$PY scripts/ocr/consensus_label.py training-data

echo "== Vorzeichen-/Geometrie-Audit =="
$PY scripts/ocr/relabel.py training-data

echo "== Training =="
OUT=$($PY scripts/ocr/train.py training-data | tee /dev/stderr)
ACC=$(echo "$OUT" | sed -n 's/.*Zellen-Accuracy (Holdout): \([0-9.]*\).*/\1/p')
if [ -z "$ACC" ]; then
    echo "FEHLER: keine Holdout-Accuracy im Trainings-Output" >&2
    exit 1
fi
if ! $PY -c "exit(0 if float('$ACC') >= $GATE else 1)"; then
    echo "GATE VERLETZT: Holdout $ACC < $GATE — Modell wird NICHT gepusht." >&2
    echo "Lokales model.npz pruefen oder verwerfen:" >&2
    echo "    git checkout scripts/ocr/model.npz" >&2
    exit 1
fi

echo "== Push (Holdout $ACC >= $GATE) =="
for i in 1 2 3; do
    git add -A
    git commit -m "ocr: retrain (make retrain, holdout $ACC)" || true
    git pull --rebase origin main || true
    if git push origin refs/heads/main:refs/heads/main; then
        echo "gepusht — NUC uebernimmt das Modell beim naechsten Sync"
        exit 0
    fi
    sleep 5
done
echo "Push nach 3 Versuchen fehlgeschlagen" >&2
exit 1
