"""Coordinator that owns the controller connection and live event stream."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
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
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.client = client
        self.identity = identity
        self.doors: dict[str, Door] = {door.token: door for door in doors}
        self._states: dict[str, DoorState] = {token: DoorState() for token in self.doors}
        self._pullpoint = PullPointManager(
            client, on_event=self._handle_event, on_resync=self._async_resync
        )
        self._event_task = None

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
        """Apply a DoorMode notification to the matching local door."""
        if not notification.topic.endswith(DOOR_MODE_TOPIC_SUFFIX):
            return
        token = notification.door_token
        if token is None or token not in self._states:
            return
        self._states[token] = DoorState(mode=DoorMode.parse(notification.state))
        self.async_set_updated_data(dict(self._states))

    @callback
    def set_door_mode(self, token: str, mode: DoorMode) -> None:
        """Optimistically reflect a command; the next event confirms/corrects it."""
        if token not in self._states:
            return
        self._states[token] = DoorState(mode=mode)
        self.async_set_updated_data(dict(self._states))

    def start_event_listener(self) -> None:
        """Launch the PullPoint loop as an entry-scoped background task."""
        self._event_task = self.config_entry.async_create_background_task(
            self.hass,
            self._pullpoint.async_run(),
            name=f"{DOMAIN}_pullpoint_{self.identity.serial}",
        )

    async def async_shutdown(self) -> None:
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
