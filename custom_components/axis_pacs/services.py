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
import secrets
from collections import Counter
from datetime import date

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_ACCESS_PROFILE_TOKENS,
    ATTR_CODE,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CREDENTIAL_TOKEN,
    ATTR_DESCRIPTION,
    DATA_EXPIRE_ACTIONS,
    DATA_EXPIRE_ACTIONS_STORE,
    DATA_LAST_USED,
    ATTR_DOOR_TOKEN,
    ATTR_ENABLED,
    ATTR_EXPIRE_ACTION,
    ATTR_FIRST_NAME,
    ATTR_INCLUDE_PINS,
    ATTR_KIND,
    ATTR_LAST_NAME,
    ATTR_LENGTH,
    ATTR_NAME,
    ATTR_PIN,
    ATTR_SCHEDULE_TOKEN,
    ATTR_USER_TOKEN,
    ATTR_VALID_FROM,
    ATTR_VALID_TO,
    ALWAYS_SCHEDULE_TOKEN,
    CONF_MANAGE_ALLOW_NON_ADMIN,
    CONF_MANAGE_CODES,
    CREDENTIAL_KIND_ID_KEY,
    DEFAULT_CODE_LENGTH,
    DEFAULT_EXPIRE_ACTION,
    DEFAULT_MANAGE_ALLOW_NON_ADMIN,
    DEFAULT_MANAGE_CODES,
    DOMAIN,
    EXPIRE_ACTIONS,
    EXPIRE_ACTIONS_SAVE_DELAY,
    SERVICE_ADD_CREDENTIAL,
    SERVICE_ADD_PIN,
    SERVICE_ENSURE_DOOR_PROFILE,
    SERVICE_GENERATE_CODE,
    SERVICE_LIST_ACCESS_PROFILES,
    SERVICE_LIST_CREDENTIALS,
    SERVICE_LIST_DOORS,
    SERVICE_LIST_SCHEDULES,
    SERVICE_LIST_USERS,
    SERVICE_REMOVE_CREDENTIAL,
    SERVICE_REMOVE_USER,
    SERVICE_SET_CREDENTIAL_ACCESS_PROFILES,
    SERVICE_SET_CREDENTIAL_CODE,
    SERVICE_SET_CREDENTIAL_ENABLED,
    SERVICE_SET_CREDENTIAL_VALIDITY,
    SERVICE_SET_USER,
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
ADD_CREDENTIAL_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_NAME): cv.string,
        vol.Required(ATTR_KIND): vol.In(list(CREDENTIAL_KIND_ID_KEY)),
        vol.Required(ATTR_CODE): cv.string,
        vol.Optional(ATTR_ACCESS_PROFILE_TOKENS, default=list): vol.All(
            cv.ensure_list, [cv.string]
        ),
        vol.Optional(ATTR_ENABLED, default=True): cv.boolean,
        # Optional native validity window (date only) + what the daily reaper
        # does once the end date passes. Coerce(str) so both the card's ISO
        # strings and the UI date selector (a date object) are accepted.
        vol.Optional(ATTR_VALID_FROM, default=""): vol.Coerce(str),
        vol.Optional(ATTR_VALID_TO, default=""): vol.Coerce(str),
        vol.Optional(ATTR_EXPIRE_ACTION, default=DEFAULT_EXPIRE_ACTION): vol.In(
            EXPIRE_ACTIONS
        ),
    }
)
SET_CREDENTIAL_VALIDITY_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_CREDENTIAL_TOKEN): cv.string,
        vol.Optional(ATTR_VALID_FROM, default=""): vol.Coerce(str),
        vol.Optional(ATTR_VALID_TO, default=""): vol.Coerce(str),
        vol.Optional(ATTR_EXPIRE_ACTION, default=DEFAULT_EXPIRE_ACTION): vol.In(
            EXPIRE_ACTIONS
        ),
    }
)
GENERATE_CODE_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_KIND): vol.In(list(CREDENTIAL_KIND_ID_KEY)),
        vol.Optional(ATTR_LENGTH): vol.All(vol.Coerce(int), vol.Range(min=1, max=32)),
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
LIST_USERS_SCHEMA = vol.Schema(_ENTRY_FIELD)
LIST_DOORS_SCHEMA = vol.Schema(_ENTRY_FIELD)
LIST_SCHEDULES_SCHEMA = vol.Schema(_ENTRY_FIELD)
ENSURE_DOOR_PROFILE_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_DOOR_TOKEN): cv.string,
        vol.Optional(ATTR_SCHEDULE_TOKEN, default=ALWAYS_SCHEDULE_TOKEN): cv.string,
    }
)
SET_USER_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_USER_TOKEN): cv.string,
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_FIRST_NAME): cv.string,
        vol.Optional(ATTR_LAST_NAME): cv.string,
        vol.Optional(ATTR_DESCRIPTION): cv.string,
    }
)
REMOVE_USER_SCHEMA = vol.Schema(
    {**_ENTRY_FIELD, vol.Required(ATTR_USER_TOKEN): cv.string}
)
SET_CREDENTIAL_PROFILES_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_CREDENTIAL_TOKEN): cv.string,
        vol.Required(ATTR_ACCESS_PROFILE_TOKENS, default=list): vol.All(
            cv.ensure_list, [cv.string]
        ),
    }
)
SET_CREDENTIAL_CODE_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_CREDENTIAL_TOKEN): cv.string,
        vol.Required(ATTR_KIND): vol.In(list(CREDENTIAL_KIND_ID_KEY)),
        vol.Required(ATTR_CODE): cv.string,
    }
)


