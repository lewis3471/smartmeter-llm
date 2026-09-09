#!/usr/bin/env python3
"""Capture manually verified live kWh frames for an urgent OCR correction."""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meter_reader  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--kwh", type=int, required=True)
    p.add_argument("--count", type=int, default=30)
    p.add_argument("--output", type=Path, default=Path("samples/manual_live"))
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    saved = 0
    try:
        while saved < args.count:
            image = meter_reader.get_snapshot()
            try:
                # Reject segment-test/dark frames but do not use its mistaken
                # number as a label; the human-provided kWh is authoritative.
                meter_reader._local_reader.read(image)
            except ValueError:
                continue
            stem = f"{time.strftime('%Y%m%d_%H%M%S')}_{saved:03d}"
            (args.output / f"{stem}.jpg").write_bytes(image)
            (args.output / f"{stem}.json").write_text(json.dumps({"kwh": args.kwh}))
            saved += 1
            print(f"saved {saved}/{args.count}")
            time.sleep(0.25)
    finally:
        if meter_reader._cam is not None:
            meter_reader._cam.shutdown()


if __name__ == "__main__":
    main()
