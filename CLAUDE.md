# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

HACS-compatible Home Assistant custom integration for Axis VAPIX access controllers (A1001, A1601, A1610/A1210 on the VAPIX-OS firmware track). Integration domain: `axis_pacs`. One config entry per physically-connected controller. Two capabilities:

- **Door control (v0.1):** each door **local to the connected controller** is a `lock` entity, with live state from the controller's event stream (no polling).
- **Access-code management (v0.2):** create/list/remove PIN & card credentials via integration services, opted-in per controller (see below).

Doors are filtered to the local controller, but the **credential database is shared cluster-wide** — so unlike door control, access-code management is inherently a cluster-wide concern (it is not "one controller in isolation").

## Validated device facts (test unit: A1001, fw 1.65.6, 10.1.4.12)

- **Door control is SOAP/ONVIF only:** `POST /vapix/services`, HTTP **digest** auth. JSON `/vapix/doorcontrol` returns 503 on this firmware.
- **Lock state = ONVIF `DoorMode`** (`GetDoorState`). The test door has no physical door/lock monitors, so `DoorMode` is authoritative. Mapping lives in `vapix/models.py` (`LOCKED_MODES` / `UNLOCKED_MODES`).
- **Cluster filtering:** `GetDoorInfoList` returns *all* cluster doors; expose only local ones — a door is local when its token MAC (`Axis-<mac>:<id>`) equals the device serial. Peer doors return an empty `<Capabilities/>`.
- **Events:** ONVIF WS-Eventing **PullPoint** (`CreatePullPointSubscription` → `PullMessages` long-poll) on `/vapix/services`. No JSON `ws-data-stream` / `apidiscovery.cgi` on this firmware (both 404). Door topic `tns1:Door/State/DoorMode`, source key `DoorToken`.
- **Identity** via `GET /axis-cgi/param.cgi?action=list&group=Brand,Properties` (serial == MAC without colons).
- **Access-code stack validated separately:** the full credential/accessrules/schedule/accesscontrol stack was confirmed read **and** write (reversibly) against a live production A1001 cluster on the same firmware — details in *Access-code management* below.

## Architecture

- `custom_components/axis_pacs/vapix/` — **self-contained async client with no Home Assistant imports** (so it runs standalone). httpx + `DigestAuth`, stdlib XML. `soap.py` (SOAP/ONVIF builders + parsers, matched by namespace URI not prefix), `client.py` (door control + event primitives), `events.py` (`PullPointManager` loop with renew + self-healing resubscribe), `models.py` (`DoorMode`, `Door`, `DoorState`, `Notification`). **Keep this package HA-free.**
- `coordinator.py` — push `DataUpdateCoordinator[dict[token, DoorState]]`; seeds via `GetDoorState`, applies live `DoorMode` events, optimistic `set_door_mode` after commands.
- `lock.py` — one `LockEntity` per local door. lock→`LockDoor`, unlock→`UnlockDoor` (permanent), open→`AccessDoor` (momentary, via `LockEntityFeature.OPEN`).
- `config_flow.py` — host/username/password/port/https; `unique_id` = serial. **Options flow** exposes a per-instance `manage_codes` toggle (default off) — the opt-in that designates which controller(s) expose the cluster-wide code services.
- `services.py` — integration-level **access-code** services (`add_pin`, `remove_credential`, `set_credential_enabled`, `list_credentials`, `list_access_profiles`). Registered once in `async_setup`. Each takes a `config_entry_id` to route through a controller, but acts on the **cluster-wide** credential DB (see below); the resolver rejects entries that don't have `manage_codes` enabled. `services.yaml` + `strings.json`/`translations/en.json` provide UI metadata.

## Access-code management (credentials / PINs / cards)

- **Cluster-global, unlike doors:** the credential / cardholder / access-profile / schedule database is replicated across the whole A1001 cluster. `GetUserList`/`GetCredentialList` return *all* of them regardless of which node is queried, while doors/access-points are owned per-controller. So lock entities are local but access codes are cluster-wide — hence domain services, not per-door entity services.
- **Native Axis path (not vanilla ONVIF):** cardholders via `axudb` (`SetUser`/`GetUserList`/`RemoveUser`), credentials via `pacsaxis` (`SetCredential`/`GetCredential(List)`/`RemoveCredential`/`Enable`/`Disable`). PINs are **raw ASCII** (`IdData Name="PIN" Value="1234"`) — the hex/base64 encoding only applies to the unused ONVIF `tcr` path. Access *rules* are ONVIF: profiles (`tar`), schedules (`tsc`), access points (`tac`).
- **A working PIN** = `SetUser` (holder) → `SetCredential` with a PIN `IdData` + a `CredentialAccessProfile/AccessProfile` pointing at an existing profile that grants the door. v1 reuses existing profiles; it does not create them.
- **`SetCredential` gotcha:** `<Status>` is required, in schema order `UserToken, Description, Enabled, Status, IdData*, CredentialAccessProfile*`; omitting it → `ter:InvalidArgs` "occurrence violation in element Credential". A zero-profile credential is valid (grants nothing).

## Commands

- **Standalone client smoke-test** against a real controller (no HA needed):
  `pip install httpx && AXIS_HOST=10.1.4.12 AXIS_USER=root AXIS_PASS=… python3 scripts/devcheck.py`
  (add `--open` to momentarily `AccessDoor` the first local door and confirm eventing).
- **Access-code read-only verification** (lists users/credentials/profiles/schedules/access-points + door→profile map; masks PINs): `AXIS_HOST=… AXIS_USER=… AXIS_PASS=… python3 scripts/devcheck_credentials.py`.
- **Reversible credential write test** (creates a throwaway disabled PIN, verifies round-trip, deletes it — the only writing script; safe against production): `AXIS_HOST=… AXIS_USER=… AXIS_PASS=… AXIS_PROFILE=<profile-token> python3 scripts/devcheck_write_test.py`.
- **Raw read-only protocol probing:** `tools/probe.sh <ip> <user> <pass>` (door control); `tools/probe-credentials.sh <ip> <user> <pass>` (credential/access-management stack).
- **Syntax check** (HA not installed): `python3 -m py_compile custom_components/axis_pacs/**/*.py`.
- **HA tests** (when set up): `pytest` with `pytest-homeassistant-custom-component`.

## Conventions / gotchas

- Door commands (lock/unlock/access) **and** access-code management require an **Admin** account.
- Access codes are **cluster-wide**: enabling `manage_codes` on a controller exposes the *shared* credential DB, and `GetCredentialList`/`GetUserList` return the whole cluster's records (not just this node's). Treat returned PINs as PII.
- Parse SOAP by **namespace URI**, not prefix — prefixes vary across responses.
- PullPoint filter expressions use `onvif:`/`axis:` prefixes; notifications come back as `tns1:`/`tnsaxis:`.
- A1001-only WSDLs (`connection_axis`, `thirdpartycredential`) are deliberately unused.
- Newer A1601/A1610 firmware adds JSON door control + the `ws-data-stream` websocket; gate transport by capability and add those behind detection rather than replacing the SOAP/PullPoint path.
