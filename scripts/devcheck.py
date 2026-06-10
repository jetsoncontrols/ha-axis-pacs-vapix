#!/usr/bin/env python3
"""Standalone smoke-test for the Axis PACS VAPIX client (no Home Assistant).

Exercises identity, local-door enumeration, door state, and the PullPoint event
stream against a real controller — useful for fast iteration outside HA.

Usage:
    AXIS_HOST=10.1.4.12 AXIS_USER=root AXIS_PASS=secret python3 scripts/devcheck.py
    python3 scripts/devcheck.py 10.1.4.12 root secret [--open]

Requires httpx (``pip install httpx``). Read-only unless ``--open`` is passed,
which momentarily AccessDoors the first local door to confirm live eventing.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Import the HA-independent client package without importing the HA integration.
_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "axis_pacs"
sys.path.insert(0, str(_PKG))

from vapix.client import AxisPacsClient  # noqa: E402
from vapix.events import PullPointManager  # noqa: E402


async def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    host = positional[0] if positional else os.environ.get("AXIS_HOST", "")
    user = positional[1] if len(positional) > 1 else os.environ.get("AXIS_USER", "root")
    password = positional[2] if len(positional) > 2 else os.environ.get("AXIS_PASS", "")
    do_open = "--open" in sys.argv[1:]

    if not host or not password:
        print(__doc__)
        return 2

    client = AxisPacsClient(host, user, password)
    try:
        identity = await client.async_get_identity()
        print(
            f"Device : {identity.product_full_name or identity.model} "
            f"| fw {identity.firmware} | serial {identity.serial}"
        )

        all_doors = await client.async_get_door_info_list()
        local = [d for d in all_doors if d.is_local_to(identity.serial)]
        print(
            f"Doors  : {len(all_doors)} cluster-wide, {len(local)} local to this controller"
        )
        for door in local:
            state = await client.async_get_door_state(door.token)
            print(f"   - {door.name!r:40s} mode={state.mode.value:10s} token={door.token}")

        if not local:
            print("No local doors to monitor.")
            return 0

        events = []
        manager = PullPointManager(
            client,
            on_event=events.append,
            on_resync=lambda: asyncio.sleep(0),
        )
        task = asyncio.create_task(manager.async_run())
        try:
            if do_open:
                await asyncio.sleep(2)  # let the subscription establish
                print(f"\n--open: AccessDoor {local[0].name!r} (momentary)\n")
                await client.async_access(local[0].token)
            print("Listening for events for ~12s ...")
            await asyncio.sleep(12)
        finally:
            await manager.async_stop()
            task.cancel()

        door_events = [n for n in events if n.door_token]
        for n in door_events:
            print(f"   EVENT {n.topic.rsplit('/', 1)[-1]:20s} state={n.state} token={n.door_token}")
        print(f"\nCaptured {len(events)} notifications ({len(door_events)} door-related).")
        return 0
    finally:
        await client.async_close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
