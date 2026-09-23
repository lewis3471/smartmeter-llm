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
from extractor import Extractor, labels_for, minus_ratio, prep_cell, shifted_variants  # noqa: E402
from local_reader import FALLBACK_MARGIN  # noqa: E402

MODEL_FILE = Path(__file__).with_name("model.npz")
FAILS_DIR = Path(__file__).with_name("train_fails")


def load_samples(root: Path) -> list[tuple[Path, dict]]:
    out = []
    for jf in sorted(root.glob("*/*.json")):
        if jf.parent.name == "quarantine":  # aussortierte Labels, nie trainieren
            continue
        img = jf.with_suffix(".jpg")
        if not img.exists():
            continue
        data = json.loads(jf.read_text())
        if "kwh" not in data:  # z.B. disagreements/ hat anderes Format
            continue
        out.append((img, data))
    return out


KWH_MAX = 99_999  # Anzeige: fuehrende 0 + fuenf Stellen (036291)


def day_of(img: Path) -> str:
    """auto/ und seg/ tragen das Datum im Dateinamen (20260923_081642),
    die Tagesordner nur die Uhrzeit (20260923/081642). Frueher stand hier
    fuer alle name[:8] — bei Tagesordnern "081642.j", also eine eigene
    Gruppe pro Bild: deren Labels wurden nie gegen den Tag geprueft."""
    return img.name[:8] if img.name[:8].isdigit() else img.parent.name[:8]


