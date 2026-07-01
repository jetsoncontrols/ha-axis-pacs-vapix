"""The Axis PACS (VAPIX) door-controller integration."""

from __future__ import annotations

import hashlib
import logging
from contextlib import suppress
from pathlib import Path

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_MANAGE_ALLOW_NON_ADMIN,
    CONF_MANAGE_CODES,
    CONF_USE_HTTPS,
    DATA_EXPIRE_ACTIONS,
    DATA_EXPIRE_ACTIONS_STORE,
    DATA_LAST_USED,
    DATA_LAST_USED_STORE,
    DEFAULT_MANAGE_ALLOW_NON_ADMIN,
    DEFAULT_MANAGE_CODES,
    DOMAIN,
    EXPIRE_ACTIONS_STORAGE_KEY,
    EXPIRE_ACTIONS_STORAGE_VERSION,
    FRONTEND_CARD_FILENAME,
    FRONTEND_DIR,
    FRONTEND_URL_BASE,
    LAST_USED_STORAGE_KEY,
    LAST_USED_STORAGE_VERSION,
    PLATFORMS,
    WS_TYPE_MANAGERS,
)
from .coordinator import AxisPacsConfigEntry, AxisPacsCoordinator
from .services import async_setup_services
from .vapix import AxisPacsClient, CannotConnect, InvalidAuth, VapixError

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register access-code services, the WS discovery command, and the card."""
    async_setup_services(hass)
    websocket_api.async_register_command(hass, _ws_list_managers)
    await _async_register_frontend(hass)
    await _async_load_last_used(hass)
    await _async_load_expire_actions(hass)
    return True


async def _async_load_last_used(hass: HomeAssistant) -> None:
    """Load the persisted per-credential "last used" map into ``hass.data``.

    Domain-level (shared by every controller's event stream), so the manager's
    ``list_credentials`` sees usage recorded at doors owned by any controller.
    """
    store: Store[dict] = Store(hass, LAST_USED_STORAGE_VERSION, LAST_USED_STORAGE_KEY)
    data = await store.async_load() or {}
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[DATA_LAST_USED] = data
    domain_data[DATA_LAST_USED_STORE] = store


async def _async_load_expire_actions(hass: HomeAssistant) -> None:
    """Load the persisted per-credential expiry-action map (the reaper's opt-in
    set) into ``hass.data``. Domain-level, like "last used"."""
    store: Store[dict] = Store(
        hass, EXPIRE_ACTIONS_STORAGE_VERSION, EXPIRE_ACTIONS_STORAGE_KEY
    )
    data = await store.async_load() or {}
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[DATA_EXPIRE_ACTIONS] = data
    domain_data[DATA_EXPIRE_ACTIONS_STORE] = store


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the bundled management card and auto-load it (extra JS + resource).

    Ships the custom Lovelace card inside the integration (the full browser_mod
    pattern), so installing the integration is enough — no HACS frontend entry
    and no www/ drop. The Lovelace resource is auto-managed (not hand-added).
    Loading is idempotent across reloads.
    """
    frontend_dir = Path(__file__).parent / FRONTEND_DIR
    # cache_headers=True is REQUIRED, not cosmetic. With it False the card JS is
    # served WITHOUT Cache-Control, and Safari over the Nabu Casa tunnel stalls /
    # deprioritizes the non-cacheable module request on page load — the module
    # never executes, the element never registers, and the card shows a permanent
    # "Configuration error" until a full page reload. Every card that loads
    # reliably (HACS /hacsfiles/*, browser_mod /browser_mod.js) is served WITH
    # cache headers; this one was the lone exception. The ?v=<hash> query still
    # busts the cache on updates, so True is safe. (Matches browser_mod.)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL_BASE, str(frontend_dir), True)]
    )
    # Cache-bust with a content hash so a card update is always a fresh URL for
    # the BROWSER's HTTP cache. (HA's service worker does NOT cache this path, so
    # this is not an SW concern.) The static handler ignores the query string and
    # serves the current file; _async_register_card_resource rewrites the resource
    # entry's ?v= to this hash on each setup.
    version = await hass.async_add_executor_job(
        _card_version, frontend_dir / FRONTEND_CARD_FILENAME
    )
    card_url = f"{FRONTEND_URL_BASE}/{FRONTEND_CARD_FILENAME}?v={version}"
    add_extra_js_url(hass, card_url)
    # ...and ALSO register it as a Lovelace resource. add_extra_js_url alone is a
    # fire-and-forget dynamic import() in the app shell that races the dashboard
    # render, so on a cold load the card can paint "Configuration error" until a
    # manual refresh. Lovelace resources are loaded by the panel BEFORE it builds
    # cards, which is how every other custom card here loads reliably. Same URL as
    # add_extra_js_url so the browser dedupes to a single module load. This is the
    # full browser_mod pattern (which does both); we previously only did the first
    # half. Best-effort — never fail setup over the resource.
    await _async_register_card_resource(hass, card_url)


