#!/usr/bin/env python3
"""Konsens-Labeler: erzeugt/aktualisiert training-data/auto/ aus ALLEN
Disagreements. Regeln (streng, weil Gemini-W-Werte fehleranfaellig sind):

- kWh: Modell (conf>=0.90) und Gemini muessen uebereinstimmen; nur einer
  plausibel -> der; beidseitig sicher aber verschieden -> KEIN Label.
- W: nur bei Doppel-Konsens Modell==Gemini, sonst kWh-only.
- Ausserhalb der kWh-Aera oder 8er-Werte -> ungueltig. Die Aera wird
  DYNAMISCH aus den juengsten vorhandenen Labels abgeleitet (Median des
  letzten Tages mit >=3 Labels, Fenster -40/+80) — ein hartkodiertes
  Fenster braeche exakt beim Zaehler-Rollover.
- Bestehende auto-Labels, denen der Konsens widerspricht, werden nach
  training-data/quarantine/ VERSCHOBEN (nie geloescht): ein degradiertes
  Modell kann so kuratierte Labels nicht unwiederbringlich erodieren.

Aufruf: consensus_label.py [training-data] [era_lo] [era_hi]
(era_lo/era_hi nur als manueller Override)
"""
import json
import shutil
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_reader import LocalReader  # noqa: E402

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "training-data")


def day_medians(root: Path) -> dict[str, int]:
    """Tages-Mediane der kWh aus auto/ + Gemini-Disagreements — Basis fuer
    ein PRO-TAG-Aera-Fenster. Ein globales Fenster wuerde historische
    Labels aelterer Zaehlerstaende faelschlich verwerfen."""
    by_day: dict[str, list[int]] = {}
    for jf in list(root.glob("auto/*.json")):
        try:
            k = json.loads(jf.read_text()).get("kwh")
        except (OSError, ValueError):
            continue
        if isinstance(k, int) and 10_000 <= k < 1_000_000 and k != 888888:
            by_day.setdefault(jf.name[:8], []).append(k)
    for jf in root.glob("disagreements/*.json"):
        try:
            k = (json.loads(jf.read_text()).get("gemini") or {}).get("kwh")
        except (OSError, ValueError):
            continue
        if isinstance(k, int) and 10_000 <= k < 1_000_000 and k != 888888:
            by_day.setdefault(jf.name[:8], []).append(k)
    return {d: sorted(v)[len(v) // 2] for d, v in by_day.items() if len(v) >= 3}


_OVERRIDE = ((int(sys.argv[2]), int(sys.argv[3]))
             if len(sys.argv) > 3 else None)
_DAY_MED = None if _OVERRIDE else day_medians(ROOT)
if not _OVERRIDE and not _DAY_MED:
    raise SystemExit("Keine Labels fuer Aera-Ableitung — era_lo/era_hi "
                     "explizit uebergeben")


def era_for(day: str) -> tuple[int, int]:
    """Fenster fuer das Datum des Samples: Median des Tages (oder des
    naechstliegenden Tages mit Daten) -40/+80."""
    if _OVERRIDE:
        return _OVERRIDE
    if day in _DAY_MED:
        base = _DAY_MED[day]
    else:
        nearest = min(_DAY_MED, key=lambda d: abs(int(d) - int(day)))
        base = _DAY_MED[nearest]
    return base - 40, base + 80


def era_ok(k, day: str):
    lo, hi = era_for(day)
    return isinstance(k, int) and lo <= k <= hi


def main():
    if _DAY_MED:
        latest = max(_DAY_MED)
        print(f"kWh-Aera: pro Tag dynamisch, aktuell ({latest}): "
              f"{era_for(latest)}")
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
        day = jf.name[:8]
        gem = d.get("gemini") or {}
        g_k = gem.get("kwh") if era_ok(gem.get("kwh"), day) else None
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
                if m_conf >= 0.90 and era_ok(got["kwh"], day):
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
                quar = ROOT / "quarantine"
                quar.mkdir(exist_ok=True)
                target.rename(quar / target.name)
                tj = target.with_suffix(".jpg")
                if tj.exists():
                    tj.rename(quar / tj.name)
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
