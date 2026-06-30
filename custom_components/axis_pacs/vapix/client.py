"""Self-contained async VAPIX client for Axis door controllers.

Uses ``httpx`` (bundled with Home Assistant) with HTTP Digest auth. Door control
and events both target ``POST /vapix/services``; identity comes from
``param.cgi``. Home Assistant independent.
"""

from __future__ import annotations

import logging
from xml.etree import ElementTree as ET

import httpx

from . import soap
from .models import (
    AccessPoint,
    AccessProfile,
    Credential,
    DeviceIdentity,
    Door,
    DoorState,
    Notification,
    Schedule,
    TcrCredential,
    User,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0


class VapixError(Exception):
    """Base error for VAPIX client failures (including SOAP faults)."""


class CannotConnect(VapixError):
    """The controller could not be reached."""


class InvalidAuth(VapixError):
    """Authentication was rejected."""


class AxisPacsClient:
    """Thin async client for the Axis door-control + event APIs."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 0,
        use_https: bool = False,
        verify_ssl: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        scheme = "https" if use_https else "http"
        netloc = f"{host}:{port}" if port else host
        self._base = f"{scheme}://{netloc}"
        self._services_url = f"{self._base}/vapix/services"
        self._param_url = f"{self._base}/axis-cgi/param.cgi"
        self._client = httpx.AsyncClient(
            auth=httpx.DigestAuth(username, password),
            verify=verify_ssl,
            timeout=httpx.Timeout(timeout),
            headers={"Accept-Encoding": "identity"},
        )

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def services_url(self) -> str:
        return self._services_url

    async def async_close(self) -> None:
        await self._client.aclose()

    # --- transport ---------------------------------------------------------- #
    async def async_call(
        self, body: str, *, action: str | None = None, timeout: float | None = None
    ) -> ET.Element:
        """POST a SOAP envelope to ``/vapix/services`` and return the root element."""
        content_type = "application/soap+xml; charset=utf-8"
        if action:
            content_type += f'; action="{action}"'
        kwargs: dict = {
            "content": body.encode("utf-8"),
            "headers": {"Content-Type": content_type},
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = await self._client.post(self._services_url, **kwargs)
        except httpx.HTTPError as err:
            raise CannotConnect(str(err)) from err
        self._raise_for_status(resp.status_code)
        try:
            return soap.parse(resp.content)
        except soap.SoapFault as err:
            raise VapixError(str(err)) from err
        except ET.ParseError as err:
            raise VapixError(f"Malformed XML response: {err}") from err

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if status_code == 401:
            raise InvalidAuth("Authentication failed")
        if status_code >= 400:
            raise CannotConnect(f"HTTP {status_code}")

    # --- identity / door control ------------------------------------------- #
    async def async_get_identity(self) -> DeviceIdentity:
        try:
            resp = await self._client.get(
                self._param_url,
                params={"action": "list", "group": "Brand,Properties"},
            )
        except httpx.HTTPError as err:
            raise CannotConnect(str(err)) from err
        self._raise_for_status(resp.status_code)
        identity = soap.parse_identity(resp.text)
        if not identity.serial:
            raise VapixError("Device did not return a serial number")
        return identity

    async def async_get_door_info_list(self) -> list[Door]:
        return soap.parse_door_info_list(await self.async_call(soap.get_door_info_list()))

    async def async_get_local_doors(self, serial: str) -> list[Door]:
        """Doors owned by *this* controller only (peer doors filtered out)."""
        return [
            door
            for door in await self.async_get_door_info_list()
            if door.is_local_to(serial)
        ]

    async def async_get_door_state(self, token: str) -> DoorState:
        return soap.parse_door_state(await self.async_call(soap.get_door_state(token)))

    async def async_lock(self, token: str) -> None:
        await self.async_call(soap.door_command("LockDoor", token))

    async def async_unlock(self, token: str) -> None:
        await self.async_call(soap.door_command("UnlockDoor", token))

    async def async_access(self, token: str) -> None:
        """Momentary unlock (auto-relocks after the door's access time)."""
        await self.async_call(soap.door_command("AccessDoor", token))

    # --- event subscription primitives (used by PullPointManager) ---------- #
    async def async_create_pull_point(self, termination: str) -> tuple[str, str]:
        """Create a subscription; return ``(subscription_id, address)``."""
        root = await self.async_call(
            soap.create_pull_point(self._services_url, termination),
            action=soap.ACTION_CREATE_PULLPOINT,
        )
        sub_id, address = soap.parse_create_pull_point(root)
        if not sub_id:
            raise VapixError("CreatePullPointSubscription returned no SubscriptionId")
        return sub_id, address or self._services_url

    async def async_pull_messages(
        self,
        address: str,
        subscription_id: str,
        timeout: str,
        limit: int,
        http_timeout: float,
    ) -> list[Notification]:
        root = await self.async_call(
            soap.pull_messages(address, subscription_id, timeout, limit),
            action=soap.ACTION_PULL,
            timeout=http_timeout,
        )
        return soap.parse_notifications(root)

    async def async_renew(
        self, address: str, subscription_id: str, termination: str
    ) -> None:
        await self.async_call(
            soap.renew(address, subscription_id, termination),
            action=soap.ACTION_RENEW,
        )

    async def async_unsubscribe(self, address: str, subscription_id: str) -> None:
        await self.async_call(
            soap.unsubscribe(address, subscription_id),
            action=soap.ACTION_UNSUBSCRIBE,
        )

    # --- access-code / credential management (cluster-wide; not per-door) ----- #
    # Reads are safe to run anywhere. Writes mutate the *shared cluster database*
    # — every controller in the cluster sees the result — so callers must treat
    # them as cluster-global, not local to this controller.
    async def async_get_users(
        self, *, page: int = 100, max_total: int = 5000
    ) -> list[User]:
        """All cardholders, following pagination up to ``max_total``."""
        users: list[User] = []
        start: str | None = None
        while True:
            root = await self.async_call(soap.get_user_list(page, start))
            chunk, start = soap.parse_user_list(root)
            users.extend(chunk)
            if not start or len(users) >= max_total:
                return users

    async def async_get_credentials(
        self, *, page: int = 100, max_total: int = 5000
    ) -> list[Credential]:
        """All credentials (PIN/card), following pagination up to ``max_total``."""
        creds: list[Credential] = []
        start: str | None = None
        while True:
            root = await self.async_call(soap.get_credential_list(page, start))
            chunk, start = soap.parse_credentials(root)
            creds.extend(chunk)
            if not start or len(creds) >= max_total:
                return creds

    async def async_get_credential(self, token: str) -> Credential | None:
        creds, _ = soap.parse_credentials(await self.async_call(soap.get_credential(token)))
        return creds[0] if creds else None

    async def async_get_access_profiles(self) -> list[AccessProfile]:
        return soap.parse_access_profiles(
            await self.async_call(soap.get_access_profile_list())
        )

    async def async_get_schedules(self) -> list[Schedule]:
        return soap.parse_schedule_info_list(
            await self.async_call(soap.get_schedule_info_list())
        )

    async def async_get_access_points(self) -> list[AccessPoint]:
        return soap.parse_access_point_info_list(
            await self.async_call(soap.get_access_point_info_list())
        )

    async def async_get_access_points_for_door(self, door_token: str) -> list[AccessPoint]:
        """Access points (reader sides) that belong to ``door_token``."""
        return [
            ap
            for ap in await self.async_get_access_points()
            if ap.door_token == door_token
        ]

    async def async_fetch_event_log(
        self, *, limit: int = 1000, descending: bool = True
    ) -> list[tuple[str, str, dict[str, str]]]:
        """The persistent event log as ``(utc_time, topic_path, data)`` tuples.

        Uses the **JSON** EventLogger API (``POST /vapix/eventlogger``) — the SOAP
        ``FetchEvents`` only ever returns the oldest events. ``descending=True``
        returns NEWEST-first (the only way to reach recent activity); the device
        caps the response (~1000) and exposes no topic/time/pagination filter, so
        this returns a single newest- or oldest-end window.
        """
        url = f"{self._base}/vapix/eventlogger"
        body = {"FetchEvents3": {"Limit": int(limit), "Descending": bool(descending)}}
        try:
            resp = await self._client.post(
                url, json=body, timeout=httpx.Timeout(120.0)
            )
        except httpx.HTTPError as err:
            raise CannotConnect(str(err)) from err
        self._raise_for_status(resp.status_code)
        try:
            data = resp.json()
        except ValueError as err:
            raise VapixError(f"Malformed event-log JSON: {err}") from err

        out: list[tuple[str, str, dict[str, str]]] = []
        for ev in data.get("Event", []):
            utc = ev.get("UtcTime", "") or ""
            topic: dict[str, str] = {}
            kv: dict[str, str] = {}
            for item in ev.get("KeyValues", []):
                key = item.get("Key")
                if not key:
                    continue
                (topic if key.startswith("topic") else kv)[key] = item.get("Value", "")
            topic_path = "/".join(topic[k] for k in sorted(topic) if topic.get(k))
            out.append((utc, topic_path, kv))
        return out

    # --- writes (mutate the shared cluster DB — handle with care) ------------ #
    async def async_set_user(
        self,
        *,
        token: str = "",
        name: str,
        first_name: str | None = None,
        last_name: str | None = None,
        description: str = "",
    ) -> str:
        """Create (token="") or modify a cardholder; returns the user token."""
        root = await self.async_call(
            soap.set_user(token, name, first_name, last_name, description)
        )
        return soap.parse_token(root, soap.UDB) or token

    async def async_remove_user(self, token: str) -> None:
        await self.async_call(soap.remove_user(token))

    async def async_set_credential(
        self,
        *,
        token: str = "",
        user_token: str,
        id_data: dict[str, str],
        access_profile_tokens: list[str],
        enabled: bool = True,
        description: str = "",
        status: str = "Enabled",
    ) -> str:
        """Create (token="") or modify a credential; returns the credential token."""
        root = await self.async_call(
            soap.set_credential(
                token,
                user_token,
                id_data,
                access_profile_tokens,
                enabled=enabled,
                description=description,
                status=status,
            )
        )
        return soap.parse_token(root, soap.PX) or token

    async def async_remove_credential(self, token: str) -> None:
        await self.async_call(soap.remove_credential(token))

    async def async_create_access_profile(
        self, *, name: str, policies: list[tuple[str, str]], description: str = ""
    ) -> str:
        """Create an access profile (group); returns its token."""
        root = await self.async_call(
            soap.create_access_profile(name, policies, description)
        )
        return soap.parse_token(root, soap.TAR) or ""

    async def async_delete_access_profile(self, token: str) -> None:
        await self.async_call(soap.delete_access_profile(token))

    async def async_ensure_door_profile(
        self,
        *,
        door_token: str,
        schedule_token: str,
        door_name: str = "",
        schedule_name: str = "",
    ) -> str:
        """Find or create a one-door profile granting ``schedule`` at ``door``.

        Reuses an existing profile whose policy set is EXACTLY that door's access
        point(s) on that schedule (so per-door grants don't proliferate); else
        creates one named e.g. ``"Side Entry (Always)"``. Returns the token.
        """
        aps = [
            ap.token
            for ap in await self.async_get_access_points()
            if ap.door_token == door_token
        ]
        if not aps:
            raise VapixError(f"Door {door_token!r} has no access points")
        want = {(schedule_token, ap) for ap in aps}
        for profile in await self.async_get_access_profiles():
            have = {(pol.schedule_token, pol.entity_token) for pol in profile.policies}
            if have == want:
                return profile.token
        label = f"{door_name} ({schedule_name})" if door_name and schedule_name else (
            door_name or "Door grant"
        )
        return await self.async_create_access_profile(
            name=label, policies=[(schedule_token, ap) for ap in aps]
        )

    async def async_set_credential_enabled(self, token: str, enabled: bool) -> None:
        await self.async_call(soap.set_credential_enabled(token, enabled))

    async def async_set_credential_access_profiles(
        self, token: str, access_profile_tokens: list[str]
    ) -> str:
        """Replace which access profiles (doors) an existing credential grants.

        ``SetCredential`` requires the *whole* record, so fetch the current
        credential first and re-set it preserving its holder, identifiers
        (PIN/card), enabled flag and description — changing only the profiles.
        """
        cred = await self.async_get_credential(token)
        if cred is None:
            raise VapixError(f"Credential {token!r} not found")
        return await self.async_set_credential(
            token=token,
            user_token=cred.user_token,
            id_data=cred.id_data,
            access_profile_tokens=access_profile_tokens,
            enabled=cred.enabled,
            description=cred.description,
            status=cred.status or "Enabled",
        )

    async def async_set_credential_id_data(
        self, token: str, id_data: dict[str, str]
    ) -> str:
        """Replace an existing credential's identifiers (PIN/card), keeping its
        holder, access profiles, enabled flag and status."""
        cred = await self.async_get_credential(token)
        if cred is None:
            raise VapixError(f"Credential {token!r} not found")
        return await self.async_set_credential(
            token=token,
            user_token=cred.user_token,
            id_data=id_data,
            access_profile_tokens=cred.access_profile_tokens,
            enabled=cred.enabled,
            description=cred.description,
            status=cred.status or "Enabled",
        )

    # --- validity window (ONVIF tcr; date-only enforced by the controller) ---- #
    async def async_get_tcr_credential(self, token: str) -> TcrCredential | None:
        """Full ONVIF-`tcr` view of a credential (carries the validity window)."""
        creds, _ = soap.parse_tcr_credentials(
            await self.async_call(soap.get_tcr_credentials(token))
        )
        return creds[0] if creds else None

    async def async_list_credential_validity(
        self, *, page: int = 100, max_total: int = 5000
    ) -> dict[str, tuple[str | None, str | None]]:
        """Map ``credential_token -> (valid_from, valid_to)`` for all credentials.

        One `tcr:GetCredentialList` pass (paged); credentials with no window are
        omitted, so the caller treats a missing key as "no validity set".
        """
        out: dict[str, tuple[str | None, str | None]] = {}
        start: str | None = None
        total = 0
        while True:
            root = await self.async_call(soap.get_tcr_credential_list(page, start))
            chunk, start = soap.parse_tcr_credentials(root)
            for c in chunk:
                total += 1
                if c.valid_from or c.valid_to:
                    out[c.token] = (c.valid_from, c.valid_to)
            if not start or total >= max_total:
                return out

    async def async_set_credential_validity(
        self, token: str, valid_from: str | None, valid_to: str | None
    ) -> None:
        """Set (or clear) a credential's validity window via `tcr:ModifyCredential`.

        Validity has no Axis-native (`pacsaxis`) field, so it goes through the
        ONVIF `tcr` view of the same credential. ModifyCredential needs the whole
        record, so fetch it first and re-send it unchanged but for the window.
        ``valid_from``/``valid_to`` are device dateTime strings (or "" to clear);
        the controller honours the DATE only.
        """
        cred = await self.async_get_tcr_credential(token)
        if cred is None:
            raise VapixError(f"Credential {token!r} not found")
        await self.async_call(
            soap.modify_tcr_credential(cred, valid_from or "", valid_to or "")
        )

    async def async_add_credential(
        self,
        *,
        name: str,
        id_data: dict[str, str],
        access_profile_tokens: list[str],
        first_name: str | None = None,
        last_name: str | None = None,
        enabled: bool = True,
    ) -> tuple[str, str]:
        """Create a cardholder + a credential (PIN and/or card) granting profiles.

        ``id_data`` maps identifier names to raw values, e.g. ``{"PIN": "1234"}``
        or ``{"CardNr": "12345"}``. Returns ``(user_token, credential_token)``.
        """
        user_token = await self.async_set_user(
            name=name, first_name=first_name, last_name=last_name
        )
        credential_token = await self.async_set_credential(
            user_token=user_token,
            id_data=id_data,
            access_profile_tokens=access_profile_tokens,
            enabled=enabled,
            description=name,
        )
        return user_token, credential_token

    async def async_add_pin(
        self,
        *,
        name: str,
        pin: str,
        access_profile_tokens: list[str],
        first_name: str | None = None,
        last_name: str | None = None,
        enabled: bool = True,
    ) -> tuple[str, str]:
        """Convenience wrapper: create a cardholder + a PIN credential."""
        return await self.async_add_credential(
            name=name,
            id_data={"PIN": pin},
            access_profile_tokens=access_profile_tokens,
            first_name=first_name,
            last_name=last_name,
            enabled=enabled,
        )
