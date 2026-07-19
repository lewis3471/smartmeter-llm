#!/usr/bin/env python3
"""Synchronize NUC OCR evidence, retrain, and publish the model.

Läuft aus einem beschreibbaren Git-Checkout (auf dem NUC: /data/feedback-repo,
Auth über GIT_SSH_COMMAND mit Deploy-Key). Ablauf pro Lauf:

1. pull --rebase (Modell/Daten anderer Maschinen integrieren)
2. Evidence einsammeln: disagreements/ + events/ komplett, Routine-Samples
   (YYYYMMDD/) nur jedes N-te (Klassen-Balance ohne Repo-Flut)
3. Disagreements mit gueltigem Gemini-Label -> training-data/auto/
4. commit/push; NUR nach erfolgreichem Push wird lokal geprunt

Training passiert hier NICHT mehr — Gemini-Labels sind fehlerbehaftet und
werden erst auditiert/relabelt (scripts/ocr/relabel.py). Trainiert wird
bewusst: manuell oder via AUTO_TRAIN_HOUR im meter_reader.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import zlib
from pathlib import Path

ROUTINE_KEEP_EVERY = 20   # jedes N-te Routine-Sample committen
PRUNE_AGE_DAYS = 3        # nicht-synced Routine-Reste lokal aufraeumen


def log(msg: str, err: bool = False):
    print(time.strftime("[%m-%d %H:%M:%S]"), msg,
          file=sys.stderr if err else sys.stdout, flush=True)


def run(args, cwd: Path, check=True):
    return subprocess.run(args, cwd=cwd, text=True, check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def changed(repo: Path, path: str) -> bool:
    return bool(run(["git", "status", "--porcelain", "--", path], repo).stdout.strip())


def keep_routine(name: str) -> bool:
    return zlib.crc32(name.encode()) % ROUTINE_KEEP_EVERY == 0


def valid_label(d: dict) -> bool:
    # kWh hat auf dem ESY11 immer 6 Ziffern (fuehrende 0 -> int 5-6-stellig).
    # Gemini trunkiert gelegentlich auf 4 Stellen — solche Labels vergiften
    # das Training und werden hier verworfen.
    return (isinstance(d.get("kwh"), int) and isinstance(d.get("w"), int)
            and 10_000 <= d["kwh"] < 1_000_000 and abs(d["w"]) <= 20_000
            and d["kwh"] != 888888 and abs(d["w"]) not in (88888, 888888))


def collect_evidence(samples: Path, evidence: Path) -> tuple[list[Path], int]:
    """Kopiert Neues aus samples/ nach training-data/. -> (kopierte Quellen,
    Anzahl neuer Labels)."""
    copied, new_labels = [], 0
    # 1) Disagreements + Events: vollstaendig (das ist das Gold)
    for sub in ("disagreements", "events"):
        src = samples / sub
        if not src.exists():
            continue
        for f in sorted(src.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(samples)
            dst = evidence / rel
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            copied.append(f)
    # 2) Disagreements mit gueltigem Gemini-Label -> auto/ (Trainingslabel)
    auto = evidence / "auto"
    auto.mkdir(parents=True, exist_ok=True)
    for jf in sorted((evidence / "disagreements").glob("*.json")):
        target = auto / jf.name
        if target.exists() or not jf.with_suffix(".jpg").exists():
            continue
        try:
            gem = json.loads(jf.read_text()).get("gemini")
            if not gem or not valid_label(gem):
                continue
            target.write_text(json.dumps(gem))
            shutil.copy2(jf.with_suffix(".jpg"), auto / f"{jf.stem}.jpg")
            new_labels += 1
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # 3) Routine-Samples (YYYYMMDD/): nur jedes N-te fuer die Klassen-Balance
    for jf in sorted(samples.glob("2*/*.json")):
        if not keep_routine(jf.stem) or not jf.with_suffix(".jpg").exists():
            continue
        rel = jf.relative_to(samples)
        dst = evidence / rel
        if dst.exists():
            continue
        try:
            if not valid_label(json.loads(jf.read_text())):
                continue
        except (OSError, ValueError):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(jf, dst)
        shutil.copy2(jf.with_suffix(".jpg"), dst.with_suffix(".jpg"))
        copied.extend([jf, jf.with_suffix(".jpg")])
        new_labels += 1
    # 4) Regler-Telemetrie (control/*.jsonl): heutige Datei waechst noch und
    #    wird nur kopiert (Overwrite); abgeschlossene Tage werden geprunt
    ctl = samples / "control"
    if ctl.exists():
        (evidence / "control").mkdir(parents=True, exist_ok=True)
        today = time.strftime("%Y%m%d")
        for f in sorted(ctl.glob("*.jsonl")):
            dst = evidence / "control" / f.name
            if dst.exists() and dst.stat().st_size == f.stat().st_size:
                continue
            shutil.copy2(f, dst)
            if f.stem != today:
                copied.append(f)
    return copied, new_labels


def prune(samples: Path, synced: list[Path]):
    """Nach erfolgreichem Push: synced Dateien loeschen (+ zugehoerige JPGs)
    und alte, nicht ausgewaehlte Routine-Samples aufraeumen."""
    removed = 0
    for f in synced:
        for victim in {f, f.with_suffix(".jpg"), f.with_suffix(".json")}:
            if victim.exists() and samples in victim.parents:
                victim.unlink()
                removed += 1
    cutoff = time.time() - PRUNE_AGE_DAYS * 86400
    for day_dir in samples.glob("2*/"):
        for f in list(day_dir.iterdir()):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        if not any(day_dir.iterdir()):
            day_dir.rmdir()
    log(f"Prune: {removed} lokale Dateien entfernt")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--samples", type=Path, required=True,
                        help="SAVE_SAMPLES_DIR des meter_reader")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    repo, samples = args.repo.resolve(), args.samples.resolve()

    if args.push:
        r = run(["git", "pull", "--rebase", "origin", "HEAD"], repo, check=False)
        if r.returncode:
            log(f"git pull fehlgeschlagen, Lauf uebersprungen: "
                f"{r.stdout.strip()[-200:]}", err=True)
            return
    evidence = repo / "training-data"
    evidence.mkdir(exist_ok=True)
    copied, new_labels = collect_evidence(samples, evidence)
    log(f"Evidence: {len(copied)} Dateien kopiert, {new_labels} neue Labels")

    if changed(repo, "training-data"):
        run(["git", "add", "-f", "training-data"], repo)
        run(["git", "commit", "-m", "ocr: sync evidence"], repo)
        log("Commit erstellt")
    if args.push:
        r = run(["git", "push", "origin", "HEAD"], repo, check=False)
        if r.returncode:
            log(f"git push fehlgeschlagen — Prune uebersprungen: "
                f"{r.stdout.strip()[-200:]}", err=True)
            return
        log("Push ok")
        if changed(repo, "training-data"):
            # Sicherheitsgurt: Evidence NICHT vollstaendig committet (z.B.
            # gitignore-Panne) -> niemals lokale Originale loeschen
            log("training-data nicht vollstaendig committet — "
                "Prune uebersprungen", err=True)
            return
        prune(samples, copied)


if __name__ == "__main__":
    main()
