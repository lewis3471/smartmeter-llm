"""Persist OCR evidence in a Git-friendly outbox.

Writing the evidence is deliberately independent of Git.  The meter loop must
never wait for a network operation; ``nuc_feedback_sync.py`` uploads and trains
from this directory in a separate process.
"""

import json
import time
from pathlib import Path


def save_event(root: str, image: bytes | None, kind: str, **data) -> Path | None:
    """Atomically save metadata and, when available, the source JPEG."""
    if not root:
        return None
    directory = Path(root) / "events" / time.strftime("%Y%m%d")
    directory.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%H%M%S") + f"_{time.time_ns() % 1_000_000:06d}"
    payload = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "kind": kind, **data}
    target = directory / stem
    if image:
        (target.with_suffix(".jpg")).write_bytes(image)
    # Rename prevents the sync process from committing a partial event.
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    tmp.replace(target.with_suffix(".json"))
    return target
