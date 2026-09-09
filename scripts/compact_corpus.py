#!/usr/bin/env python3
"""Korpus-Retention: haelt training-data/ dauerhaft klein (läuft in
make retrain vor dem Push).

Befund 23.07.: events/ war mit 71.797 Dateien / 1,1 GB fuer 93% des
Korpus verantwortlich — reine Diagnose-Frames, nie Trainingsmaterial;
Failsafe-Stuerme schreiben tausende quasi identische Bilder pro Stunde.

Regeln:
- events/:        max EVENTS_DAYS Tage UND max EVENTS_PER_DAY neueste
                  Dateien pro Tag (Sturm-Ausduennung)
- control/:       max CONTROL_DAYS Tage
- disagreements/, seg/, Routine-Tagesordner (2*/): max RAW_DAYS Tage —
  ihre Essenz lebt laenger in den Konsens-Labels
- auto/ und quarantine/: unangetastet (das ist der kuratierte Korpus)

Nur Arbeitskopie: die Git-Historie waechst weiter — der NUC begrenzt
sich per Auto-Re-Clone (run.sh), GitHub darf wachsen.
"""
import re
import sys
import time
from pathlib import Path

EVENTS_DAYS = 10
EVENTS_PER_DAY = 300
CONTROL_DAYS = 14
RAW_DAYS = 45

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "training-data")


def day_of(name: str) -> str | None:
    m = re.match(r"(20\d{6})", name)
    return m.group(1) if m else None


def main():
    today = int(time.strftime("%Y%m%d"))
    removed = freed = 0

    def rm(f: Path):
        nonlocal removed, freed
        freed += f.stat().st_size
        f.unlink()
        removed += 1

    # events/: Alter + Sturm-Ausduennung
    for daydir in sorted(ROOT.glob("events/*/")):
        d = day_of(daydir.name)
        if not d:
            continue
        files = sorted(daydir.iterdir())
        if today - int(d) > EVENTS_DAYS:
            for f in files:
                rm(f)
        else:
            # .json+.jpg-Paare: neueste EVENTS_PER_DAY Paare behalten
            stems = sorted({f.stem for f in files}, reverse=True)
            for stem in stems[EVENTS_PER_DAY:]:
                for f in (daydir / f"{stem}.json", daydir / f"{stem}.jpg"):
                    if f.exists():
                        rm(f)
        if not any(daydir.iterdir()):
            daydir.rmdir()

    # control/: Alter
    for f in sorted(ROOT.glob("control/*.jsonl")):
        d = day_of(f.name)
        if d and today - int(d) > CONTROL_DAYS:
            rm(f)

    # Roh-Evidence: Alter (auto/ + quarantine/ bleiben)
    for pattern in ("disagreements/*", "seg/*"):
        for f in sorted(ROOT.glob(pattern)):
            d = day_of(f.name)
            if d and today - int(d) > RAW_DAYS and f.is_file():
                rm(f)
    for daydir in sorted(ROOT.glob("2*/")):
        d = day_of(daydir.name)
        if d and today - int(d) > RAW_DAYS:
            for f in daydir.iterdir():
                rm(f)
            daydir.rmdir()

    print(f"Kompaktiert: {removed} Dateien, {freed / 1e6:.0f} MB frei")


if __name__ == "__main__":
    main()
