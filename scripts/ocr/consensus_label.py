#!/usr/bin/env python3
"""Konsens-Labeler: erzeugt/aktualisiert training-data/auto/ aus ALLEN
Disagreements. Regeln (streng, weil Gemini-W-Werte fehleranfaellig sind):

- kWh: Modell (conf>=0.90) und Gemini muessen uebereinstimmen; nur einer
  plausibel -> der; beidseitig sicher aber verschieden -> KEIN Label.
- W: nur bei Doppel-Konsens Modell==Gemini, sonst kWh-only.
- Ausserhalb der kWh-Aera oder 8er-Werte -> ungueltig.
- Bestehende auto-Labels werden korrigiert/entfernt, wenn der Konsens
  anders ausfaellt.

Aufruf: consensus_label.py [training-data] [era_lo] [era_hi]
"""
import json
import shutil
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_reader import LocalReader  # noqa: E402

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "training-data")
ERA = (int(sys.argv[2]) if len(sys.argv) > 2 else 35680,
       int(sys.argv[3]) if len(sys.argv) > 3 else 35950)


def era_ok(k):
    return isinstance(k, int) and ERA[0] <= k <= ERA[1]


def main():
    reader = LocalReader()
    auto = ROOT / "auto"
    auto.mkdir(exist_ok=True)
    stats = dict(full=0, kwh_only=0, skip=0, removed=0)
    for jf in sorted((ROOT / "disagreements").glob("*.json")):
        img_f = jf.with_suffix(".jpg")
        if not img_f.exists():
            continue
        try:
            d = json.loads(jf.read_text())
        except ValueError:
            continue
        gem = d.get("gemini") or {}
        g_k = gem.get("kwh") if era_ok(gem.get("kwh")) else None
        g_w = gem.get("w") if (isinstance(gem.get("w"), int)
                               and abs(gem["w"]) <= 20000
                               and abs(gem["w"]) not in (888, 8888, 88888)) \
            else None
        m_k = m_w = None
        m_conf = 0.0
        img = cv2.imread(str(img_f), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            try:
                got, m_conf = reader.read(img)
                if m_conf >= 0.90 and era_ok(got["kwh"]):
                    m_k, m_w = got["kwh"], got["w"]
            except ValueError:
                pass  # Segmenttest/unlesbar
        if g_k is not None and m_k is not None:
            kwh = g_k if g_k == m_k else None  # beidseitig sicher+ungleich: raus
        elif g_k is not None:
            kwh = g_k
        elif m_k is not None and m_conf >= 0.95:
            kwh = m_k
        else:
            kwh = None
        target = auto / jf.name
        if kwh is None:
            if target.exists():
                target.unlink()
                tj = target.with_suffix(".jpg")
                if tj.exists():
                    tj.unlink()
                stats["removed"] += 1
            else:
                stats["skip"] += 1
            continue
        label = {"kwh": kwh}
        if g_w is not None and m_w is not None and g_w == m_w:
            label["w"] = g_w
            stats["full"] += 1
        else:
            stats["kwh_only"] += 1
        old = None
        if target.exists():
            try:
                old = json.loads(target.read_text())
            except ValueError:
                pass
        if old != label:
            target.write_text(json.dumps(label))
        if not target.with_suffix(".jpg").exists():
            shutil.copy2(img_f, target.with_suffix(".jpg"))
    print("Konsens-Labels:", stats)


if __name__ == "__main__":
    main()
