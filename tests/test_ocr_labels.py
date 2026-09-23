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

Dazu die Kasten-Wahl im LocalReader (ebenfalls 23.09.): der Rueckfall auf
andere Kaesten darf eine im eigenen Kasten bekannte Ziffer nicht knapp
ueberstimmen.
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


def _leser(eintraege):
    """LocalReader auf einer kuenstlichen Basis: [(slot, label, vektor)].
    prep_cell wird zur Identitaet, damit die Kosinus-Werte exakt die
    konstruierten sind."""
    import tempfile
    import numpy as np
    import local_reader as lr
    X = np.array([v for _, _, v in eintraege], np.float32)
    f = Path(tempfile.mkdtemp()) / "m.npz"
    np.savez(f, X=X.astype(np.float16), y=np.array([l for _, l, _ in eintraege]),
             slots=np.array([sl for sl, _, _ in eintraege]), anchor=np.zeros((2, 2)))
    lr.prep_cell = lambda c: np.asarray(c, np.float32).ravel()
    return lr.LocalReader(f)


def _vek(cos, achse, n=8):
    import numpy as np
    v = np.zeros(n, np.float32)
    v[0], v[achse] = cos, (1 - cos * cos) ** 0.5
    return v


def test_eigener_kasten_schlaegt_knappen_rueckfall():
    """23.09.: Kasten 2 (immer eine 3) kippte zur 0 aus Kasten 1 — drei
    fremde Nullen minimal naeher als die eine eigene Drei. Frage: e0."""
    import numpy as np
    basis = [(1, "3", _vek(0.950, 1))] + [(0, "0", _vek(0.952, 2))] * 3
    r = _leser(basis)
    frage = [np.zeros(8, np.float32), _vek(1.0, 1)]
    p, _ = r._predict(frage)
    check("knapper_rueckfall_ueberstimmt_nicht", p[1] == "3", str(p))


def test_neue_ziffer_kommt_ueber_den_rueckfall():
    """Eine Ziffer, die im eigenen Kasten nie vorkam (363xx: die 3 an
    Stelle 4), muss weiter ueber die anderen Kaesten erkannt werden."""
    import numpy as np
    basis = [(3, "2", _vek(0.90, 1))] + [(1, "3", _vek(0.99, 2))] * 3
    r = _leser(basis)
    p, _ = r._predict([np.zeros(8, np.float32)] * 3 + [_vek(1.0, 1)])
    check("klar_besserer_rueckfall_gewinnt", p[3] == "3", str(p))


for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    print(f"\n{fn.__name__}:")
    fn()

print()
if FAILS:
    print(f"{len(FAILS)} FEHLGESCHLAGEN: {', '.join(FAILS)}")
    sys.exit(1)
print("OCR-Labels gruen")
