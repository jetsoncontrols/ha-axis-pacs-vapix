"""The Axis PACS (VAPIX) door-controller integration."""

from __future__ import annotations

import hashlib
import logging
from contextlib import suppress
from pathlib import Path

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.frontend import add_extra_js_url
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
    """Deploy the bundled management card to ``www/`` and load it from ``/local/``.

    The card MUST be served from a path Home Assistant's service worker caches
    (``api | static | auth | frontend_latest | frontend_es5 | local``). A custom
    integration static path such as ``/axis_pacs/frontend`` is NOT SW-cached, so
    Safari over a Nabu Casa tunnel stalls the module request on page load: the
    element never registers via ``customElements.define`` and the card shows a
    permanent "Configuration error" until a full reload (Chrome tolerated it).
    Serving from ``www`` (``/local/``, which IS SW-cached) loads reliably in
    every browser — verified on SI2 (3/3 Safari + Chrome).

    So we copy the bundled card into ``www/<domain>/`` on every setup — a HACS
    update thus propagates automatically, no manual www management — and load it
    from ``/local/``. Earlier ``/axis_pacs/`` approaches (the add_extra_js_url
    race fix, the Lovelace-resource registration, and ``cache_headers=True``)
    were red herrings; ``cache_headers=True`` was in fact harmful — its long
    ``max-age`` let a stale/bad response stick in-cache. The SW-cache path is the
    real fix. See CLAUDE.md.
    """
    src = Path(__file__).parent / FRONTEND_DIR / FRONTEND_CARD_FILENAME
    dest_dir = Path(hass.config.path("www")) / DOMAIN
    version = await hass.async_add_executor_job(_deploy_card, src, dest_dir)
    base_url = f"/local/{DOMAIN}/{FRONTEND_CARD_FILENAME}"
    card_url = f"{base_url}?v={version}"
    # Load it two ways for robustness: add_extra_js_url (works on any lovelace
    # mode) + an auto-managed Lovelace resource (reconciled below). Same URL, so
    # the browser dedupes to a single module load.
    add_extra_js_url(hass, card_url)
    await _async_register_card_resource(hass, card_url, base_url)


def _deploy_card(src: Path, dest_dir: Path) -> str:
    """Copy the card into ``www/<domain>/`` (idempotent) and return its content
    hash for cache-busting. Runs in an executor — blocking file I/O."""
    try:
        data = src.read_bytes()
    except OSError:
        return "0"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    # Only rewrite when the bytes changed, to avoid needless disk/mtime churn.
    if not dest.exists() or dest.read_bytes() != data:
        dest.write_bytes(data)
    return hashlib.md5(data, usedforsecurity=False).hexdigest()[:8]


async def _async_register_card_resource(
    hass: HomeAssistant, card_url: str, base_url: str
) -> None:
    """Register (or update) the management card as a storage-mode Lovelace resource.

    Mirrors browser_mod's ``mod_view`` approach so it tracks HA's internal
    resource API. Matched by URL prefix (ignoring the ``?v=`` cache-bust) so a
    card update just rewrites the existing entry's hash instead of piling up
    duplicates. No-op in YAML lovelace mode (resources are user-managed there;
    ``add_extra_js_url`` still delivers the card).
    """
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
