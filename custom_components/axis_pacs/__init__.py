"""The Axis PACS (VAPIX) door-controller integration."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.typing import ConfigType

from .const import CONF_USE_HTTPS, PLATFORMS
from .coordinator import AxisPacsConfigEntry, AxisPacsCoordinator
from .services import async_setup_services
from .vapix import AxisPacsClient, CannotConnect, InvalidAuth, VapixError

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's (cluster-wide) access-code services."""
    async_setup_services(hass)
    return True


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

    coordinator = AxisPacsCoordinator(hass, entry, client, identity, doors)
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
