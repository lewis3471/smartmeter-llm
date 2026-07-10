#!/usr/bin/env python3
"""Trainiert den lokalen Ziffern-Leser aus samples/ (Gemini-Labels).

Klassifikator: k-Nearest-Neighbor (Kosinus) auf normalisierten Digit-Zellen.
Bei fester Kamera und 7-Segment-LCD reicht das (>99% Zellen-Accuracy) und
laeuft in Millisekunden. Ausgabe: scripts/ocr/model.npz + Accuracy-Report.

Nutzung: .venv/bin/python scripts/ocr/train.py [samples-dir]
Regelmaessig neu trainieren, wenn neue Samples da sind — insbesondere nach
Disagreements (samples/disagreements/) im Hybrid-Betrieb.
"""

import json
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from extractor import Extractor, labels_for, prep_cell  # noqa: E402

MODEL_FILE = Path(__file__).with_name("model.npz")
FAILS_DIR = Path(__file__).with_name("train_fails")


def load_samples(root: Path) -> list[tuple[Path, dict]]:
    out = []
    for jf in sorted(root.glob("*/*.json")):
        img = jf.with_suffix(".jpg")
        if not img.exists():
            continue
        data = json.loads(jf.read_text())
        if "kwh" not in data:  # z.B. disagreements/ hat anderes Format
            continue
        out.append((img, data))
    return out


def clean(samples):
    """Offensichtliche Fehl-Labels raus: kWh-Ausreisser gegen den Median."""
    kwhs = sorted(s[1]["kwh"] for s in samples)
    med = kwhs[len(kwhs) // 2]
    good = [s for s in samples
            if abs(s[1]["kwh"] - med) <= 50 and abs(s[1]["w"]) <= 20000]
    return good, len(samples) - len(good)


def collect(ex, subset):
    X, y = [], []
    for img_path, reading in subset:
        lbl = labels_for(reading)
        if lbl is None:
            continue
        kwh_cells, w_cells = ex.cells(cv2.imread(str(img_path)))
        for cell, label in zip(kwh_cells + w_cells, lbl[0] + lbl[1]):
            X.append(prep_cell(cell))
            y.append(label)
    X = np.array(X, np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    return X, np.array(y)


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples")
    samples, dropped = clean(load_samples(root))
    print(f"{len(samples)} Samples ({dropped} als Fehl-Label verworfen)")
    if len(samples) < 20:
        sys.exit("zu wenig Daten")

    ex = Extractor()
    ref = cv2.imread(str(samples[0][0]), cv2.IMREAD_GRAYSCALE)
    ex.set_anchor_reference(ref)

    # Zeitlicher Split: letzte 25% als Holdout
    cut = int(len(samples) * 0.75)
    Xtr, ytr = collect(ex, samples[:cut])
    Xte, yte = collect(ex, samples[cut:])
    print(f"Digit-Zellen: {len(ytr)} Training, {len(yte)} Test")
    print("Klassen:", dict(sorted(Counter(ytr).items())))

    def predict(X, k=3):
        sim = X @ Xtr.T
        top = np.argpartition(-sim, k, axis=1)[:, :k]
        pred, conf = [], []
        for row, srow in zip(top, sim):
            vals, cnt = np.unique(ytr[row], return_counts=True)
            pred.append(vals[cnt.argmax()])
            conf.append(float(srow[row].mean()))
        return np.array(pred), np.array(conf)

    pred, conf = predict(Xte)
    acc = (pred == yte).mean()
    print(f"\nZellen-Accuracy (Holdout): {acc:.4f}  "
          f"(Conf min/median: {conf.min():.2f}/{np.median(conf):.2f})")

    n_img = len(yte) // 11
    pv, tv = pred.reshape(n_img, 11), yte.reshape(n_img, 11)
    ok = int((pv == tv).all(axis=1).sum())
    FAILS_DIR.mkdir(exist_ok=True)
    for i, (img_path, _) in enumerate(samples[cut:cut + n_img]):
        if not (pv[i] == tv[i]).all():
            cv2.imwrite(str(FAILS_DIR / img_path.name), cv2.imread(str(img_path)))
            print(f"  FAIL {img_path.name}: {''.join(pv[i][:6])}|{''.join(pv[i][6:])}"
                  f" vs {''.join(tv[i][:6])}|{''.join(tv[i][6:])}")
    print(f"End-to-End (beide Zeilen exakt): {ok}/{n_img}")

    # Finales Modell: ALLE Daten (Training+Holdout) als kNN-Basis
    Xall = np.concatenate([Xtr, Xte])
    yall = np.concatenate([ytr, yte])
    np.savez_compressed(MODEL_FILE, X=Xall, y=yall, anchor=ex._anchor_ref)
    print(f"Modell ({len(yall)} Zellen) -> {MODEL_FILE}")


if __name__ == "__main__":
    main()
