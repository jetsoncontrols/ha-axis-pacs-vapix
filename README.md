# ha-axis-pacs-vapix

Home Assistant integration for Axis VAPIX access controllers (A1001, A1601,
A1610/A1210 on the VAPIX-OS track).

## What it does

### Door locks (v0.1)

Exposes each door **local to the connected controller** as a Home Assistant
`lock` entity, with **live** state driven by the controller's event stream (no
polling):

- **State** from the ONVIF `DoorMode` (`Locked`/`Blocked`/`DoubleLocked`/`LockedDown` →
  locked; `Unlocked`/`LockedOpen`/`Accessed` → unlocked).
- **Lock** → `LockDoor`, **Unlock** → `UnlockDoor` (permanent), **Open/Unlatch** →
  `AccessDoor` (momentary buzz-in, exposed via the lock *open* feature).
- **Live updates** via ONVIF WS-Eventing **PullPoint** over `/vapix/services`.

One integration instance per physically-connected controller. When controllers
are clustered, only the **local** controller's doors are shown — peer doors
(identified by the token's MAC vs. the device serial) are filtered out.

### Access-code management card (v0.3+)

A bundled Lovelace card (`custom:axis-pacs-codes-card`) — auto-registered by the
integration, no separate HACS frontend entry or `www/` drop — manages the
**cluster-wide** cardholder / PIN / card database that the controllers share:

- List, add, edit, enable/disable and delete cardholders and their PIN/card
  credentials, with a generate-a-unique-code button.
- Assign access by **groups** (ONVIF access profiles) and/or **individual
  doors** (find-or-created one-door profiles, with a schedule).
- **Last-used** column, seeded from the controller's event log and kept live.
- Enable the card on one controller via its **Configure → Manage access codes**
  option; a per-controller **Allow non-admins** toggle opens it to logged-in
  non-admins (e.g. property managers) while keeping them off HA admin.

### Validity windows + expiry (v0.4)

Per-credential **start / end dates** (the controller enforces the window itself,
**date only**), plus a daily reaper that, once the end date passes, either
**disables** the credential (keeps the code reserved so it isn't re-issued) or
**deletes** it — your choice per credential.

## Requirements

- A controller on the VAPIX-OS firmware track that exposes the SOAP door-control
  API at `POST /vapix/services` (validated on A1001 firmware 1.65.6).
- An **Admin** account (lock/unlock/access actions require it).
- Home Assistant 2024.12 or newer. No third-party Python dependencies.

## Installation (HACS)

Add this repository as a custom integration repository in HACS, install it,
restart Home Assistant, then add **Axis PACS Door Controllers** from
*Settings → Devices & Services* and enter the host + Admin credentials.

Controllers on the same network are **auto-discovered** (DHCP by Axis MAC OUI,
and zeroconf where advertised) — you'll be prompted only for credentials.

## Development

`scripts/devcheck.py` exercises the VAPIX client directly against a controller
(no Home Assistant needed) for fast iteration:

```sh
pip install httpx
AXIS_HOST=10.1.4.12 AXIS_USER=root AXIS_PASS=secret python3 scripts/devcheck.py
# add --open to momentarily AccessDoor the first local door and confirm eventing
```

`tools/probe.sh` performs raw read-only VAPIX probes with `curl` for protocol
debugging.

## Architecture

- `custom_components/axis_pacs/vapix/` — self-contained async VAPIX client
  (httpx + digest, SOAP/ONVIF, stdlib XML). Home Assistant independent.
- `coordinator.py` — push coordinator; seeds door state then applies live
  `DoorMode` events from the PullPoint stream.
- `lock.py` — one `LockEntity` per local door.

Not yet implemented: door/alarm/tamper binary sensors, access-granted/denied
event entities, newer-firmware JSON + websocket transports for A1601/A1610,
and reauthentication. A1001 clustering is intentionally not used.

## License

Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE) —
free for any noncommercial purpose; **commercial use is not permitted**.