def _user_display_name(user) -> str:
    """Best-effort human label for a cardholder (``name`` then ``First Last``)."""
    if user.name:
        return user.name
    return f"{user.first_name} {user.last_name}".strip()


def _credential_kind(credential) -> str:
    """Classify a credential by the identifiers it carries."""
    has_pin = credential.has_pin
    has_card = bool(credential.card)
    if has_pin and has_card:
        return "both"
    if has_pin:
        return "pin"
    if has_card:
        return "card"
    return "none"


def _to_device_datetime(date_str: str, *, end: bool) -> str:
    """Convert a ``YYYY-MM-DD`` date to the controller's validity dateTime.

    The controller honours the DATE only (``ValiditySupportsTimeValue=false``),
    so the time is cosmetic; the upper bound uses end-of-day so the end date is
    the LAST valid day (inclusive) — matched by the reaper's expiry test. Empty
    string clears that bound. Verified seam: if the device turns out to treat
    ValidTo exclusively, bump here and in the reaper together.
    """
    date_str = (date_str or "").strip()
    if not date_str:
        return ""
    try:
        date.fromisoformat(date_str)
    except ValueError as err:
        raise ServiceValidationError(
            f"Invalid date {date_str!r}; expected YYYY-MM-DD"
        ) from err
    return f"{date_str}T23:59:59Z" if end else f"{date_str}T00:00:00Z"


def _date_part(value: str | None) -> str | None:
    """The ``YYYY-MM-DD`` portion of a device dateTime (for the card)."""
    return value[:10] if value else None


def _record_expire_action(hass: HomeAssistant, token: str, action: str) -> None:
    """Persist (or clear, when ``action`` is empty) the reaper's expiry action
    for a credential. This map is the reaper's opt-in set."""
    domain_data = hass.data.get(DOMAIN) or {}
    actions = domain_data.get(DATA_EXPIRE_ACTIONS)
    if actions is None:
        return
    if action:
        actions[token] = action
    else:
        actions.pop(token, None)
    store = domain_data.get(DATA_EXPIRE_ACTIONS_STORE)
    if store is not None:
        store.async_delay_save(lambda: actions, EXPIRE_ACTIONS_SAVE_DELAY)


