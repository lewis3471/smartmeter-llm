"""Digit-Extraktion vom EasyMeter-LCD (feste Kameraposition).

Grid kalibriert auf 1024x768-Frames der ESP32-Cam. Ein Anker-Patch
(kWh-Label) kompensiert kleine Kamera-Verschiebungen per Template-Matching.
"""

import json
from pathlib import Path

import cv2
import numpy as np

CONFIG_FILE = Path(__file__).with_name("config.json")

DEFAULT_CONFIG = {
    # Ziffernraster (Pixel im Originalbild)
    "kwh": {"x0": 489, "y0": 292, "h": 64, "pitch": 36.4, "w": 35, "n": 6},
    "watt": {"xend": 707, "y0": 350, "h": 68, "pitch": 36.4, "w": 35, "n": 5},
    # Anker fuer Drift-Korrektur: Region um das "kWh"-Label
    "anchor": {"x": 700, "y": 295, "w": 90, "h": 65, "search": 25},
    # Normalisierte Zellgroesse fuer den Klassifikator
    "cell": {"w": 20, "h": 32},
}

CLASSES = list("0123456789") + ["-", "_"]  # "_" = leer


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return DEFAULT_CONFIG


class Extractor:
    def __init__(self, config: dict | None = None):
        self.cfg = config or load_config()
        self._anchor_ref: np.ndarray | None = None

    def _to_gray(self, img) -> np.ndarray:
        if isinstance(img, (bytes, bytearray)):
            img = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_GRAYSCALE)
        elif img.ndim == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return img

    def set_anchor_reference(self, gray: np.ndarray):
        a = self.cfg["anchor"]
        self._anchor_ref = gray[a["y"]:a["y"] + a["h"], a["x"]:a["x"] + a["w"]].copy()

    def _drift(self, gray: np.ndarray) -> tuple[int, int]:
        """(dx, dy) der aktuellen Aufnahme relativ zur Referenz."""
        if self._anchor_ref is None:
            return 0, 0
        a = self.cfg["anchor"]
        s = a["search"]
        y0, y1 = max(0, a["y"] - s), a["y"] + a["h"] + s
        x0, x1 = max(0, a["x"] - s), a["x"] + a["w"] + s
        region = gray[y0:y1, x0:x1]
        res = cv2.matchTemplate(region, self._anchor_ref, cv2.TM_CCOEFF_NORMED)
        _, conf, _, loc = cv2.minMaxLoc(res)
        if conf < 0.4:  # Anker nicht gefunden (dunkel/verdeckt)
            return 0, 0
        return x0 + loc[0] - a["x"], y0 + loc[1] - a["y"]

    def _cell(self, gray, x, y, w, h) -> np.ndarray:
        c = self.cfg["cell"]
        patch = gray[int(y):int(y + h), int(x):int(x + w)]
        if patch.size == 0:
            patch = np.zeros((h, w), np.uint8)
        patch = cv2.resize(patch, (c["w"], c["h"]))
        # Kontrast normalisieren (LED-Helligkeit schwankt)
        patch = patch.astype(np.float32)
        patch -= patch.mean()
        std = patch.std()
        return patch / std if std > 1e-3 else patch

    def cells(self, img) -> tuple[list[np.ndarray], list[np.ndarray]]:
        """-> (6 kWh-Zellen, 5 W-Zellen von links nach rechts)"""
        gray = self._to_gray(img)
        dx, dy = self._drift(gray)
        k, w = self.cfg["kwh"], self.cfg["watt"]
        kwh = [
            self._cell(gray, k["x0"] + i * k["pitch"] + dx, k["y0"] + dy, k["w"], k["h"])
            for i in range(k["n"])
        ]
        watt = [
            self._cell(
                gray,
                w["xend"] - (w["n"] - i) * w["pitch"] + dx,
                w["y0"] + dy,
                w["w"],
                w["h"],
            )
            for i in range(w["n"])
        ]
        return kwh, watt


def prep_cell(cell: np.ndarray) -> np.ndarray:
    """Zelle -> normalisierter Feature-Vektor (Helligkeit/Kontrast-invariant)."""
    rng = float(cell.max() - cell.min())
    c = (cell - cell.min()) / (rng + 1e-9)
    c = c.astype(np.float32)
    c -= c.mean()
    c /= c.std() + 1e-9
    return c.ravel()


def labels_for(reading: dict) -> tuple[list[str], list[str]] | None:
    """Zellen-Labels aus einer Gesamt-Lesung ableiten (None = nicht abbildbar)."""
    kwh, w = reading["kwh"], reading["w"]
    if not (0 <= kwh <= 999999):
        return None
    kwh_lbl = list(f"{kwh:06d}")
    w_str = str(w)
    if len(w_str) > 5:
        return None
    w_lbl = ["_"] * (5 - len(w_str)) + list(w_str)
    return kwh_lbl, w_lbl
