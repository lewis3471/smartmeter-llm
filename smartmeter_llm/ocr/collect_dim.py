#!/usr/bin/env python3
"""Sammelt gedimmte Trainingsbilder: LED-Helligkeits-Sweep, Label nur wenn
lokales OCR und Gemini uebereinstimmen (hohes Vertrauen).

Hauptscript vorher stoppen (LED-Konflikt)!
Nutzung: .venv/bin/python scripts/ocr/collect_dim.py [runden=3]
"""

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from meter_reader import (  # noqa: E402
    ESPHOME_API_KEY, ESPHOME_HOST, gemini_read,
)
from aioesphomeapi import APIClient  # noqa: E402
from local_reader import LocalReader  # noqa: E402

BRIGHTNESS = [0.6, 0.5, 0.45, 0.4, 0.35, 0.3]
OUT = ROOT / "samples" / (time.strftime("%Y%m%d") + "_dim")


async def capture(client, light_key, frames_sink, brightness, n_frames=6):
    client.light_command(key=light_key, state=True, brightness=brightness)
    await asyncio.sleep(2.5)
    got = None
    for _ in range(n_frames):
        n = len(frames_sink)
        client.request_single_image()
        for _ in range(50):
            await asyncio.sleep(0.2)
            if len(frames_sink) > n:
                got = frames_sink[-1]
                break
    client.light_command(key=light_key, state=False)
    await asyncio.sleep(0.5)
    return got


async def main(rounds: int):
    reader = LocalReader()
    OUT.mkdir(parents=True, exist_ok=True)
    client = APIClient(ESPHOME_HOST, 6053, password=None, noise_psk=ESPHOME_API_KEY)
    await client.connect(login=True)
    entities, _ = await client.list_entities_services()
    light_key = next(e.key for e in entities if type(e).__name__ == "LightInfo")
    frames: list[bytes] = []
    client.subscribe_states(
        lambda s: frames.append(bytes(s.data)) if getattr(s, "data", None) else None
    )
    saved = skipped = 0
    for rnd in range(rounds):
        for b in BRIGHTNESS:
            img = await capture(client, light_key, frames, b)
            if img is None:
                print(f"r{rnd} b={b}: kein Frame")
                continue
            try:
                local, conf = reader.read(img)
            except ValueError:
                local, conf = None, 0.0
            try:
                gem = gemini_read(img)
            except Exception as e:
                print(f"r{rnd} b={b}: Gemini-Fehler {e}")
                continue
            if local == gem:
                stem = f"{time.strftime('%H%M%S')}_b{int(b*100)}"
                (OUT / f"{stem}.jpg").write_bytes(img)
                (OUT / f"{stem}.json").write_text(json.dumps(gem))
                saved += 1
                print(f"r{rnd} b={b}: OK {gem} (lokal c={conf:.2f}) -> gespeichert")
            else:
                skipped += 1
                print(f"r{rnd} b={b}: UNEINIG lokal={local} c={conf:.2f} "
                      f"gemini={gem} -> verworfen")
    try:
        await client.disconnect()
    except Exception:
        pass
    print(f"\n{saved} gespeichert, {skipped} uneinig -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3))
