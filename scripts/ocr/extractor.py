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
    "kwh": {"x0": 489, "y0": 289, "h": 64, "pitch": 36.4, "w": 35, "n": 6},
    "watt": {"xend": 707, "y0": 353, "h": 65, "pitch": 36.4, "w": 35, "n": 5},
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

    def _refine_pose(self, gray, dx, dy):
        """Kamm-Korrelation der 6 kWh-Glyphen -> feinjustiertes (dx, dy,
        pitch). Der Template-Anker (search 25px) verrutscht oder rastet auf
        Nachbarstrukturen ein — die 6 immer beleuchteten kWh-Ziffern bilden
        dagegen einen periodischen Tinten-Kamm, dessen Phase ein robustes,
        textur-basiertes Alignment liefert (dx +-45, dy +-12, pitch +-1px;
        Pitch mitfitten, weil ein Fehler von 0.4px bis Slot 5 auf eine halbe
        Strichbreite akkumuliert). Auf dem zeitlichen Holdout hebt das die
        kWh-Zeilen-Genauigkeit von 78% auf 91%."""
        k = self.cfg["kwh"]
        pitch, W = k["pitch"], int(k["w"])
        gw, SX, SY = 25, 45, 12
        x0 = int(k["x0"] + dx) - SX - 8
        x1 = int(k["x0"] + 5 * pitch + W + dx) + SX + 8
        y0 = int(k["y0"] + dy) - SY
        y1 = int(k["y0"] + k["h"] + dy) + SY
        if x0 < 0 or y0 < 0 or x1 > gray.shape[1] or y1 > gray.shape[0]:
            return dx, dy, pitch
        strip = gray[y0:y1, x0:x1].astype(np.float32)
        bg = cv2.morphologyEx(strip, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25)))
        dark = np.clip(bg - strip, 0, None)
        colp = dark.sum(axis=0)
        best = (-1e18, 0, pitch)
        for p10 in range(-10, 11, 2):
            pp = pitch + p10 / 10
            comb = np.zeros(int(5 * pp) + W + 16, np.float32)
            for i in range(6):
                g0 = int(i * pp) + 8 + (W - gw) // 2
                comb[g0:g0 + gw] = 1.0
            comb -= comb.mean()
            for sdx in range(-SX, SX + 1):
                lo = SX + sdx
                if lo < 0 or lo + len(comb) > len(colp):
                    continue
                sc = float((comb * colp[lo:lo + len(comb)]).sum())
                if sc > best[0]:
                    best = (sc, sdx, float(pp))
        _, sdx, pfit = best
        cols = np.zeros(dark.shape[1], bool)
        for i in range(6):
            g0 = SX + sdx + 8 + int(i * pfit) + (W - gw) // 2
            cols[g0:g0 + gw] = True
        rowp = dark[:, cols].sum(axis=1)
        box = np.zeros(len(rowp), np.float32)
        box[SY:SY + k["h"]] = 1.0
        box -= box.mean()
        ts = list(range(-SY + 1, SY))
        sdy = ts[int(np.argmax([float((np.roll(box, t) * rowp).sum()) for t in ts]))]
        return dx + sdx, dy + sdy, pfit

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
        dx, dy = self._drift(gray)          # grober Anker-Seed
        dx, dy, pitch = self._refine_pose(gray, dx, dy)  # Kamm-Feinjustage
        k, w = self.cfg["kwh"], self.cfg["watt"]
        kwh = [
            self._cell(gray, k["x0"] + i * pitch + dx, k["y0"] + dy, k["w"], k["h"])
            for i in range(k["n"])
        ]
        watt = [
            self._cell(
                gray,
                w["xend"] - (w["n"] - i) * pitch + dx,
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


def shifted_variants(vec: np.ndarray, cell_w: int = 20,
                     shifts=(-1, 1)) -> list[np.ndarray]:
    """Horizontal verschobene Kopien eines Zellen-Vektors (nur fuers Training):
    dieselbe Ziffer sitzt je nach Box-Position leicht versetzt — mit den
    Varianten in der kNN-Basis generalisiert sie ueber alle Positionen."""
    img = vec.reshape(-1, cell_w)
    out = []
    for s in shifts:
        v = np.roll(img, s, axis=1)
        if s > 0:
            v[:, :s] = 0.0
        else:
            v[:, s:] = 0.0
        out.append(v.ravel())
    return out


def minus_ratio(cell: np.ndarray) -> float:
    """Dunkle Masse Mittelband vs. aussen. Minus-Zellen: >0.75 (p5=0.57),
    echte Blanks: <0.3 (p95=0.31). Dazwischen: unsicher."""
    dark = np.clip(-cell, 0, None)
    mid = float(dark[13:19].sum())
    outer = float(dark[:11].sum() + dark[21:].sum())
    return mid / (outer + 1.0)


def labels_for(reading: dict) -> tuple[list[str], list[str]] | None:
    """Zellen-Labels aus einer Gesamt-Lesung ableiten (None = nicht abbildbar)."""
    kwh = reading["kwh"]
    if not (0 <= kwh <= 999999):
        return None
    kwh_lbl = list(f"{kwh:06d}")
    # Manually verified recovery samples may label only the kWh row.  This is
    # safer than manufacturing a watt label when repairing a kWh OCR error.
    if "w" not in reading:
        return kwh_lbl, []
    w_str = str(reading["w"])
    if len(w_str) > 5:
        return None
    w_lbl = ["_"] * (5 - len(w_str)) + list(w_str)
    return kwh_lbl, w_lbl
