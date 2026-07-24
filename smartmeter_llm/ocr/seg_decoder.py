#!/usr/bin/env python3
"""Deterministischer 7-Segment-Dekoder — Prototyp fuer das EasyMeter-LCD.

Idee: Statt kNN werden die 7 Segmente jeder Digit-Zelle geometrisch
abgetastet (Regionen relativ zur Glyph-Box, hell/dunkel relativ zur
Zeilenstatistik) und das Segmentmuster per Lookup in eine Ziffer
uebersetzt. Eine '3' ist damit an jeder Slot-Position eine '3' —
das Rollover-Problem (nie an dieser Position gesehene Ziffer) entfaellt
strukturell.

Pipeline:
  0. Pose-Refinement pro Bild: Kamm-Korrelation der 6 kWh-Glyphen
     (dx +-45px, dy +-11px, Pitch +-1px) — der Anker-Match des Extractors
     versagt bei groesseren Kamerabewegungen oder rastet falsch ein.
  1. Pro Zelle (native 35x64px + 6px Rand): Hintergrund pro PATCH-Zeile
     flatten (Beleuchtung hat horizontalen UND vertikalen Gradienten),
     globale Tinten-Skala aus der kWh-Zeile -> darkness in [0,1].
  2. Deslant (Italic-Schrift, Scherung 0.15, aus Daten kalibriert).
  3. Alignment-Suche: Glyph-Fenster (25px, Zelle x=6..31, Ziffern ragen
     rechts ueber die 35px-Box hinaus) in +-4px um die nominale Position.
  4. 7 Segment-Aktivierungen: Top-60%-Mittel enger Regionen auf den
     Stroke-Mittellinien (BRUCHTEILE der Glyph-Box, nicht hardcodiert),
     Mikro-Alignment +-2px senkrecht zum Strich, Normalisierung auf das
     Tintenniveau der Zelle (p93 im Glyph-Kasten); Blank-Erkennung mit
     nachbar-relativem Boden (Ghost-Segmente!).
  5. Logistisches Matching gegen die 12 Muster (0-9, '-', leer) mit
     per-Segment-Schwellen (E/F blass, G ghostet stark);
     Konfidenz = Log-Likelihood-Abstand zum zweitbesten Muster.

Ergebnis auf training-data/auto (373 Bilder, 3308 Zellen):
  92.2% Zellen-Accuracy; zeitlicher Holdout 95.7% vs. kNN 71.9%;
  Rollover-Simulation (Ziffer@Slot nie gesehen): SEG 92.8% vs. kNN 61.9%.

Nutzung:
    sys.path fuer scripts/ocr setzen, dann
    r = SegReader(anchor_ref)          # anchor aus model.npz oder Referenzbild
    labels, confs, debug = r.read_cells(gray_1024x768)

Eval:  .venv/bin/python /tmp/seg_decoder.py [training-data/auto]
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extractor import Extractor  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

SEGMENTS = "ABCDEFG"
PATTERNS = {
    "0": "ABCDEF", "1": "BC",     "2": "ABDEG",  "3": "ABCDG",
    "4": "BCFG",   "5": "ACDFG",  "6": "ACDEFG", "7": "ABC",
    "8": "ABCDEFG", "9": "ABCDFG", "-": "G",      "_": "",
}
PAT_VECS = {c: np.array([s in p for s in SEGMENTS], np.float32)
            for c, p in PATTERNS.items()}

# Segment-Regionen als Bruchteile der Glyph-Box (x0,x1,y0,y1) —
# eng auf den Stroke-Mittellinien (Stroke-Zentren aus Spaltenprofilen:
# Vertikale bei x~0.14 und ~0.84, Horizontale bei y~0.07/0.50/0.94)
SEG_REGIONS = {
    "A": (0.32, 0.68, 0.00, 0.13),
    "B": (0.74, 0.98, 0.16, 0.38),
    "C": (0.74, 0.98, 0.62, 0.84),
    "D": (0.32, 0.68, 0.87, 1.00),
    "E": (0.02, 0.26, 0.62, 0.84),
    "F": (0.02, 0.26, 0.16, 0.38),
    "G": (0.32, 0.68, 0.44, 0.56),
}


class SegDecoder:
    """Dekodiert eine darkness-Map einer Zelle (Patch mit Rand)."""

    def __init__(self, cell_h=64, slant=0.15, marg=6,
                 glyph_x0=6.0, glyph_w=25.0, glyph_y0=6.0, glyph_y1=59.0,
                 x_search=4, t_on=None, sharp=0.10):
        # Schwelle pro Segment: linke Segmente (E,F) sind blickwinkel-
        # bedingt auch beleuchtet blass, das Mittelsegment (G) ghostet
        # am staerksten
        if t_on is None:
            t_on = np.array([0.72, 0.72, 0.72, 0.72, 0.42, 0.42, 0.78],
                            np.float32)
        self.h = cell_h
        self.slant = slant
        self.marg = marg
        self.gx0 = glyph_x0 + marg     # in Patch-Koordinaten
        self.gw = glyph_w
        self.gy0, self.gy1 = glyph_y0, glyph_y1
        self.x_search = x_search
        self.t_on, self.sharp = t_on, sharp
        self.M = np.float32([[1, slant, -slant * cell_h / 2], [0, 1, 0]])
        gh = glyph_y1 - glyph_y0
        # Regionen einmalig in Pixel-Offsets relativ zur Fensterposition
        self.regions = {}
        for s, (fx0, fx1, fy0, fy1) in SEG_REGIONS.items():
            self.regions[s] = (
                int(round(fx0 * glyph_w)), int(round(fx1 * glyph_w)),
                int(round(glyph_y0 + fy0 * gh)), int(round(glyph_y0 + fy1 * gh)),
            )

    def deslant(self, dark_patch):
        return cv2.warpAffine(dark_patch, self.M,
                              (dark_patch.shape[1], dark_patch.shape[0]))

    @staticmethod
    def _robust_mean(vals):
        """Mittel der dunkelsten 60% — Regionen sind eng auf den
        Mittellinien, ein zu kleiner Anteil wuerde Nachbar-Bleed belohnen."""
        v = np.sort(vals.ravel())
        n = max(1, int(len(v) * 0.6))
        return float(v[-n:].mean())

    # Mikro-Alignment pro Segment: senkrecht zum Strich +-2px schieben und
    # das Maximum nehmen — beleuchtete Segmente erreichen so zuverlaessig
    # das Zell-Tintenniveau, Region-Restfehler kosten keine Marge mehr.
    _PERP = {"A": "y", "G": "y", "D": "y", "B": "x", "C": "x", "E": "x", "F": "x"}

    def activations(self, ds, x_off):
        a = np.empty(7, np.float32)
        x_base = self.gx0 + x_off
        for i, s in enumerate(SEGMENTS):
            rx0, rx1, ry0, ry1 = self.regions[s]
            best = 0.0
            for m in (-2, -1, 0, 1, 2):
                mx = m if self._PERP[s] == "x" else 0
                my = m if self._PERP[s] == "y" else 0
                x0 = int(round(x_base + rx0 + mx))
                x1 = int(round(x_base + rx1 + mx))
                reg = ds[max(0, ry0 + my):ry1 + my, max(0, x0):x1]
                if reg.size:
                    best = max(best, self._robust_mean(reg))
            a[i] = best
        return a

    def _loglik(self, acts):
        p = 1.0 / (1.0 + np.exp(-(acts - self.t_on) / self.sharp))
        p = np.clip(p, 1e-4, 1 - 1e-4)
        out = {}
        for c, b in PAT_VECS.items():
            out[c] = float((b * np.log(p) + (1 - b) * np.log(1 - p)).sum())
        return out

    BLANK_FLOOR = 0.20   # absoluter p93-Tinten-Boden

    def _ink_level(self, ds, x_off):
        x0 = int(round(self.gx0 + x_off))
        box = ds[int(self.gy0):int(self.gy1), x0:x0 + int(self.gw)]
        return float(np.percentile(box, 93)) if box.size else 0.0

    def ink(self, dark_patch):
        return self._ink_level(self.deslant(dark_patch), 0)

    def decode(self, dark_patch, blank_floor=None):
        """-> (char, conf, best_acts, best_off).
        Aktivierungen werden pro Zelle auf das eigene Tintenniveau
        normalisiert (Beleuchtung variiert ueber die Slots); Zellen ohne
        Tinte sind direkt leer. blank_floor kann vom Aufrufer relativ zu
        den Nachbarzellen angehoben werden (Ghost-Segmente unbeleuchteter
        Zellen liegen sonst ueber dem absoluten Boden).
        conf = Log-Likelihood-Vorsprung des Siegers vor dem Zweiten."""
        ds = self.deslant(dark_patch)
        floor = self.BLANK_FLOOR if blank_floor is None else blank_floor
        ink = self._ink_level(ds, 0)
        if ink < floor:
            margin = (floor - ink) / floor * 10
            return "_", margin, np.zeros(7, np.float32), 0
        best = {}   # char -> (loglik, acts, off)
        for off in range(-self.x_search, self.x_search + 1):
            acts = self.activations(ds, off)
            ink_o = max(self._ink_level(ds, off), 1e-3)
            acts = np.clip(acts / ink_o, 0, 1.2)
            for c, ll in self._loglik(acts).items():
                if c not in best or ll > best[c][0]:
                    best[c] = (ll, acts, off)
        order = sorted(best.items(), key=lambda kv: -kv[1][0])
        (c1, (l1, a1, o1)), (c2, (l2, _, _)) = order[0], order[1]
        return c1, l1 - l2, a1, o1


class SegReader:
    """Volles Bild -> 11 Zell-Labels (6 kWh + 5 W) via Segment-Dekoder."""

    MARG = 6

    def __init__(self, anchor_ref=None, config=None):
        self.ex = Extractor(config)
        if anchor_ref is not None:
            self.ex._anchor_ref = anchor_ref
        k = self.ex.cfg["kwh"]
        self.dec = SegDecoder(cell_h=int(k["h"]), marg=self.MARG)

    @staticmethod
    def _diff_map(patch):
        """Lokales Background-Flattening: bg pro PATCH-Zeile (p85).
        Die Beleuchtung variiert horizontal ueber das Display (rechte
        Slots liegen im Schatten) — ein zeilenweiter bg ueber den ganzen
        Streifen liest dort alles als Tinte."""
        bg = np.percentile(patch, 85, axis=1, keepdims=True)
        return np.clip(bg - patch, 0, None)

    def _raw_patches(self, gray, dx, dy, pitch=None):
        k, w = self.ex.cfg["kwh"], self.ex.cfg["watt"]
        M = self.MARG
        pk = pitch or k["pitch"]
        pw = pitch or w["pitch"]
        pats = []
        y0, y1 = int(k["y0"] + dy), int(k["y0"] + k["h"] + dy)
        for i in range(k["n"]):
            x = int(k["x0"] + i * pk + dx) - M
            pats.append(gray[y0:y1, x:x + int(k["w"]) + 2 * M].astype(np.float32))
        y0, y1 = int(w["y0"] + dy), int(w["y0"] + w["h"] + dy)
        for i in range(w["n"]):
            x = int(w["xend"] - (w["n"] - i) * pw + dx) - M
            pats.append(gray[y0:y1, x:x + int(w["w"]) + 2 * M].astype(np.float32))
        return pats

    def _refine_pose(self, gray, dx, dy):
        """Anker-Drift feinjustieren: Kamm-Korrelation der kWh-Zeile.
        Der Anker-Match versagt bei groesseren Kamera-Bewegungen (Suchradius
        25px) oder rastet auf Nachbarstrukturen ein. Die 6 immer lit-en
        kWh-Glyphen bilden einen Kamm mit Pitch 36.4 — dessen Phase ist
        ein deterministisches, textur-basiertes Alignment-Signal."""
        k = self.ex.cfg["kwh"]
        d = self.dec
        pitch, W = k["pitch"], int(k["w"])
        SX, SY = 45, 12
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
        # --- dx + Pitch: Spaltenprofil vs. Glyph-Kamm (mittelwertfrei).
        # Pitch mitfitten: Perspektive/Zoom aendert sich ueber die
        # Korpus-Lebensdauer, ein Fehler von 0.4px akkumuliert bis Slot 5
        # auf eine halbe Strichbreite. ---
        colp = dark.sum(axis=0)
        best = (-1e18, 0, pitch)
        for p in np.arange(pitch - 1.0, pitch + 1.01, 0.2):
            comb = np.zeros(int(5 * p) + W + 16, np.float32)
            for i in range(6):
                g0 = int(i * p + d.gx0 - self.MARG) + 8
                comb[g0:g0 + int(d.gw)] = 1.0
            comb -= comb.mean()
            for s in range(-SX, SX + 1):
                if SX + s < 0 or SX + s + len(comb) > len(colp):
                    continue
                sc = float((comb * colp[SX + s:SX + s + len(comb)]).sum())
                if sc > best[0]:
                    best = (sc, s, float(p))
        _, sdx, pfit = best
        # --- dy: Zeilenprofil (nur Glyph-Spalten) vs. Glyph-Hoehenfenster ---
        cols = np.zeros(dark.shape[1], bool)
        for i in range(6):
            g0 = SX + sdx + 8 + int(i * pfit + d.gx0 - self.MARG)
            cols[g0:g0 + int(d.gw)] = True
        rowp = dark[:, cols].sum(axis=1)
        box = np.zeros(len(rowp), np.float32)
        box[int(d.gy0) + SY:int(d.gy1) + SY] = 1.0
        box -= box.mean()
        tshift = range(-SY + 1, SY)
        scoresy = [float((np.roll(box, t) * rowp).sum()) for t in tshift]
        sdy = list(tshift)[int(np.argmax(scoresy))]
        return dx + sdx, dy + sdy, pfit

    def score_candidates(self, gray, candidates):
        """Binaerer (bzw. n-facher) Hypothesentest auf der kWh-Zeile.

        Statt offen zu lesen ("welche der 10 Ziffern steht da?") wird nur
        gefragt: "welcher der uebergebenen Zaehlerstaende passt besser?".
        Das ist der Job des Schiedsrichters — er kennt die Kandidaten
        (aktueller Stand und Stand+1). Offenes Lesen ist an der rechten
        Schattenzone nicht zuverlaessig zu bekommen (Slot 5 braucht fuer
        99% Praezision conf>=3.9, was nur 11% der Frames erreichen); die
        Unterscheidung ZWEIER bekannter Muster dagegen schon, weil sie nur
        die tatsaechlich unterschiedlichen Zellen bewerten muss.

        -> (bester Kandidat, Vorsprung in Log-Likelihood ueber alle
        unterscheidenden Zellen) oder (None, 0.0) wenn nicht auswertbar.
        """
        cands = [f"{c:06d}" for c in candidates]
        if len({len(c) for c in cands}) != 1 or any(len(c) != 6 for c in cands):
            return None, 0.0
        diff = [i for i in range(6) if len({c[i] for c in cands}) > 1]
        if not diff:
            return candidates[0], float("inf")
        dx, dy = self.ex._drift(gray)
        dx, dy, pfit = self._refine_pose(gray, dx, dy)
        pats = self._raw_patches(gray, dx, dy, pitch=pfit)[:6]
        diffs = [self._diff_map(p) for p in pats]
        scale = max(float(np.percentile(np.concatenate(
            [d.ravel() for d in diffs]), 98)), 8.0)
        totals = [0.0] * len(cands)
        for i in diff:
            d = np.clip(diffs[i] / scale, 0, 1)
            ds = self.dec.deslant(d)
            best = {}
            for off in range(-self.dec.x_search, self.dec.x_search + 1):
                acts = self.dec.activations(ds, off)
                ink_o = max(self.dec._ink_level(ds, off), 1e-3)
                acts = np.clip(acts / ink_o, 0, 1.2)
                for c, ll in self.dec._loglik(acts).items():
                    if c not in best or ll > best[c]:
                        best[c] = ll
            for j, cand in enumerate(cands):
                totals[j] += best.get(cand[i], -1e9)
        order = sorted(range(len(cands)), key=lambda j: -totals[j])
        margin = totals[order[0]] - totals[order[1]]
        return candidates[order[0]], float(margin)

    def read_cells(self, gray):
        dx, dy = self.ex._drift(gray)
        dx, dy, pfit = self._refine_pose(gray, dx, dy)
        pats = self._raw_patches(gray, dx, dy, pitch=pfit)
        diffs = [self._diff_map(p) for p in pats]
        # globale Tinten-Skala aus der kWh-Zeile (hat immer 6 Ziffern);
        # Blank-Zellen der W-Zeile rauschen so nicht hoch
        scale = max(float(np.percentile(np.concatenate(
            [d.ravel() for d in diffs[:6]]), 98)), 8.0)
        dark = [np.clip(d / scale, 0, 1) for d in diffs]
        inks = [self.dec.ink(d) for d in dark]
        out_lbl, out_conf, dbg = [], [], []
        for slot, d in enumerate(dark):
            # Blank-Boden relativ zur hellsten Nachbarzelle derselben
            # Zeile: Ghost-Segmente (~25% Tinte) vs. echte, ggf. im
            # Schatten liegende Ziffern (Schatten ist ein sanfter
            # Gradient — der Nachbar ist dann ebenfalls dunkel)
            lo = 6 if slot >= 6 else 0
            hi = 11 if slot >= 6 else 6
            neigh = [inks[j] for j in (slot - 1, slot + 1) if lo <= j < hi]
            ref = max([inks[slot]] + neigh)
            floor = max(self.dec.BLANK_FLOOR, 0.38 * ref)
            c, conf, acts, off = self.dec.decode(d, blank_floor=floor)
            out_lbl.append(c); out_conf.append(conf)
            dbg.append((acts, off))
        return out_lbl, out_conf, dbg


# ---------------------------------------------------------------- Eval ----

def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "training-data" / "auto"
    m = np.load(REPO / "scripts" / "ocr" / "model.npz")
    reader = SegReader(anchor_ref=m["anchor"])

    files = sorted(root.glob("*.json"))
    from collections import Counter, defaultdict
    tot = Counter(); ok = Counter()
    slot_tot = Counter(); slot_ok = Counter()
    confusions = Counter()
    conf_correct, conf_wrong = [], []
    fails = []

    for jf in files:
        img_p = jf.with_suffix(".jpg")
        if not img_p.exists():
            continue
        lab = json.loads(jf.read_text())
        gray = cv2.imread(str(img_p), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            continue
        truth = list(f"{lab['kwh']:06d}")
        n_cells = 6
        if "w" in lab:
            ws = str(lab["w"])
            truth += ["_"] * (5 - len(ws)) + list(ws)
            n_cells = 11
        pred, confs, dbg = reader.read_cells(gray)
        for slot in range(n_cells):
            t, p = truth[slot], pred[slot]
            tot[t] += 1; slot_tot[slot] += 1
            if p == t:
                ok[t] += 1; slot_ok[slot] += 1
                conf_correct.append(confs[slot])
            else:
                confusions[(t, p)] += 1
                conf_wrong.append(confs[slot])
                fails.append((img_p.name, slot, t, p, confs[slot], dbg[slot]))

    n_tot = sum(tot.values()); n_ok = sum(ok.values())
    print(f"Zellen gesamt: {n_ok}/{n_tot} = {n_ok/n_tot:.4f}")
    print("\nPer Ziffer:")
    for c in sorted(tot):
        print(f"  {c!r}: {ok[c]}/{tot[c]} = {ok[c]/tot[c]:.4f}")
    print("\nPer Slot (0-5 kWh, 6-10 W):")
    for s in sorted(slot_tot):
        print(f"  {s:2d}: {slot_ok[s]}/{slot_tot[s]} = {slot_ok[s]/slot_tot[s]:.4f}")
    print("\nTop-Konfusionen (wahr -> vorhergesagt):")
    for (t, p), n in confusions.most_common(15):
        print(f"  {t!r} -> {p!r}: {n}")
    cc, cw = np.array(conf_correct), np.array(conf_wrong)
    print(f"\nKonfidenz korrekt : p5={np.percentile(cc,5):.2f} "
          f"med={np.median(cc):.2f}")
    if len(cw):
        print(f"Konfidenz falsch  : med={np.median(cw):.2f} "
              f"p95={np.percentile(cw,95):.2f}")
        for th in (2, 4, 6, 8, 10):
            fw = (cw < th).mean()
            fc = (cc < th).mean()
            print(f"  Schwelle {th:2d}: faengt {fw:.0%} der Fehler, "
                  f"opfert {fc:.1%} der korrekten")
    print(f"\nSchlimmste Fails (max 15):")
    for name, slot, t, p, cf, (acts, off) in sorted(
            fails, key=lambda f: -f[4])[:15]:
        a = " ".join(f"{s}{v:.2f}" for s, v in zip(SEGMENTS, acts))
        print(f"  {name} slot{slot} {t!r}->{p!r} conf={cf:.1f} off={off} {a}")


if __name__ == "__main__":
    main()
