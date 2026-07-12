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
from extractor import Extractor, prep_cell  # noqa: E402

# MODEL_FILE per Env uebersteuerbar: im Add-on zeigt es auf das
# git-gesyncte Modell im Feedback-Checkout (Hot-Reload bei Aenderung)
MODEL_FILE = Path(os.environ.get("MODEL_FILE",
                                 Path(__file__).with_name("model.npz")))


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

    def _predict(self, cells) -> tuple[list[str], float]:
        F = np.array([prep_cell(c) for c in cells], np.float32)
        F /= np.linalg.norm(F, axis=1, keepdims=True) + 1e-9
        pred, confs = [], []
        for slot, feature in enumerate(F):
            if self.slots is None:  # backwards-compatible with shipped model
                mask = np.ones(len(self.y), dtype=bool)
            else:
                present = set(self.y[self.slots == slot])
                mask = (self.slots == slot) | ~np.isin(self.y, list(present))
            scores = feature @ self.X[mask].T
            k = min(self.k, len(scores))
            row = np.argpartition(-scores, k - 1)[:k]
            labels, values = self.y[mask][row], scores[row]
            vals, cnt = np.unique(labels, return_counts=True)
            pred.append(str(vals[cnt.argmax()]))
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
        if len(digits) >= 8 and set(digits) <= {"8", "0"} and digits.count("8") >= 6:
            raise ValueError("LCD-Segmenttest (alles 8er)")
        if "_" in kwh_s or "-" in kwh_s:
            raise ValueError(f"kWh-Zeile unlesbar: {kwh_s!r}")
        w_clean = w_s.replace("_", "")
        if not w_clean or w_clean == "-" or "_" in w_s.strip("_"):
            raise ValueError(f"W-Zeile unlesbar: {w_s!r}")
        return {"kwh": int(kwh_s), "w": int(w_clean)}, conf
