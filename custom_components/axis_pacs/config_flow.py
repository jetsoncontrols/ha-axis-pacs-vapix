"""Config flow for the Axis PACS integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import CONF_USE_HTTPS, DEFAULT_PORT, DEFAULT_USERNAME, DOMAIN
from .vapix import AxisPacsClient, CannotConnect, InvalidAuth, VapixError

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_USE_HTTPS, default=False): bool,
    }
)

# Credentials only — the host is already known from discovery.
STEP_DISCOVERY_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_USE_HTTPS, default=False): bool,
    }
)


def _serial_from_mac(mac: str) -> str:
    """Axis serial == MAC, uppercase without separators."""
    return mac.replace(":", "").replace("-", "").upper()


async def _validate(data: dict[str, Any]) -> tuple[str, str]:
    """Connect to the controller and return ``(serial, display_name)``."""
    client = AxisPacsClient(
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        port=data.get(CONF_PORT, 0),
        use_https=data.get(CONF_USE_HTTPS, False),
    )
    try:
        identity = await client.async_get_identity()
    finally:
        await client.async_close()
    display = (
        identity.product_short_name
        or identity.product_full_name
        or identity.model
        or "Axis controller"
    )
    return identity.serial, display


class AxisPacsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Axis PACS."""

    VERSION = 1

    _discovered_host: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                serial, display = await _validate(user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except VapixError:
                errors["base"] = "unknown"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating Axis controller")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{display} ({user_input[CONF_HOST]})", data=user_input
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    # --- Discovery (DHCP + zeroconf) --------------------------------------- #
    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        return await self._async_start_discovery(
            discovery_info.ip, _serial_from_mac(discovery_info.macaddress)
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        mac = discovery_info.properties.get("macaddress")
        serial = _serial_from_mac(mac) if mac else None
        return await self._async_start_discovery(
            str(discovery_info.ip_address), serial
        )

    async def _async_start_discovery(
        self, host: str, serial: str | None
    ) -> ConfigFlowResult:
        """Record the discovered host and ask for credentials."""
        if serial:
            await self.async_set_unique_id(serial)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self._discovered_host = host
        self.context["title_placeholders"] = {"name": host}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovered_host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {CONF_HOST: self._discovered_host, **user_input}
            try:
                serial, display = await _validate(data)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except VapixError:
                errors["base"] = "unknown"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating Axis controller")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: self._discovered_host}
                )
                return self.async_create_entry(
                    title=f"{display} ({self._discovered_host})", data=data
                )
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=STEP_DISCOVERY_DATA_SCHEMA,
            description_placeholders={"host": self._discovered_host},
            errors=errors,
        )
