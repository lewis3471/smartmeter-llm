#!/usr/bin/env python3
"""Audit & Relabel: repariert Gemini-Fehl-Labels in training-data/.

Gemini verschluckt gelegentlich das Minuszeichen (oder halluziniert eins).
Die Minus-Geometrie (Masse nur im Mittelband) ist auf unseren Zellen nahezu
perfekt trennscharf — sie ist hier der Schiedsrichter:

- Label sagt positiv, Geometrie sagt Minus (oder umgekehrt):
  -> Vorzeichen flippen, WENN die Ziffern-Zellen (kNN, ohne Vorzeichen)
     zum Betrag des Labels passen (sonst war auch die Ziffer falsch)
  -> andernfalls W-Label entfernen (kWh-only Label, train.py kann das)

Nutzung: .venv/bin/python scripts/ocr/relabel.py [training-data] [--dry-run]
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from extractor import Extractor, labels_for, minus_ratio  # noqa: E402
from local_reader import LocalReader  # noqa: E402


def sign_contradiction(w_cells, w_labels) -> bool:
    for cell, label in zip(w_cells, w_labels):
        r = minus_ratio(cell)
        if (label == "_" and r > 0.75) or (label == "-" and r < 0.3):
            return True
    return False


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    root = Path(args[0]) if args else Path("training-data")
    reader = LocalReader()
    ex = reader.ex

    flipped = stripped = ok = 0
    for jf in sorted(root.glob("*/*.json")):
        img_path = jf.with_suffix(".jpg")
        if not img_path.exists():
            continue
        try:
            reading = json.loads(jf.read_text())
        except ValueError:
            continue
        if "kwh" not in reading or "w" not in reading:
            continue
        lbl = labels_for(reading)
        if lbl is None or not lbl[1]:
            continue
        img = cv2.imread(str(img_path))
        if img is None:
            continue
        _, w_cells = ex.cells(img)
        if not sign_contradiction(w_cells, lbl[1]):
            ok += 1
            continue
        # Kandidat: Vorzeichen geflippt — passt der dann geometrisch UND
        # stimmen die Ziffern (kNN ohne Vorzeichen-Slot) mit dem Betrag?
        flipped_reading = dict(reading, w=-reading["w"])
        flbl = labels_for(flipped_reading)
        digits_ok = False
        if flbl is not None and flbl[1] and not sign_contradiction(w_cells, flbl[1]):
            pred, _ = reader._predict(list(w_cells))
            pred_digits = "".join(p for p in pred if p.isdigit())
            digits_ok = pred_digits == str(abs(reading["w"]))
        if digits_ok:
            flipped += 1
            action = f"FLIP  w {reading['w']:+d} -> {-reading['w']:+d}"
            if not dry:
                jf.write_text(json.dumps(flipped_reading))
        else:
            stripped += 1
            action = f"STRIP w={reading['w']:+d} (Ziffern unsicher -> kWh-only)"
            if not dry:
                del reading["w"]
                jf.write_text(json.dumps(reading))
        print(f"{jf}: {action}")

    print(f"\n{ok} sauber, {flipped} Vorzeichen korrigiert, "
          f"{stripped} auf kWh-only reduziert{' (dry-run)' if dry else ''}")


if __name__ == "__main__":
    main()
