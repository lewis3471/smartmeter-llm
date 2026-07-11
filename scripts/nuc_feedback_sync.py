#!/usr/bin/env python3
"""Synchronize NUC OCR evidence, retrain, and publish the model.

Run this from a writable Git checkout on the NUC. Authentication is delegated
to normal Git SSH credentials (a deploy key or credential helper), never an
environment variable or command line token.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run(args, cwd: Path, check=True):
    return subprocess.run(args, cwd=cwd, text=True, check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def changed(repo: Path, path: str) -> bool:
    return bool(run(["git", "status", "--porcelain", "--", path], repo).stdout.strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--samples", type=Path, required=True,
                        help="SAVE_SAMPLES_DIR used by meter_reader")
    parser.add_argument("--min-new-labels", type=int, default=10)
    parser.add_argument("--push", action="store_true", help="push commits to origin")
    args = parser.parse_args()
    repo, samples = args.repo.resolve(), args.samples.resolve()
    # Integrate another machine's retrained model before creating ours. The
    # captured files are still safely in ``samples`` if the remote is offline.
    if args.push:
        run(["git", "pull", "--rebase", "origin", "HEAD"], repo)
    evidence = repo / "training-data"
    evidence.mkdir(exist_ok=True)
    # Copy, not move: the reader's evidence remains available during a Git outage.
    run(["rsync", "-a", "--ignore-existing", f"{samples}/", f"{evidence}/"], repo)

    # A disagreement becomes a training label only when Gemini supplied a
    # structurally valid answer.  Raw rejected frames stay as review evidence.
    auto = evidence / "auto"
    auto.mkdir(exist_ok=True)
    promoted = []
    for source in (evidence / "disagreements").glob("*.json"):
        target = auto / source.name
        if target.exists():
            continue
        try:
            item = json.loads(source.read_text())
            label = item["gemini"]
            if not (isinstance(label.get("kwh"), int) and
                    isinstance(label.get("w"), int) and
                    0 < label["kwh"] < 1_000_000 and abs(label["w"]) <= 20_000):
                continue
            image = source.with_suffix(".jpg")
            if not image.exists():
                continue
            target.write_text(json.dumps(label))
            (auto / image.name).write_bytes(image.read_bytes())
            promoted.append(target)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Count any newly copied Gemini-labelled frame, including routine Gemini
    # reads saved under YYYYMMDD/, not only cross-check disagreements.
    tracked = set(run(["git", "ls-files", "training-data"], repo).stdout.splitlines())
    new_labels = []
    for label_file in evidence.glob("*/*.json"):
        if str(label_file.relative_to(repo)) in tracked:
            continue
        try:
            label = json.loads(label_file.read_text())
            if (label_file.with_suffix(".jpg").exists() and
                    isinstance(label.get("kwh"), int) and
                    isinstance(label.get("w"), int)):
                new_labels.append(label_file)
        except (OSError, ValueError, TypeError):
            continue
    model = repo / "scripts/ocr/model.npz"
    needs_training = len(new_labels) >= args.min_new_labels
    if needs_training:
        result = run([sys.executable, "scripts/ocr/train.py", str(evidence)], repo, check=False)
        print(result.stdout, end="")
        if result.returncode:
            raise SystemExit("training failed; evidence was retained and was not committed")
        # Keep the add-on payload identical to the NUC runtime model.
        run(["scripts/sync_addon.sh"], repo)

    paths = ["training-data"]
    if needs_training:
        paths += ["scripts/ocr/model.npz", "smartmeter_llm/ocr/model.npz"]
    dirty = any(changed(repo, path) for path in paths)
    if not dirty:
        if args.push:
            ahead = run(["git", "rev-list", "--count", "@{u}..HEAD"], repo,
                        check=False)
            if ahead.returncode == 0 and int(ahead.stdout.strip() or 0):
                run(["git", "push", "origin", "HEAD"], repo)
        return
    run(["git", "add", "-f", *paths], repo)
    run(["git", "commit", "-m", "ocr: sync evidence" + (" and retrain model" if needs_training else "")], repo)
    if args.push:
        run(["git", "push", "origin", "HEAD"], repo)


if __name__ == "__main__":
    main()