async def _async_register_card_resource(hass: HomeAssistant, card_url: str) -> None:
    """Register (or update) the management card as a storage-mode Lovelace resource.

    Mirrors browser_mod's ``mod_view`` approach so it tracks HA's internal
    resource API. Matched by URL prefix (ignoring the ``?v=`` cache-bust) so a
    card update just rewrites the existing entry's hash instead of piling up
    duplicates. No-op in YAML lovelace mode (resources are user-managed there;
    ``add_extra_js_url`` still delivers the card).
    """
    base_url = f"{FRONTEND_URL_BASE}/{FRONTEND_CARD_FILENAME}"
    try:
        lovelace = hass.data.get("lovelace")
        resources = getattr(lovelace, "resources", None) if lovelace else None
        if resources is None or not isinstance(resources, ResourceStorageCollection):
            return  # YAML mode / not ready — add_extra_js_url covers delivery
        if not resources.loaded:
            await resources.async_load()
            resources.loaded = True
        for item in resources.async_items():
            if item["url"].split("?", 1)[0] == base_url:
                if item["url"] != card_url:
                    await resources.async_update_item(
                        item["id"], {"res_type": "module", "url": card_url}
                    )
                return
        await resources.async_create_item({"res_type": "module", "url": card_url})
    except Exception:  # noqa: BLE001 — resource wiring must never break setup
        _LOGGER.debug("Could not register card as a Lovelace resource", exc_info=True)


def _card_version(path: Path) -> str:
    """Short content hash of the card file, for cache-busting its module URL."""
    try:
        return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()[:8]
    except OSError:
        return "0"


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_MANAGERS})
@callback
def _ws_list_managers(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return the loaded controllers that expose the cluster-wide code services.

    Lets the management card self-discover its controller (so no opaque config
    entry id is baked into the dashboard). Discovery is open to any logged-in
    user — the actual code operations are admin-gated server-side unless the
    controller opts into ``manage_allow_non_admin`` (reported here so the card
    can show controls to the right users). Only ``manage_codes`` entries listed.
    """
    managers = [
        {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "host": entry.data.get(CONF_HOST, ""),
            "allow_non_admin": entry.options.get(
                CONF_MANAGE_ALLOW_NON_ADMIN, DEFAULT_MANAGE_ALLOW_NON_ADMIN
            ),
        }
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and entry.options.get(CONF_MANAGE_CODES, DEFAULT_MANAGE_CODES)
    ]
    connection.send_result(msg["id"], {"managers": managers})


async def async_setup_entry(hass: HomeAssistant, entry: AxisPacsConfigEntry) -> bool:
    """Set up Axis PACS from a config entry."""
    data = entry.data
    client = AxisPacsClient(
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        port=data.get(CONF_PORT, 0),
        use_https=data.get(CONF_USE_HTTPS, False),
    )

    try:
        identity = await client.async_get_identity()
        doors = await client.async_get_local_doors(identity.serial)
    except InvalidAuth as err:
        await client.async_close()
        # Reauth flow is deferred; surface as not-ready so the entry retries.
        raise ConfigEntryNotReady(f"Authentication failed: {err}") from err
    except (CannotConnect, VapixError) as err:
        await client.async_close()
        raise ConfigEntryNotReady(str(err)) from err

    if not doors:
        _LOGGER.warning(
            "Controller %s reported no local doors; no lock entities will be created",
            identity.serial,
        )

    # Map access points (readers) to door names so the "last used" tracker can
    # label which door a credential was used at. CLUSTER-WIDE (all doors, not
    # just this controller's): access events arrive cluster-wide via global event
    # distribution, and the backfill log covers every door. Best-effort — a
    # missing map just means an event stores the raw reader token.
    ap_to_door: dict[str, str] = {}
    with suppress(VapixError):
        all_doors = {d.token: d for d in await client.async_get_door_info_list()}
        for ap in await client.async_get_access_points():
            door = all_doors.get(ap.door_token)
            if door is not None:
                ap_to_door[ap.token] = door.name

    coordinator = AxisPacsCoordinator(hass, entry, client, identity, doors, ap_to_door)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.async_close()
        raise

    entry.runtime_data = coordinator
    coordinator.start_event_listener()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AxisPacsConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok
