#!/usr/bin/env python3
"""Holt ein Einzelbild von der ESP32-Cam über die ESPHome Native API (Port 6053).

Nutzung: .venv/bin/python scripts/fetch_snapshot_esphome.py [ausgabe.jpg]
Benötigt in .env: ESPHOME_HOST, ESPHOME_API_KEY (Base64, aus ESPHome Builder).
"""

import asyncio
import os
import sys
from pathlib import Path

from aioesphomeapi import APIClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from meter_reader import load_env  # noqa: E402

load_env()

HOST = os.environ.get("ESPHOME_HOST", "192.168.178.58")
KEY = os.environ["ESPHOME_API_KEY"]
OUT = sys.argv[1] if len(sys.argv) > 1 else "snapshot.jpg"


async def main():
    client = APIClient(HOST, 6053, password=None, noise_psk=KEY)
    await client.connect(login=True)
    info = await client.device_info()
    print(f"Verbunden: {info.name} (ESPHome {info.esphome_version})")

    fut: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()

    def on_state(state):
        # CameraState enthält das JPEG in .data
        if hasattr(state, "data") and state.data and not fut.done():
            fut.set_result(bytes(state.data))

    client.subscribe_states(on_state)
    client.request_single_image()
    img = await asyncio.wait_for(fut, timeout=15)
    Path(OUT).write_bytes(img)
    print(f"{OUT}: {len(img)} Bytes")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
