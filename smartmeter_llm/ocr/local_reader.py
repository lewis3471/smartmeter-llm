"""Lokaler LCD-Leser: kNN-Klassifikation der Digit-Zellen, kein Cloud-Call.

Nutzung:
    reader = LocalReader()          # laedt scripts/ocr/model.npz
    reading, conf = reader.read(jpeg_bytes)   # -> ({"kwh":..,"w":..}, 0..1)

Wirft ValueError bei unlesbarem Display (z.B. Segmenttest, Leerbild).
"""

import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from extractor import Extractor, minus_ratio, prep_cell  # noqa: E402

# MODEL_FILE per Env uebersteuerbar: im Add-on zeigt es auf das
# git-gesyncte Modell im Feedback-Checkout (Hot-Reload bei Aenderung)
MODEL_FILE = Path(os.environ.get("MODEL_FILE",
                                 Path(__file__).with_name("model.npz")))

# Rueckfall auf Beispiele ANDERER Kaesten (fuer Ziffern, die im eigenen
# Kasten noch nie vorkamen) nur, wenn er um mehr als diese Kosinus-Marge
# besser passt als der beste Treffer im eigenen Kasten. Ohne Marge kippte
# am 23.09.2026 der zweite Kasten (immer eine 3) zur 0 aus dem ersten:
# 36302 -> 6302 auf 55 von 61 Bildern. Das entdoppelte Modell hat dort
# nur noch 1.4k statt 33k Dreien, die fremden Nullen stimmten sie nieder.
# 0,005: 60/61 auf diesen Bildern, Holdout ab 15.09. unveraendert; ab
# 01.09. -0,2 Punkte (neue Ziffern an neuer Stelle, die der Segment-
# Dekoder abfaengt, bis ein Retrain sie kennt).
FALLBACK_MARGIN = 0.005


class LocalReader:
    def __init__(self, model_file: Path | None = None, k: int = 3):
        model_file = model_file or MODEL_FILE
        m = np.load(model_file, allow_pickle=False)
        self.X = m["X"].astype(np.float32)  # ggf. float16-komprimiert gespeichert
        self.y = m["y"]
        self.slots = m["slots"] if "slots" in m.files else None
        self.k = k
        self.ex = Extractor()
        self.ex._anchor_ref = m["anchor"]
        self._slot_idx: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    def _kandidaten(self, slot: int) -> tuple[np.ndarray, np.ndarray]:
        """(eigener Kasten, Rueckfall) als Zeilen der kNN-Basis — haengt nur
        vom Modell ab, wird also einmal berechnet statt pro Zelle und Bild.
        Frueher kostete das Maskieren samt Kopie von X[mask] 85 % der
        Lesezeit (214 von 250 ms beim Modell vom 23.09.2026)."""
        if slot not in self._slot_idx:
            if self.slots is None:  # backwards-compatible with shipped model
                own = np.arange(len(self.y))
                fb = own[:0]
            else:
                present = set(self.y[self.slots == slot])
                own = np.flatnonzero(self.slots == slot)
                fb = np.flatnonzero((self.slots != slot)
                                    & ~np.isin(self.y, list(present)))
            self._slot_idx[slot] = (own, fb)
        return self._slot_idx[slot]

    def _predict(self, cells) -> tuple[list[str], float]:
        F = np.array([prep_cell(c) for c in cells], np.float32)
        F /= np.linalg.norm(F, axis=1, keepdims=True) + 1e-9
        S = self.X @ F.T  # alle Zellen in einem Matrixprodukt
        pred, confs = [], []
        for slot in range(len(F)):
            own, fb = self._kandidaten(slot)
            so, sf = S[own, slot], S[fb, slot]
            if len(fb) and (not len(own) or sf.max() > so.max() + FALLBACK_MARGIN):
                idx, scores = np.concatenate([own, fb]), np.concatenate([so, sf])
            else:
                idx, scores = own, so
            k = min(self.k, len(scores))
            row = np.argpartition(-scores, k - 1)[:k]
            labels, values = self.y[idx[row]], scores[row]
            vals, cnt = np.unique(labels, return_counts=True)
            p = str(vals[cnt.argmax()])
            if slot >= 6 and p in ("-", "_"):
                # W-Zeile: in den eindeutigen Geometrie-Zonen hat die
                # Geometrie Veto ueber kNN (Minus = Masse nur im Mittelband)
                r = minus_ratio(cells[slot])
                if r > 0.75:
                    p = "-"
                elif r < 0.3:
                    p = "_"
            pred.append(p)
            confs.append(float(values.mean()))
        return pred, float(min(confs))

    def read(self, img) -> tuple[dict, float]:
        if isinstance(img, (bytes, bytearray)):
            img = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_GRAYSCALE)
        kwh_cells, w_cells = self.ex.cells(img)
        labels, conf = self._predict(kwh_cells + w_cells)
        kwh_s = "".join(labels[:6])
        w_s = "".join(labels[6:])
        digits = (kwh_s + w_s).replace("_", "")
        # Echte Lesungen haben nie >5 Achten (35888 + 88 W); der Segmenttest
        # wird oft als Mix aus 8ern und 8-aehnlichen Ziffern (3/5/6/9/0) gelesen
        if len(digits) >= 8 and digits.count("8") >= 7:
            raise ValueError("LCD-Segmenttest (alles 8er)")
        if len(digits) >= 8 and set(digits) <= {"8", "0", "3", "5", "6", "9"} \
                and digits.count("8") >= 6:
            raise ValueError("LCD-Segmenttest (8er-dominiert)")
        if "_" in kwh_s or "-" in kwh_s:
            raise ValueError(f"kWh-Zeile unlesbar: {kwh_s!r}")
        w_clean = w_s.replace("_", "")
        if not w_clean or w_clean == "-" or "_" in w_s.strip("_"):
            raise ValueError(f"W-Zeile unlesbar: {w_s!r}")
        return {"kwh": int(kwh_s), "w": int(w_clean)}, conf