async def _client(hass: HomeAssistant, call: ServiceCall) -> AxisPacsClient:
    """Resolve the controller addressed by ``config_entry_id`` to its client.

    Server-side authorization: unless the controller opts into
    ``manage_allow_non_admin``, a call by a non-admin user is rejected (the card
    hides the controls, but the services must enforce it too). Calls with no user
    — automations, scripts, internal — always pass through.
    """
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
    allow_non_admin = entry.options.get(
        CONF_MANAGE_ALLOW_NON_ADMIN, DEFAULT_MANAGE_ALLOW_NON_ADMIN
    )
    if not allow_non_admin:
        user_id = call.context.user_id
        if user_id is not None:
            user = await hass.auth.async_get_user(user_id)
            if user is None or not user.is_admin:
                raise Unauthorized(
                    context=call.context, permission="admin", user_id=user_id
                )
    coordinator: AxisPacsCoordinator = entry.runtime_data
    return coordinator.client


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the access-code services once for the integration."""
    if hass.services.has_service(DOMAIN, SERVICE_ADD_PIN):
        return

    async def add_pin(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
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

    async def add_credential(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
        kind = call.data[ATTR_KIND]
        id_key = CREDENTIAL_KIND_ID_KEY[kind]
        # Validate dates up-front so a bad value can't create a credential then
        # fail half-applied.
        dev_from = _to_device_datetime(call.data[ATTR_VALID_FROM], end=False)
        dev_to = _to_device_datetime(call.data[ATTR_VALID_TO], end=True)
        try:
            user_token, credential_token = await client.async_add_credential(
                name=call.data[ATTR_NAME],
                id_data={id_key: call.data[ATTR_CODE]},
                access_profile_tokens=call.data[ATTR_ACCESS_PROFILE_TOKENS],
                enabled=call.data[ATTR_ENABLED],
            )
            if dev_from or dev_to:
                await client.async_set_credential_validity(
                    credential_token, dev_from, dev_to
                )
        except VapixError as err:
            raise HomeAssistantError(f"Failed to add credential: {err}") from err
        # The reaper only manages credentials with an end date; record its action.
        if call.data[ATTR_VALID_TO]:
            _record_expire_action(hass, credential_token, call.data[ATTR_EXPIRE_ACTION])
        return {"user_token": user_token, "credential_token": credential_token}

    async def generate_code(call: ServiceCall) -> ServiceResponse:
        """Generate a random, unique numeric code of the conventional length.

        The device exposes no configured length, so it's derived from existing
        codes (same kind first, then any, then the default). Uniqueness is
        checked against ALL existing credential codes. Uses ``secrets`` (these
        are access codes, not toys).
        """
        client = await _client(hass, call)
        id_key = CREDENTIAL_KIND_ID_KEY[call.data[ATTR_KIND]]
        try:
            creds = await client.async_get_credentials()
        except VapixError as err:
            raise HomeAssistantError(f"Failed to read credentials: {err}") from err

        same = [c.id_data.get(id_key) for c in creds if c.id_data.get(id_key)]
        id_keys = set(CREDENTIAL_KIND_ID_KEY.values())
        all_codes = [
            v for c in creds for k, v in c.id_data.items() if k in id_keys and v
        ]
        length = call.data.get(ATTR_LENGTH)
        if not length:
            lengths = [len(v) for v in same] or [len(v) for v in all_codes]
            length = (
                Counter(lengths).most_common(1)[0][0]
                if lengths
                else DEFAULT_CODE_LENGTH
            )
        taken = set(all_codes)
        for _ in range(2000):
            code = secrets.choice("123456789") + "".join(
                secrets.choice("0123456789") for _ in range(length - 1)
            )
            if code not in taken:
                return {"code": code, "length": length}
        raise HomeAssistantError("Could not generate a unique code")

    async def remove_credential(call: ServiceCall) -> None:
        client = await _client(hass, call)
        try:
            await client.async_remove_credential(call.data[ATTR_CREDENTIAL_TOKEN])
        except VapixError as err:
            raise HomeAssistantError(f"Failed to remove credential: {err}") from err

    async def set_credential_enabled(call: ServiceCall) -> None:
        client = await _client(hass, call)
        try:
            await client.async_set_credential_enabled(
                call.data[ATTR_CREDENTIAL_TOKEN], call.data[ATTR_ENABLED]
            )
        except VapixError as err:
            raise HomeAssistantError(f"Failed to change credential: {err}") from err

    async def list_credentials(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
        include_pins = call.data[ATTR_INCLUDE_PINS]
        try:
            creds = await client.async_get_credentials()
            users = await client.async_get_users()
            validity = await client.async_list_credential_validity()
        except VapixError as err:
            raise HomeAssistantError(f"Failed to list credentials: {err}") from err
        names = {u.token: _user_display_name(u) for u in users}
        domain_data = hass.data.get(DOMAIN) or {}
        last_used = domain_data.get(DATA_LAST_USED) or {}
        expire_actions = domain_data.get(DATA_EXPIRE_ACTIONS) or {}

        def _cred(c):
            lu = last_used.get(c.token) or {}
            vf, vt = validity.get(c.token, (None, None))
            return {
                "token": c.token,
                "user_token": c.user_token,
                "user_name": names.get(c.user_token, ""),
                "description": c.description,
                "enabled": c.enabled,
                "kind": _credential_kind(c),
                "has_pin": c.has_pin,
                "has_card": bool(c.card),
                "access_profile_tokens": c.access_profile_tokens,
                "last_used": lu.get("time") or None,
                "last_used_door": lu.get("door") or None,
                # Native validity window (date only) + the reaper's end-action.
                "valid_from": _date_part(vf),
                "valid_to": _date_part(vt),
                "expire_action": expire_actions.get(c.token) or None,
                **({"pin": c.pin, "card": c.card} if include_pins else {}),
            }

        return {"credentials": [_cred(c) for c in creds]}

    async def list_access_profiles(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
        try:
            profiles = await client.async_get_access_profiles()
            access_points = await client.async_get_access_points()
            doors = await client.async_get_door_info_list()
            schedules = await client.async_get_schedules()
        except VapixError as err:
            raise HomeAssistantError(f"Failed to list access profiles: {err}") from err

        door_names = {d.token: d.name for d in doors}
        ap_to_door = {ap.token: ap.door_token for ap in access_points if ap.door_token}
        sched_names = {s.token: s.name for s in schedules}

        def _door_of(entity_token: str) -> str:
            return (
                entity_token
                if entity_token in door_names
                else ap_to_door.get(entity_token, "")
            )

        def resolve_doors(profile) -> list[dict[str, str]]:
            out: dict[str, str] = {}
            for entity_token in profile.entity_tokens:
                dt = _door_of(entity_token)
                if dt and dt not in out:
                    out[dt] = door_names.get(dt, dt)
            return [{"token": t, "name": n} for t, n in out.items()]

        def resolve_policies(profile) -> list[dict[str, str]]:
            seen: set[tuple[str, str]] = set()
            out: list[dict[str, str]] = []
            for pol in profile.policies:
                dt = _door_of(pol.entity_token)
                key = (dt, pol.schedule_token)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "door_token": dt,
                        "door_name": door_names.get(dt, dt),
                        "schedule_token": pol.schedule_token,
                        "schedule_name": sched_names.get(
                            pol.schedule_token, pol.schedule_token
                        ),
                    }
                )
            return out

        return {
            "access_profiles": [
                {
                    "token": p.token,
                    "name": p.name,
                    "access_point_tokens": p.entity_tokens,
                    "doors": resolve_doors(p),
                    "policies": resolve_policies(p),
                    "schedules": sorted(
                        {pol["schedule_name"] for pol in resolve_policies(p)}
                    ),
                    # The built-in Request-to-Exit enabler profile is internal,
                    # not a user-assignable group — flag it so the card hides it.
                    "system": "rexenabler" in p.name.lower(),
                }
                for p in profiles
            ]
        }

    async def list_doors(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
        try:
            doors = await client.async_get_door_info_list()
        except VapixError as err:
            raise HomeAssistantError(f"Failed to list doors: {err}") from err
        return {"doors": [{"token": d.token, "name": d.name} for d in doors]}

    async def list_schedules(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
        try:
            schedules = await client.async_get_schedules()
        except VapixError as err:
            raise HomeAssistantError(f"Failed to list schedules: {err}") from err
        return {"schedules": [{"token": s.token, "name": s.name} for s in schedules]}

    async def ensure_door_profile(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
        door_token = call.data[ATTR_DOOR_TOKEN]
        schedule_token = call.data[ATTR_SCHEDULE_TOKEN]
        try:
            doors = {d.token: d.name for d in await client.async_get_door_info_list()}
            scheds = {s.token: s.name for s in await client.async_get_schedules()}
            profile_token = await client.async_ensure_door_profile(
                door_token=door_token,
                schedule_token=schedule_token,
                door_name=doors.get(door_token, ""),
                schedule_name=scheds.get(schedule_token, ""),
            )
        except VapixError as err:
            raise HomeAssistantError(f"Failed to ensure door profile: {err}") from err
        return {"profile_token": profile_token}

    async def list_users(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
        try:
            users = await client.async_get_users()
        except VapixError as err:
            raise HomeAssistantError(f"Failed to list users: {err}") from err
        return {
            "users": [
                {
                    "token": u.token,
                    "name": u.name,
                    "display_name": _user_display_name(u),
                    "first_name": u.first_name,
                    "last_name": u.last_name,
                    "description": u.description,
                }
                for u in users
            ]
        }

    async def set_user(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
        try:
            user_token = await client.async_set_user(
                token=call.data[ATTR_USER_TOKEN],
                name=call.data[ATTR_NAME],
                first_name=call.data.get(ATTR_FIRST_NAME),
                last_name=call.data.get(ATTR_LAST_NAME),
                description=call.data.get(ATTR_DESCRIPTION, ""),
            )
        except VapixError as err:
            raise HomeAssistantError(f"Failed to set user: {err}") from err
        return {"user_token": user_token}

    async def remove_user(call: ServiceCall) -> None:
        client = await _client(hass, call)
        try:
            await client.async_remove_user(call.data[ATTR_USER_TOKEN])
        except VapixError as err:
            raise HomeAssistantError(f"Failed to remove user: {err}") from err

    async def set_credential_access_profiles(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
        try:
            credential_token = await client.async_set_credential_access_profiles(
                call.data[ATTR_CREDENTIAL_TOKEN],
                call.data[ATTR_ACCESS_PROFILE_TOKENS],
            )
        except VapixError as err:
            raise HomeAssistantError(
                f"Failed to set credential access profiles: {err}"
            ) from err
        return {"credential_token": credential_token}

    async def set_credential_code(call: ServiceCall) -> ServiceResponse:
        client = await _client(hass, call)
        id_key = CREDENTIAL_KIND_ID_KEY[call.data[ATTR_KIND]]
        try:
            credential_token = await client.async_set_credential_id_data(
                call.data[ATTR_CREDENTIAL_TOKEN],
                {id_key: call.data[ATTR_CODE]},
            )
        except VapixError as err:
            raise HomeAssistantError(f"Failed to set credential code: {err}") from err
        return {"credential_token": credential_token}

    async def set_credential_validity(call: ServiceCall) -> ServiceResponse:
        """Set (or clear) a credential's start/end dates + record the reaper's
        end-action. The controller enforces the window itself (date only); the
        reaper applies disable/delete once the end date passes."""
        client = await _client(hass, call)
        token = call.data[ATTR_CREDENTIAL_TOKEN]
        dev_from = _to_device_datetime(call.data[ATTR_VALID_FROM], end=False)
        dev_to = _to_device_datetime(call.data[ATTR_VALID_TO], end=True)
        try:
            await client.async_set_credential_validity(token, dev_from, dev_to)
        except VapixError as err:
            raise HomeAssistantError(
                f"Failed to set credential validity: {err}"
            ) from err
        # An end date opts the credential into the reaper; clearing it opts out.
        _record_expire_action(
            hass, token, call.data[ATTR_EXPIRE_ACTION] if call.data[ATTR_VALID_TO] else ""
        )
        return {"credential_token": token}

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_PIN, add_pin,
        schema=ADD_PIN_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_CREDENTIAL, add_credential,
        schema=ADD_CREDENTIAL_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GENERATE_CODE, generate_code,
        schema=GENERATE_CODE_SCHEMA, supports_response=SupportsResponse.ONLY,
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
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_USERS, list_users,
        schema=LIST_USERS_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_DOORS, list_doors,
        schema=LIST_DOORS_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_SCHEDULES, list_schedules,
        schema=LIST_SCHEDULES_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ENSURE_DOOR_PROFILE, ensure_door_profile,
        schema=ENSURE_DOOR_PROFILE_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_USER, set_user,
        schema=SET_USER_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_USER, remove_user, schema=REMOVE_USER_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CREDENTIAL_ACCESS_PROFILES, set_credential_access_profiles,
        schema=SET_CREDENTIAL_PROFILES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CREDENTIAL_CODE, set_credential_code,
        schema=SET_CREDENTIAL_CODE_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CREDENTIAL_VALIDITY, set_credential_validity,
        schema=SET_CREDENTIAL_VALIDITY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
