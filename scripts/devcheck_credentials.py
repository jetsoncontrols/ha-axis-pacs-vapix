#!/usr/bin/env python3
"""Read-only verification of the Axis PACS access-code layer (no Home Assistant).

Exercises the credential/user/access-profile/schedule/access-point READ paths
against a real controller, and resolves which access profiles grant the local
door (the binding a new PIN would reference). It also prints the SOAP envelopes
the write builders would generate — WITHOUT sending them — so the create/modify
path can be validated offline before any write touches a live system.

Usage:
    AXIS_HOST=10.10.4.3 AXIS_USER=root AXIS_PASS=secret python3 scripts/devcheck_credentials.py

Requires httpx. This script performs NO writes. PINs and names are masked.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "axis_pacs"
sys.path.insert(0, str(_PKG))

from vapix import soap  # noqa: E402
from vapix.client import AxisPacsClient  # noqa: E402


def mask(value: str) -> str:
    """Mask a secret value, revealing only its length."""
    return f"<{len(value)} chars>" if value else "<empty>"


async def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    host = positional[0] if positional else os.environ.get("AXIS_HOST", "")
    user = positional[1] if len(positional) > 1 else os.environ.get("AXIS_USER", "root")
    password = positional[2] if len(positional) > 2 else os.environ.get("AXIS_PASS", "")
    if not host or not password:
        print(__doc__)
        return 2

    client = AxisPacsClient(host, user, password)
    try:
        identity = await client.async_get_identity()
        print(f"Device : {identity.model} | fw {identity.firmware} | serial {identity.serial}")

        local = await client.async_get_local_doors(identity.serial)
        print(f"Local doors: {len(local)} -> {[d.name for d in local]}")

        access_points = await client.async_get_access_points()
        schedules = await client.async_get_schedules()
        profiles = await client.async_get_access_profiles()
        print(
            f"Access points: {len(access_points)} | schedules: {len(schedules)} "
            f"| access profiles: {len(profiles)}"
        )
        print("Schedules:", [f"{s.name}={s.token}" for s in schedules])

        # Resolve door -> access points -> the profiles that grant it.
        for door in local:
            ap_tokens = {
                ap.token for ap in access_points if ap.door_token == door.token
            }
            granting = [p for p in profiles if ap_tokens & set(p.entity_tokens)]
            print(f"\nDoor {door.name!r} ({door.token})")
            print(f"   access points: {sorted(ap_tokens)}")
            print(f"   profiles granting this door: "
                  f"{[(p.name, p.token) for p in granting] or 'NONE'}")

        # Users + credentials (counts + one masked sample to confirm parsing).
        users = await client.async_get_users(max_total=500)
        creds = await client.async_get_credentials(max_total=500)
        with_pin = [c for c in creds if c.has_pin]
        print(f"\nUsers: {len(users)} | credentials: {len(creds)} "
              f"| credentials with a PIN: {len(with_pin)}")
        if users:
            u = users[0]
            print(f"   sample user : token={u.token} name=<redacted> "
                  f"first={mask(u.first_name)} last={mask(u.last_name)}")
        if creds:
            c = creds[0]
            masked = {k: mask(v) for k, v in c.id_data.items()}
            print(f"   sample cred : token={c.token} enabled={c.enabled} "
                  f"status={c.status} id_data={masked} profiles={c.access_profile_tokens}")

        # Offline validation of the WRITE builders (NOT sent to the device).
        print("\n--- generated write envelopes (NOT sent) ---")
        print("SetUser:\n  " + soap.set_user("", "Doe, Jane", "Jane", "Doe"))
        print("SetCredential:\n  " + soap.set_credential(
            "", "Axis-EXAMPLE:1.2", {"PIN": "1234"},
            ["EXAMPLE-PROFILE-TOKEN"], description="Doe, Jane",
        ))
        return 0
    finally:
        await client.async_close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
