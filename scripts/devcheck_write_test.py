#!/usr/bin/env python3
"""One-shot REVERSIBLE write test for the Axis PACS credential path.

Creates a throwaway cardholder + a DISABLED PIN credential attached to a given
access profile, reads them back to confirm the round-trip, then deletes both and
confirms removal. Cleanup runs in a finally block so nothing can be orphaned.

This is the ONLY script in the repo that writes. It mutates the shared cluster
database; it is written to touch a single brand-new throwaway record only.

Usage:
    AXIS_HOST=10.10.4.3 AXIS_USER=root AXIS_PASS=secret \
      AXIS_PROFILE=<access-profile-token> python3 scripts/devcheck_write_test.py
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "axis_pacs"
sys.path.insert(0, str(_PKG))

from vapix import soap  # noqa: E402
from vapix.client import AxisPacsClient  # noqa: E402

TEST_NAME = "HA TEST — delete me"


def returned_token(root: ET.Element) -> str | None:
    """Find a *Token element (Token/UserToken/CredentialToken) in a Set* reply."""
    for el in root.iter():
        local = el.tag.rsplit("}", 1)[-1]
        if local in ("Token", "UserToken", "CredentialToken") and el.text:
            return el.text.strip()
    return None


def dump(label: str, root: ET.Element) -> None:
    body = ET.tostring(root, encoding="unicode")
    body = body[body.find("Body") :]  # trim envelope/namespace noise
    print(f"   {label}: {body[:240]}")


async def main() -> int:
    host = os.environ.get("AXIS_HOST", "")
    user = os.environ.get("AXIS_USER", "root")
    password = os.environ.get("AXIS_PASS", "")
    profile = os.environ.get("AXIS_PROFILE", "")
    if not host or not password or not profile:
        print(__doc__)
        return 2

    pin = f"{secrets.randbelow(900000) + 100000}"  # random 6-digit, never enabled
    client = AxisPacsClient(host, user, password)
    user_token = cred_token = ""
    try:
        print(f"PIN under test (disabled): {pin}\n")

        print("1) SetUser (create throwaway holder)")
        root = await client.async_call(soap.set_user("", TEST_NAME, "HA", "Test"))
        dump("response", root)
        user_token = returned_token(root) or ""
        print(f"   -> user_token={user_token!r}")
        if not user_token:
            print("   ABORT: no user token returned; not creating a credential.")
            return 1

        print("\n2) SetCredential (DISABLED PIN, attached to profile)")
        root = await client.async_call(
            soap.set_credential(
                "", user_token, {"PIN": pin}, [profile],
                enabled=False, description=TEST_NAME,
            )
        )
        dump("response", root)
        cred_token = returned_token(root) or ""
        print(f"   -> cred_token={cred_token!r}")

        print("\n3) Read back")
        u = next((x for x in await client.async_get_users(max_total=5000)
                  if x.token == user_token), None)
        c = await client.async_get_credential(cred_token) if cred_token else None
        print(f"   user found : {u is not None} (name={u.name!r})" if u else "   user found : False")
        if c:
            print(f"   cred found : True enabled={c.enabled} "
                  f"pin_matches={c.pin == pin} profiles={c.access_profile_tokens}")
        else:
            print("   cred found : False")
        return 0
    finally:
        print("\n4) Cleanup")
        if cred_token:
            try:
                await client.async_remove_credential(cred_token)
                gone = await client.async_get_credential(cred_token)
                print(f"   credential removed; re-read gone={gone is None}")
            except Exception as err:  # noqa: BLE001
                print(f"   !! credential cleanup FAILED: {err} (token={cred_token})")
        if user_token:
            try:
                await client.async_remove_user(user_token)
                still = any(x.token == user_token
                            for x in await client.async_get_users(max_total=5000))
                print(f"   user removed; re-read gone={not still}")
            except Exception as err:  # noqa: BLE001
                print(f"   !! user cleanup FAILED: {err} (token={user_token})")
        await client.async_close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