def clean(samples):
    """Offensichtliche Fehl-Labels raus: kWh-Ausreisser gegen den TAGES-
    Median (der Zaehler laeuft ueber die Korpus-Lebensdauer weiter — ein
    globaler Median wuerde die neuesten Samples verwerfen). Sechsstellige
    kWh vorher und unabhaengig davon: an Tagen, an denen Gemini fast nur
    die Nachkomma-Signatur las (358914 statt 35891), ist der Median selbst
    sechsstellig und haelt die falschen Labels statt der richtigen."""
    n = len(samples)
    samples = [s for s in samples if s[1]["kwh"] <= KWH_MAX]
    by_day = {}
    for s in samples:
        by_day.setdefault(day_of(s[0]), []).append(s[1]["kwh"])
    day_med = {d: sorted(v)[len(v) // 2] for d, v in by_day.items()}
    good = [s for s in samples
            if abs(s[1]["kwh"] - day_med[day_of(s[0])]) <= 10
            and abs(s[1].get("w", 0)) <= 20000]
    return good, n - len(good)


def collect(ex, subset):
    X, y, slots = [], [], []
    for img_path, reading in subset:
        lbl = labels_for(reading)
        if lbl is None:
            continue
        kwh_cells, w_cells = ex.cells(cv2.imread(str(img_path)))
        # Label-Audit: widerspricht die Minus-Geometrie dem (Gemini-)Label
        # der W-Zeile, ist das Vorzeichen im Label vermutlich falsch ->
        # W-Zeile dieses Samples nicht ins Training (kWh-Zeile bleibt)
        w_labels = lbl[1]
        for cell, label in zip(w_cells, w_labels):
            r = minus_ratio(cell)
            if (label == "_" and r > 0.75) or (label == "-" and r < 0.3):
                w_labels = []
                break
        for slot, (cell, label) in enumerate(
                zip(kwh_cells + w_cells, lbl[0] + w_labels)):
            X.append(prep_cell(cell))
            y.append(label)
            slots.append(slot)
    X = np.array(X, np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    return X, np.array(y), np.array(slots)


DEDUP_COS = 0.99


def dedup(X, y, slots, thr=DEDUP_COS):
    """Beinahe-Duplikate raus: je (Slot, Label) faellt eine Zelle weg, wenn
    eine bereits behaltene derselben Klasse Kosinus >= thr hat.

    Bei fester Kamera ist fast jede Zelle eine Kopie derselben Ziffer
    (Slot 0 ist immer eine 3). Ungedeckelt wuchs das Modell bis zum
    23.09.2026 auf 262k Zellen: 153 MB model.npz (GitHub nimmt max.
    100 MB) und ~670 MB RAM im Add-on. Gemessen am Holdout 15.-23.09.:
    0.99 haelt die Accuracy (0.9891 vs 0.9892 ungedeckelt, gleiche
    Konfidenz-Verteilung) bei 44 % der Basis; 0.985 kippte beim Split
    01.09. auf 0.929, gleichmaessiges Ausduennen kostete 1-2 Punkte."""
    keep = []
    for key in sorted(set(zip(slots.tolist(), y.tolist()))):
        idx = np.flatnonzero((slots == key[0]) & (y == key[1]))[::-1]  # neueste zuerst
        kept = np.empty((0, X.shape[1]), X.dtype)
        for c in range(0, len(idx), 256):
            chunk = idx[c:c + 256]
            V = X[chunk]
            frei = ((V @ kept.T).max(axis=1) < thr if len(kept)
                    else np.ones(len(chunk), bool))
            sel = []
            for j in np.flatnonzero(frei):
                if not sel or (V[sel] @ V[j]).max() < thr:
                    sel.append(j)
            kept = np.vstack([kept, V[sel]])
            keep.extend(chunk[sel].tolist())
    keep = np.sort(np.array(keep, dtype=int))
    return X[keep], y[keep], slots[keep]


def augment(X, y, slots):
    """Shift-Varianten (+-1/2px) fuer Ziffern-Zellen in die kNN-Basis:
    dieselbe Ziffer sitzt je Box leicht versetzt — so generalisiert sie
    ueber alle Positionen (der Slot-Fallback matcht dann sauber)."""
    ax, ay, aslot = [X], [y], [slots]
    add_x, add_y, add_s = [], [], []
    for vec, lab, sl in zip(X, y, slots):
        if lab == "_":
            continue
        for v in shifted_variants(vec):
            n = np.linalg.norm(v) + 1e-9
            add_x.append(v / n)
            add_y.append(lab)
            add_s.append(sl)
    if add_x:
        ax.append(np.array(add_x, np.float32))
        ay.append(np.array(add_y))
        aslot.append(np.array(add_s))
    return np.concatenate(ax), np.concatenate(ay), np.concatenate(aslot)


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
    Xtr0, ytr0, str0 = collect(ex, samples[:cut])
    Xte, yte, ste = collect(ex, samples[cut:])
    entdoppelt = dedup(Xtr0, ytr0, str0)
    print(f"Dedup (Kosinus {DEDUP_COS}): {len(ytr0)} -> "
          f"{len(entdoppelt[1])} Zellen im Training")
    Xtr, ytr, str_ = augment(*entdoppelt)
    print(f"Digit-Zellen: {len(ytr)} Training (mit Shift-Augmentierung), "
          f"{len(yte)} Test")
    print("Klassen:", dict(sorted(Counter(ytr).items())))

    kandidaten = {}

    def predict(X, slots, k=3):
        # Wie LocalReader._predict: Beispiele aus demselben roten Kasten
        # zuerst. Ziffern, die dort nie vorkamen, fallen auf die anderen
        # Kaesten zurueck — aber nur, wenn die um FALLBACK_MARGIN besser
        # passen (Begruendung dort).
        pred, conf = [], []
        for feature, slot in zip(X, slots):
            if slot not in kandidaten:
                present = set(ytr[str_ == slot])
                kandidaten[slot] = (np.flatnonzero(str_ == slot),
                                    np.flatnonzero((str_ != slot)
                                                   & ~np.isin(ytr, list(present))))
            own, fb = kandidaten[slot]
            so = Xtr[own] @ feature
            sf = Xtr[fb] @ feature
            if len(fb) and (not len(own) or sf.max() > so.max() + FALLBACK_MARGIN):
                idx, scores = np.concatenate([own, fb]), np.concatenate([so, sf])
            else:
                idx, scores = own, so
            kk = min(k, len(scores))
            row = np.argpartition(-scores, kk - 1)[:kk]
            labels, values = ytr[idx[row]], scores[row]
            vals, cnt = np.unique(labels, return_counts=True)
            pred.append(vals[cnt.argmax()])
            conf.append(float(values.mean()))
        return np.array(pred), np.array(conf)

    pred, conf = predict(Xte, ste)
    acc = (pred == yte).mean()
    print(f"\nZellen-Accuracy (Holdout): {acc:.4f}  "
          f"(Conf min/median: {conf.min():.2f}/{np.median(conf):.2f})")

    FAILS_DIR.mkdir(exist_ok=True)
    if len(yte) % 11 == 0:  # kWh-only manual labels have six cells
        n_img = len(yte) // 11
        pv, tv = pred.reshape(n_img, 11), yte.reshape(n_img, 11)
        ok = int((pv == tv).all(axis=1).sum())
        for i, (img_path, _) in enumerate(samples[cut:cut + n_img]):
            if not (pv[i] == tv[i]).all():
                cv2.imwrite(str(FAILS_DIR / img_path.name), cv2.imread(str(img_path)))
                print(f"  FAIL {img_path.name}: {''.join(pv[i][:6])}|{''.join(pv[i][6:])}"
                      f" vs {''.join(tv[i][:6])}|{''.join(tv[i][6:])}")
        print(f"End-to-End (beide Zeilen exakt): {ok}/{n_img}")
    else:
        print("End-to-End: übersprungen (kWh-only Labels im Holdout)")

    # Finales Modell: ALLE Daten (Training+Holdout, entdoppelt, augmentiert)
    # als kNN-Basis
    Xall, yall, sall = augment(*dedup(np.concatenate([Xtr0, Xte]),
                                      np.concatenate([ytr0, yte]),
                                      np.concatenate([str0, ste])))
    np.savez_compressed(MODEL_FILE, X=Xall.astype(np.float16), y=yall,
                        slots=sall, anchor=ex._anchor_ref)
    print(f"Modell ({len(yall)} Zellen) -> {MODEL_FILE}")


if __name__ == "__main__":
    main()
