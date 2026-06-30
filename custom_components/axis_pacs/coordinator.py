"""Coordinator that owns the controller connection and live event stream."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import date

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ACCESS_USED_TOPIC_SUFFIXES,
    BACKFILL_EVENT_LIMIT,
    CONF_MANAGE_CODES,
    DATA_BACKFILL_DONE,
    DATA_EXPIRE_ACTIONS,
    DATA_EXPIRE_ACTIONS_STORE,
    DATA_LAST_USED,
    DATA_LAST_USED_STORE,
    DEFAULT_MANAGE_CODES,
    DOMAIN,
    EXPIRE_ACTION_DELETE,
    EXPIRE_ACTIONS_SAVE_DELAY,
    LAST_USED_SAVE_DELAY,
    REAPER_HOUR,
    REAPER_MINUTE,
    REAPER_STARTUP_DELAY,
)
from .vapix import (
    AxisPacsClient,
    DeviceIdentity,
    Door,
    DoorMode,
    DoorState,
    Notification,
    PullPointManager,
    VapixError,
)

_LOGGER = logging.getLogger(__name__)

# We only care about the logical lock state for the lock platform.
DOOR_MODE_TOPIC_SUFFIX = "Door/State/DoorMode"

type AxisPacsConfigEntry = ConfigEntry[AxisPacsCoordinator]


class AxisPacsCoordinator(DataUpdateCoordinator[dict[str, DoorState]]):
    """Push-based coordinator: seeds state once, then updates on events.

    The data shape is ``{door_token: DoorState}``. There is no polling
    ``update_interval``; live updates arrive via :class:`PullPointManager`.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AxisPacsConfigEntry,
        client: AxisPacsClient,
        identity: DeviceIdentity,
        doors: list[Door],
        ap_to_door: dict[str, str] | None = None,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.client = client
        self.identity = identity
        self.doors: dict[str, Door] = {door.token: door for door in doors}
        # access point (reader) token -> door name, for labelling "last used".
        self._ap_to_door: dict[str, str] = ap_to_door or {}
        self._states: dict[str, DoorState] = {token: DoorState() for token in self.doors}
        self._pullpoint = PullPointManager(
            client, on_event=self._handle_event, on_resync=self._async_resync
        )
        self._event_task = None
        # Unsubscribes for the daily expiry reaper (manage_codes controller only).
        self._reaper_unsubs: list = []

    async def _async_update_data(self) -> dict[str, DoorState]:
        """Read the current state of every local door (initial seed + resync)."""
        try:
            for token in self.doors:
                self._states[token] = await self.client.async_get_door_state(token)
        except VapixError as err:
            raise UpdateFailed(str(err)) from err
        return dict(self._states)

    async def _async_resync(self) -> None:
        await self.async_request_refresh()

    @callback
    def _handle_event(self, notification: Notification) -> None:
        """Route a notification: door-mode → lock state, access → last-used."""
        topic = notification.topic
        if topic.endswith(DOOR_MODE_TOPIC_SUFFIX):
            token = notification.door_token
            if token is None or token not in self._states:
                return
            self._states[token] = DoorState(mode=DoorMode.parse(notification.state))
            self.async_set_updated_data(dict(self._states))
            return
        if any(topic.endswith(suffix) for suffix in ACCESS_USED_TOPIC_SUFFIXES):
            self._record_credential_use(notification)

    @callback
    def _record_credential_use(self, notification: Notification) -> None:
        """Record the time a credential was used, into the shared domain store.

        Cluster-wide: each controller sees only events for its own doors, so all
        coordinators write the same ``hass.data[DOMAIN]`` map (keyed by the
        cluster-unique credential token) that the manager's service reads back.
        """
        cred_token = notification.data.get("CredentialToken")
        if not cred_token:
            return
        domain_data = self.hass.data.get(DOMAIN)
        if not domain_data:
            return
        last_used: dict | None = domain_data.get(DATA_LAST_USED)
        if last_used is None:
            return
        when = notification.utc_time or ""
        prev = last_used.get(cred_token)
        # Keep the most recent timestamp (ISO-8601 UTC strings sort chronologically).
        if prev and when and prev.get("time", "") >= when:
            return
        ap_token = notification.source.get("AccessPointToken", "")
        last_used[cred_token] = {
            "time": when,
            "door": self._ap_to_door.get(ap_token, ap_token),
            "holder": notification.data.get("CredentialHolderName", ""),
        }
        store = domain_data.get(DATA_LAST_USED_STORE)
        if store is not None:
            store.async_delay_save(lambda: last_used, LAST_USED_SAVE_DELAY)

    async def async_backfill_last_used(self) -> None:
        """One-time seed of "last used" from the controller's event log.

        Best-effort historical backfill: runs once per session on the
        ``manage_codes`` controller (its log is cluster-wide via global event
        distribution), filling credentials that have a logged access. Live
        events always win (newer timestamp), and credentials with nothing in
        the log stay blank.
        """
        if not self.config_entry.options.get(CONF_MANAGE_CODES, DEFAULT_MANAGE_CODES):
            return
        domain_data = self.hass.data.get(DOMAIN)
        if not domain_data or domain_data.get(DATA_BACKFILL_DONE):
            return
        last_used: dict | None = domain_data.get(DATA_LAST_USED)
        if last_used is None:
            return
        domain_data[DATA_BACKFILL_DONE] = True  # guard before the slow call
        try:
            # Newest window (Descending) reaches recent activity; oldest window
            # adds credentials used only early on. No filter/pagination exists,
            # so this is a best-effort sample of the two ends of the log.
            events = await self.client.async_fetch_event_log(
                limit=BACKFILL_EVENT_LIMIT, descending=True
            )
            events += await self.client.async_fetch_event_log(
                limit=BACKFILL_EVENT_LIMIT, descending=False
            )
        except Exception as err:  # noqa: BLE001 - never break setup over a log read
            domain_data[DATA_BACKFILL_DONE] = False  # allow a retry next session
            _LOGGER.debug("last-used backfill skipped: %s", err)
            return

        changed = False
        for utc, topic, data in events:
            if not topic.startswith(ACCESS_USED_TOPIC_SUFFIXES):
                continue
            token = data.get("CredentialToken")
            if not token or not utc:
                continue
            prev = last_used.get(token)
            if prev and prev.get("time", "") >= utc:
                continue
            ap_token = data.get("AccessPointToken", "")
            last_used[token] = {
                "time": utc,
                "door": self._ap_to_door.get(ap_token, ap_token),
                "holder": data.get("CredentialHolderName", ""),
            }
            changed = True
        if changed and (store := domain_data.get(DATA_LAST_USED_STORE)) is not None:
            store.async_delay_save(lambda: last_used, LAST_USED_SAVE_DELAY)
        _LOGGER.debug(
            "last-used backfill: scanned %d events, %s credentials seeded",
            len(events), "some" if changed else "no",
        )

    async def async_run_expiry_reaper(self) -> None:
        """Apply the end-action (disable/delete) to credentials past their ValidTo.

        Native ValidTo already DENIES an expired credential on its own; this does
        the extra disable/delete, which has no on-device mechanism. Runs only on
        the ``manage_codes`` controller and acts ONLY on credentials with a
        recorded expiry action (the card's opt-in set), never one whose window
        was set out-of-band.
        """
        if not self.config_entry.options.get(CONF_MANAGE_CODES, DEFAULT_MANAGE_CODES):
            return
        domain_data = self.hass.data.get(DOMAIN)
        if not domain_data:
            return
        actions: dict | None = domain_data.get(DATA_EXPIRE_ACTIONS)
        if not actions:
            return
        try:
            validity = await self.client.async_list_credential_validity()
            creds = {c.token: c for c in await self.client.async_get_credentials()}
        except VapixError as err:
            _LOGGER.debug("expiry reaper skipped: %s", err)
            return

        today = dt_util.now().date()
        changed = False
        for token in list(actions):
            cred = creds.get(token)
            if cred is None:
                actions.pop(token, None)  # gone out-of-band; drop stale entry
                changed = True
                continue
            valid_to = validity.get(token, (None, None))[1]
            if not valid_to:
                continue  # end date cleared on the device — leave it alone
            try:
                end_date = date.fromisoformat(valid_to[:10])
            except ValueError:
                continue
            if today <= end_date:
                continue  # still valid (end day is inclusive)
            try:
                if actions.get(token) == EXPIRE_ACTION_DELETE:
                    await self._async_delete_expired(cred)
                    actions.pop(token, None)
                    changed = True
                elif cred.enabled:
                    await self.client.async_set_credential_enabled(token, False)
                    _LOGGER.info("Expiry reaper disabled credential %s", token)
            except VapixError as err:
                _LOGGER.warning("Expiry reaper failed on %s: %s", token, err)
        if changed and (store := domain_data.get(DATA_EXPIRE_ACTIONS_STORE)) is not None:
            store.async_delay_save(lambda: actions, EXPIRE_ACTIONS_SAVE_DELAY)

    async def _async_delete_expired(self, cred) -> None:
        """Remove an expired credential, plus its cardholder if now orphaned."""
        await self.client.async_remove_credential(cred.token)
        _LOGGER.info("Expiry reaper deleted credential %s", cred.token)
        if not cred.user_token:
            return
        remaining = [
            c
            for c in await self.client.async_get_credentials()
            if c.user_token == cred.user_token
        ]
        if not remaining:
            with suppress(VapixError):
                await self.client.async_remove_user(cred.user_token)

    @callback
    def _schedule_reaper_run(self, _now=None) -> None:
        """Kick a reaper pass as a background task (off the timer callback)."""
        self.config_entry.async_create_background_task(
            self.hass,
            self.async_run_expiry_reaper(),
            name=f"{DOMAIN}_reaper_{self.identity.serial}",
        )

    @callback
    def set_door_mode(self, token: str, mode: DoorMode) -> None:
        """Optimistically reflect a command; the next event confirms/corrects it."""
        if token not in self._states:
            return
        self._states[token] = DoorState(mode=mode)
        self.async_set_updated_data(dict(self._states))

    def start_event_listener(self) -> None:
        """Launch the PullPoint loop + the one-time last-used backfill."""
        self._event_task = self.config_entry.async_create_background_task(
            self.hass,
            self._pullpoint.async_run(),
            name=f"{DOMAIN}_pullpoint_{self.identity.serial}",
        )
        # Best-effort historical seed; runs once, off the setup path.
        self.config_entry.async_create_background_task(
            self.hass,
            self.async_backfill_last_used(),
            name=f"{DOMAIN}_backfill_{self.identity.serial}",
        )
        # Daily expiry reaper (manage_codes controller only): a quiet-hour tick
        # plus one shortly after startup so a restart never skips a day.
        if self.config_entry.options.get(CONF_MANAGE_CODES, DEFAULT_MANAGE_CODES):
            self._reaper_unsubs.append(
                async_track_time_change(
                    self.hass,
                    self._schedule_reaper_run,
                    hour=REAPER_HOUR,
                    minute=REAPER_MINUTE,
                    second=0,
                )
            )
            self._reaper_unsubs.append(
                async_call_later(
                    self.hass, REAPER_STARTUP_DELAY, self._schedule_reaper_run
                )
            )

    async def async_shutdown(self) -> None:
        # Cancel the reaper timers first.
        for unsub in self._reaper_unsubs:
            unsub()
        self._reaper_unsubs.clear()
        # Stop the long-poll task first so no pull is in flight, then
        # unsubscribe and close the client.
        if self._event_task is not None:
            self._event_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._event_task
            self._event_task = None
        await self._pullpoint.async_stop()
        await super().async_shutdown()
        await self.client.async_close()
