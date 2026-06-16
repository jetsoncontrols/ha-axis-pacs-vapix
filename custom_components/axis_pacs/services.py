"""Integration-level services for managing Axis access codes (PINs/cards).

On Axis controllers the credential / cardholder / access-profile database is
replicated **cluster-wide**, so access codes are not local to any one door or
controller. These are therefore domain services (not per-door entity services):
each call routes through one configured controller via ``config_entry_id`` but
reads or mutates the shared cluster database that every controller in the
cluster sees.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_ACCESS_PROFILE_TOKENS,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CREDENTIAL_TOKEN,
    ATTR_ENABLED,
    ATTR_INCLUDE_PINS,
    ATTR_NAME,
    ATTR_PIN,
    CONF_MANAGE_CODES,
    DEFAULT_MANAGE_CODES,
    DOMAIN,
    SERVICE_ADD_PIN,
    SERVICE_LIST_ACCESS_PROFILES,
    SERVICE_LIST_CREDENTIALS,
    SERVICE_REMOVE_CREDENTIAL,
    SERVICE_SET_CREDENTIAL_ENABLED,
)
from .coordinator import AxisPacsCoordinator
from .vapix import AxisPacsClient, VapixError

_LOGGER = logging.getLogger(__name__)

_ENTRY_FIELD = {vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string}

ADD_PIN_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_NAME): cv.string,
        vol.Required(ATTR_PIN): cv.string,
        vol.Optional(ATTR_ACCESS_PROFILE_TOKENS, default=list): vol.All(
            cv.ensure_list, [cv.string]
        ),
        vol.Optional(ATTR_ENABLED, default=True): cv.boolean,
    }
)
CREDENTIAL_SCHEMA = vol.Schema(
    {**_ENTRY_FIELD, vol.Required(ATTR_CREDENTIAL_TOKEN): cv.string}
)
SET_ENABLED_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_CREDENTIAL_TOKEN): cv.string,
        vol.Required(ATTR_ENABLED): cv.boolean,
    }
)
LIST_CREDENTIALS_SCHEMA = vol.Schema(
    {**_ENTRY_FIELD, vol.Optional(ATTR_INCLUDE_PINS, default=False): cv.boolean}
)
LIST_PROFILES_SCHEMA = vol.Schema(_ENTRY_FIELD)


def _client(hass: HomeAssistant, call: ServiceCall) -> AxisPacsClient:
    """Resolve the controller addressed by ``config_entry_id`` to its client."""
    entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            f"No Axis PACS controller found for config entry {entry_id!r}"
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            f"Axis PACS controller {entry.title!r} is not loaded"
        )
    if not entry.options.get(CONF_MANAGE_CODES, DEFAULT_MANAGE_CODES):
        raise ServiceValidationError(
            f"Access-code management is turned off for {entry.title!r}. Enable it in "
            "Settings → Devices & Services → Axis PACS → Configure on the controller "
            "you want to manage codes from."
        )
    coordinator: AxisPacsCoordinator = entry.runtime_data
    return coordinator.client


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the access-code services once for the integration."""
    if hass.services.has_service(DOMAIN, SERVICE_ADD_PIN):
        return

    async def add_pin(call: ServiceCall) -> ServiceResponse:
        client = _client(hass, call)
        try:
            user_token, credential_token = await client.async_add_pin(
                name=call.data[ATTR_NAME],
                pin=call.data[ATTR_PIN],
                access_profile_tokens=call.data[ATTR_ACCESS_PROFILE_TOKENS],
                enabled=call.data[ATTR_ENABLED],
            )
        except VapixError as err:
            raise HomeAssistantError(f"Failed to add PIN: {err}") from err
        return {"user_token": user_token, "credential_token": credential_token}

    async def remove_credential(call: ServiceCall) -> None:
        client = _client(hass, call)
        try:
            await client.async_remove_credential(call.data[ATTR_CREDENTIAL_TOKEN])
        except VapixError as err:
            raise HomeAssistantError(f"Failed to remove credential: {err}") from err

    async def set_credential_enabled(call: ServiceCall) -> None:
        client = _client(hass, call)
        try:
            await client.async_set_credential_enabled(
                call.data[ATTR_CREDENTIAL_TOKEN], call.data[ATTR_ENABLED]
            )
        except VapixError as err:
            raise HomeAssistantError(f"Failed to change credential: {err}") from err

    async def list_credentials(call: ServiceCall) -> ServiceResponse:
        client = _client(hass, call)
        include_pins = call.data[ATTR_INCLUDE_PINS]
        try:
            creds = await client.async_get_credentials()
        except VapixError as err:
            raise HomeAssistantError(f"Failed to list credentials: {err}") from err
        return {
            "credentials": [
                {
                    "token": c.token,
                    "user_token": c.user_token,
                    "description": c.description,
                    "enabled": c.enabled,
                    "has_pin": c.has_pin,
                    "access_profile_tokens": c.access_profile_tokens,
                    **({"pin": c.pin} if include_pins else {}),
                }
                for c in creds
            ]
        }

    async def list_access_profiles(call: ServiceCall) -> ServiceResponse:
        client = _client(hass, call)
        try:
            profiles = await client.async_get_access_profiles()
        except VapixError as err:
            raise HomeAssistantError(f"Failed to list access profiles: {err}") from err
        return {
            "access_profiles": [
                {
                    "token": p.token,
                    "name": p.name,
                    "access_point_tokens": p.entity_tokens,
                }
                for p in profiles
            ]
        }

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_PIN, add_pin,
        schema=ADD_PIN_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_CREDENTIAL, remove_credential, schema=CREDENTIAL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CREDENTIAL_ENABLED, set_credential_enabled,
        schema=SET_ENABLED_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_CREDENTIALS, list_credentials,
        schema=LIST_CREDENTIALS_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_ACCESS_PROFILES, list_access_profiles,
        schema=LIST_PROFILES_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
