#!/usr/bin/env python3
"""Label-Bereinigung vor dem OCR-Training.  Lauf:
    .venv/bin/python tests/test_ocr_labels.py

Hintergrund: Das Retrain vom 23.09.2026 hat 128 auto-Labels mit
sechsstelliger kWh aufgenommen (358914 statt 35891 — Geminis Nachkomma-
Signatur vom 25./28.07.). An diesen Tagen war schon der Tagesmedian
sechsstellig, also hielt clean() die falschen Labels und warf die
richtigen. labels_for() verschiebt jede Ziffer um eine Zelle; das
Modell las danach die 9 an Stelle 5 als 1 (36297 -> 36217), sobald das
Licht nachts dunkler wurde.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "ocr"))
import train as T  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'}   {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def s(pfad, kwh, **extra):
    return (Path(pfad), {"kwh": kwh, **extra})


def test_sechsstellige_kwh_fliegen_raus_auch_wenn_sie_den_tag_dominieren():
    """Genau der 28.07.: mehr falsche als richtige Labels am selben Tag."""
    tag = [s(f"training-data/auto/20260728_12{i:04d}.jpg", 358914) for i in range(5)]
    tag += [s(f"training-data/auto/20260728_13{i:04d}.jpg", 35891) for i in range(2)]
    good, dropped = T.clean(tag)
    kwh = sorted({g[1]["kwh"] for g in good})
    check("nur_die_fuenfstelligen_bleiben", kwh == [35891], str(kwh))
    check("verworfene_werden_gezaehlt", dropped == 5, str(dropped))


def test_tagesordner_werden_gegen_ihren_tag_geprueft():
    """Tagesordner-Dateien heissen HHMMSS.json. Frueher war name[:8] der
    Tagesschluessel — jedes Bild seine eigene Gruppe, nie ein Ausreisser."""
    tag = [s(f"training-data/20260915/1{i:05d}.jpg", 36215) for i in range(5)]
    tag.append(s("training-data/20260915/235959.jpg", 36265))  # 50 daneben
    good, dropped = T.clean(tag)
    check("ausreisser_im_tagesordner_erkannt", dropped == 1,
          f"{dropped} verworfen")
    check("tag_aus_ordnername",
          T.day_of(Path("training-data/20260915/235959.jpg")) == "20260915")
    check("tag_aus_dateiname",
          T.day_of(Path("training-data/auto/20260923_081642.jpg")) == "20260923")


for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    print(f"\n{fn.__name__}:")
    fn()

print()
if FAILS:
    print(f"{len(FAILS)} FEHLGESCHLAGEN: {', '.join(FAILS)}")
    sys.exit(1)
print("OCR-Labels gruen")
