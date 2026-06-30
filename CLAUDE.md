# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

HACS-compatible Home Assistant custom integration for Axis VAPIX access controllers (A1001, A1601, A1610/A1210 on the VAPIX-OS firmware track). Integration domain: `axis_pacs`. One config entry per physically-connected controller. Two capabilities:

- **Door control (v0.1):** each door **local to the connected controller** is a `lock` entity, with live state from the controller's event stream (no polling).
- **Access-code management (v0.2):** create/list/remove PIN & card credentials via integration services, opted-in per controller (see below).
- **Management card + extended services (v0.3):** a bundled Lovelace card (`custom:axis-pacs-codes-card`) auto-registered by the integration, backed by a richer service surface (groups + individual-door grants, last-used tracking, code generation, per-controller admin policy). See **"v0.3 — Management card, services, last-used, permissions"** at the bottom.
- **Validity windows + expiry reaper (v0.4):** per-credential start/end **dates** (the controller enforces the window natively via the ONVIF `tcr` service — date only) plus a daily HA "reaper" that disables or deletes a credential once its end date passes. See **"v0.4 — Validity windows + expiry reaper"** at the bottom.

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

## v0.3 — Management card, services, last-used, permissions

### Bundled Lovelace card (`frontend/axis-pacs-codes-card.js`)
- Plain vanilla custom element (no Lit/build step), shadow DOM, single static ES module. Renders the cluster credential DB as an editable table + add/edit forms, calling the `axis_pacs.*` services over the websocket (`hass.connection.sendMessagePromise({type:'call_service', …, return_response:true})`). There are NO entities behind the data — service-response only — which is *why* a bespoke card is required.
- **Auto-registration** (`__init__.async_setup` → `_async_register_frontend`): `hass.http.async_register_static_paths([StaticPathConfig('/axis_pacs/frontend', <pkg>/frontend, False)])` + `frontend.add_extra_js_url(hass, '/axis_pacs/frontend/axis-pacs-codes-card.js?v=<hash>')`. The browser_mod pattern — installing the integration is enough; no Lovelace resource / no `www/` drop. `manifest.json` deps: `http, frontend, websocket_api`.
- **Cache-bust is mandatory:** the URL carries `?v=<md5 of the file>` (`_card_version`). Without it, redeploying the JS to the same URL leaves the frontend **service worker serving a stale/partial module** → an **intermittent "Configuration error"** on the card that flickers across refreshes (it is NOT an expander-card problem — a bare card hits it too; don't chase the dashboard config). A restart recomputes the hash → new URL → fresh fetch.
- **Self-discovery:** the card resolves its controller via the `axis_pacs/managers` WS command (no `config_entry_id` baked into the dashboard). That command returns `{entry_id, title, host, allow_non_admin}` for every loaded `manage_codes` entry.
- Codes mask to `••••`; `_canManage` (see Permissions) gates Add/Edit/Delete/toggle/reveal; card config: `title`, `entry_id` (optional pin), `allow_reveal:false` (kiosks).

### Permissions (server-side + UI, per-controller policy)
- Services are gated in the async `_client()` resolver: **admin required by default**, raising `Unauthorized` when `call.context.user_id` is a non-admin. Calls with **no user** (automations/scripts/internal) always pass.
- Per-controller options-flow toggle **`manage_allow_non_admin`** (default false): when on, ANY logged-in user may manage codes (for non-admin property managers) — the practical gate then becomes the dashboard section's `visibility`. The gate reads `entry.options` **live** (no reload). The card mirrors it: `_canManage = is_admin || manager.allow_non_admin`, and the `axis_pacs/managers` WS command is **open** (no `@require_admin`) so non-admins can discover when allowed. Trade-off when on: HA can't enforce "who sees the card" server-side, so any authenticated user could call the services — use a user-id allowlist if tighter control is needed.

### Service surface (cluster-wide; route via the `manage_codes` controller's `config_entry_id`)
`list_credentials` (returns `user_name`/`kind`/`has_pin`/`has_card`/`last_used`/`last_used_door` + `valid_from`/`valid_to` (date only) + `expire_action`; `pin`/`card` only when `include_pins:true`) · `list_access_profiles` (`doors:[{token,name}]` + per-policy `policies:[{door,schedule}]` + `schedules:[names]` + `system` flag) · `list_users` · `list_doors` · `list_schedules` · `add_credential` (`kind` pin|card + `code` → `{"PIN":…}`/`{"CardNr":…}`; optional `valid_from`/`valid_to`/`expire_action`) · `add_pin` (legacy, delegates) · `generate_code` · `set_user` (rename) · `remove_user` · `set_credential_enabled` · `set_credential_access_profiles` (get-then-set — preserves id_data AND enabled/Status, so editing doors never re-activates a disabled credential) · `set_credential_code` (get-then-set id_data — change PIN/card value or type) · `set_credential_validity` (set/clear start+end dates + record the reaper's `expire_action`) · `ensure_door_profile` · `remove_credential`.

### Permission model (groups + individual doors)
- Entry Manager "groups" == ONVIF **access profiles** (`tar`): a bundle of `(schedule + door)` policies. A credential holds a *list* of profile tokens (union). Card treats a profile with exactly **one door** as an individual-door grant, anything else (incl. 0-door like "Guests") as a group; the `system` flag (name contains `rexenabler`) hides the internal REX profile.
- `ensure_door_profile(door, schedule=standard_always)` find-or-creates a one-door profile named `"<Door> (<Schedule>)"`, reusing one whose policy set exactly matches (so per-door grants don't proliferate).
- **`CreateAccessProfile`/`DeleteAccessProfile` (ONVIF TAR) schema gotcha:** the `<AccessProfile>` needs the `token=""` attribute (else `ter:MissingAttr`) AND a `<Description>` element in order `Name, Description, AccessPolicy*` (else `ter:InvalidArgs` occurrence violation), and AccessPolicy carries **no `EntityType`** (else `ter:TagMismatch`). `Entity` = the door's access-point token.

### Last-used tracking
- **Live (forward):** the unfiltered PullPoint stream already delivers `AccessControl/AccessGranted/Credential` + `AccessTaken/Credential` events, which carry `CredentialToken`/`CredentialHolderName`/`AccessPointToken` + UTC time. The coordinator records `last_used` into a shared, persisted domain store (`hass.data[DOMAIN]['last_used']` + a `Store`), written by EVERY controller's coordinator (each only sees its own doors' events) and read back by the manager's `list_credentials`. Door names resolve via a cluster-wide access-point→door map.
- **Historical backfill:** use the **JSON EventLogger API — `POST /vapix/eventlogger` body `{"FetchEvents3": {"Limit": N, "Descending": true}}`** — NOT the SOAP `FetchEvents` op (SOAP only ever returns the OLDEST events, which makes the log look frozen — it is not; the log holds 26k+ events). `Descending:true` is the ONLY way to reach recent activity. The API exposes **only `Limit` + `Descending`** — no topic/time/credential/pagination filter (the Entry Manager web UI's per-user filter is undocumented; the API returns descriptive `"Unhandled field"` JSON errors, which is how it was reverse-engineered). Caps ~1000 events/response. One controller's log is cluster-wide via **global event distribution** (`GlobalDistributionEnabled`). Backfill samples the newest AND oldest windows, keeps the latest access per credential (best-effort; live events always win).

### Device facts (validated empirically, A1001 fw 1.65.x cluster at 10.15.4.x)
- `GetCredentialList` returns `IdData` inline (not summary-only). Cards store the number in **`CardNr`** (5-digit at this site; the `Card`/`PIN` keys sit empty); PINs in `PIN`. **No device-configured code length** (`pacsaxis:GetStandardAttributeList`/`GetVendorAttributeList` empty, nothing in `param.cgi`) — `generate_code` derives length from existing codes (default 5), uses Python `secrets`, ensures uniqueness vs all codes, and avoids a leading zero.
- Schedules: `standard_always` (24/7), `standard_office_hours`, `standard_weekends`, `standard_after_hours`.
- `ONVIF GetServices` advertises (besides door control) `EventLogger`/`EventLoggerConfig`, `entry`, `event1`, `action1`, `IdPoint`, `pacs`, `user`, `accessrules`, `schedule`, `accesscontrol`. The `pacsaxis` JSON help page is `GET /vapix/pacs`; the EventLogger help page is `GET /vapix/eventlogger` (`axlog:FetchEvents/2/3`).
- **Credential validity:** `tcr:GetServiceCapabilities` reports `CredentialValiditySupported="true"`, `CredentialAccessProfileValiditySupported="false"`, `ValiditySupportsTimeValue="false"` (**date only**), `MaxCredentials=50000`, `MaxAccessProfilesPerCredential=20`. The Axis-native `pacsaxis` Credential schema has NO validity fields (only `UserToken/Description/Enabled/Status/IdData/CredentialAccessProfile`); the **ONVIF `tcr` service exposes the same credential DB** (identical tokens) WITH `ValidFrom`/`ValidTo`. **No native expired-credential purge** (`pacsaxis:GetAccessControllerConfiguration` carries only `DeviceUUID`), so the disable/delete end-action must be done by HA. The reaper-vs-ACAP analysis: the only on-device actor that could disable/delete is a heavyweight signed ACAP — disproportionate, and the A1001s are compute-constrained — so HA does it.

## v0.4 — Validity windows + expiry reaper

Per-credential **start/end dates** (the controller enforces the window itself; **date only**) plus a daily HA **reaper** for the disable/delete-after-expiry action (no on-device mechanism for that — see the validity device-fact above).

### Setting/reading the window — via the ONVIF `tcr` view, NOT `pacsaxis`
- `pacsaxis` has no validity fields, so validity rides the **ONVIF `tcr` service** on the *same* credential (`soap.TCR`/`PT` namespaces). `ValidFrom`/`ValidTo` are **credential-level** (`CredentialAccessProfileValiditySupported=false`, so not per-profile).
- **Set** = `tcr:ModifyCredential` which needs the WHOLE record → `client.async_set_credential_validity(token, vf, vt)` does **get-then-modify**: `tcr:GetCredentials(token)` → splice `ValidFrom`/`ValidTo` → `ModifyCredential`, echoing the `CredentialIdentifier`s (PIN/card) and `CredentialAccessProfile`s verbatim (`soap.modify_tcr_credential`). `tcr:ModifyCredential` has **no `Enabled` element** (enable state is separate via `Enable/DisableCredential`), so setting validity **cannot re-activate** a disabled credential. Element order: `Description, CredentialHolderReference, ValidFrom?, ValidTo?, CredentialIdentifier*, CredentialAccessProfile*`; explicit `tcr:`/`pt:` prefixes so the `pt:PIN`/`pt:Card` identifier-type QNames resolve.
- **Read** = `client.async_list_credential_validity()` → one `tcr:GetCredentialList` pass → `{token: (valid_from, valid_to)}`, merged into `list_credentials` (date portion only). `models.TcrCredential` holds the parsed tcr view.
- **Date encoding** (`services._to_device_datetime`, the verified seam): start → `<date>T00:00:00Z`, end → `<date>T23:59:59Z`; time is cosmetic (date-only honoured) so the **end date is the LAST valid day (inclusive)**, matched by the reaper's `today > end_date` test. If a device ever turns out to treat `ValidTo` exclusively, bump that helper AND the reaper together. (Either interpretation is fail-safe — never over-grants.)

### Expiry reaper (`coordinator.async_run_expiry_reaper`)
- Runs only on the **`manage_codes` controller** (it owns the cluster-wide map). Native `ValidTo` already **denies** an expired credential on its own — the reaper just adds the disable/delete that has no on-device path.
- **Opt-in set:** the reaper acts ONLY on credentials with a recorded **`expire_action`** (a persisted domain `Store`, `DATA_EXPIRE_ACTIONS`, token→`disable`|`delete`, written by `add_credential`/`set_credential_validity`). It never touches a credential whose `ValidFrom`/`ValidTo` was set out-of-band (e.g. the two real `valid_from`-only credentials at SI2). Worst-case store loss = falls back to the safe default and the native window still denies.
- For each recorded token: drop the entry if the credential is gone; skip if `ValidTo` cleared or `today <= end_date`; else **delete** (`remove_credential` + orphaned-cardholder `remove_user`, pop the entry) or **disable** (`set_credential_enabled(False)` if still enabled — idempotent, entry kept so the code stays reserved). **Default disable** because an expired-by-`ValidTo` credential is already denied AND still in the DB, so `generate_code`'s uniqueness scan keeps avoiding its code (never re-issued); delete frees the code but declutters.
- **Schedule:** `async_track_time_change` daily at `REAPER_HOUR:REAPER_MINUTE` (03:17 local) + an `async_call_later` ~3 min after setup so a restart never skips a day. Unsubs cleaned in `async_shutdown`. Date-granularity → daily cadence is sufficient.

### Card (add/edit) wiring
- `_renderValidity(prefix, cred)` adds Start/End `<input type="date">` + a "When expired" select (Disable default / Delete); `_readValidity` reads them synchronously before the re-render. `_validityBadge` shows a **Starts / Expires / Expired** pill in the Name cell. Add passes the three fields into `add_credential`; edit calls `set_credential_validity` only when they changed.

### Verified end-to-end (SI2, 2026-06-30)
`add_credential` with a window + `set_credential_validity` set/clear round-tripped through `list_credentials` (date-only `valid_from`/`valid_to` + `expire_action`), the `expire_actions` Store persisted then cleared to `{}`, and no SOAP faults across repeated `ModifyCredential` calls. Reaper building blocks all exercised; it ran clean at startup against an empty store.
