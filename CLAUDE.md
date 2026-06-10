# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

HACS-compatible Home Assistant custom integration for Axis VAPIX access controllers (A1001, A1601, A1610/A1210 on the VAPIX-OS firmware track). v0.1 exposes each door **local to the connected controller** as a `lock` entity, with live state from the controller's event stream (no polling). Integration domain: `axis_pacs`. One instance per physically-connected controller; A1001 clustering is intentionally not used.

## Validated device facts (test unit: A1001, fw 1.65.6, 10.1.4.12)

- **Door control is SOAP/ONVIF only:** `POST /vapix/services`, HTTP **digest** auth. JSON `/vapix/doorcontrol` returns 503 on this firmware.
- **Lock state = ONVIF `DoorMode`** (`GetDoorState`). The test door has no physical door/lock monitors, so `DoorMode` is authoritative. Mapping lives in `vapix/models.py` (`LOCKED_MODES` / `UNLOCKED_MODES`).
- **Cluster filtering:** `GetDoorInfoList` returns *all* cluster doors; expose only local ones — a door is local when its token MAC (`Axis-<mac>:<id>`) equals the device serial. Peer doors return an empty `<Capabilities/>`.
- **Events:** ONVIF WS-Eventing **PullPoint** (`CreatePullPointSubscription` → `PullMessages` long-poll) on `/vapix/services`. No JSON `ws-data-stream` / `apidiscovery.cgi` on this firmware (both 404). Door topic `tns1:Door/State/DoorMode`, source key `DoorToken`.
- **Identity** via `GET /axis-cgi/param.cgi?action=list&group=Brand,Properties` (serial == MAC without colons).

## Architecture

- `custom_components/axis_pacs/vapix/` — **self-contained async client with no Home Assistant imports** (so it runs standalone). httpx + `DigestAuth`, stdlib XML. `soap.py` (SOAP/ONVIF builders + parsers, matched by namespace URI not prefix), `client.py` (door control + event primitives), `events.py` (`PullPointManager` loop with renew + self-healing resubscribe), `models.py` (`DoorMode`, `Door`, `DoorState`, `Notification`). **Keep this package HA-free.**
- `coordinator.py` — push `DataUpdateCoordinator[dict[token, DoorState]]`; seeds via `GetDoorState`, applies live `DoorMode` events, optimistic `set_door_mode` after commands.
- `lock.py` — one `LockEntity` per local door. lock→`LockDoor`, unlock→`UnlockDoor` (permanent), open→`AccessDoor` (momentary, via `LockEntityFeature.OPEN`).
- `config_flow.py` — host/username/password/port/https; `unique_id` = serial.

## Commands

- **Standalone client smoke-test** against a real controller (no HA needed):
  `pip install httpx && AXIS_HOST=10.1.4.12 AXIS_USER=root AXIS_PASS=… python3 scripts/devcheck.py`
  (add `--open` to momentarily `AccessDoor` the first local door and confirm eventing).
- **Raw read-only protocol probing:** `tools/probe.sh <ip> <user> <pass>`.
- **Syntax check** (HA not installed): `python3 -m py_compile custom_components/axis_pacs/**/*.py`.
- **HA tests** (when set up): `pytest` with `pytest-homeassistant-custom-component`.

## Conventions / gotchas

- Door commands (lock/unlock/access) require an **Admin** account.
- Parse SOAP by **namespace URI**, not prefix — prefixes vary across responses.
- PullPoint filter expressions use `onvif:`/`axis:` prefixes; notifications come back as `tns1:`/`tnsaxis:`.
- A1001-only WSDLs (`connection_axis`, `thirdpartycredential`) are deliberately unused.
- Newer A1601/A1610 firmware adds JSON door control + the `ws-data-stream` websocket; gate transport by capability and add those behind detection rather than replacing the SOAP/PullPoint path.
