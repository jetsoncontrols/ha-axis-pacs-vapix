"""Lock platform — one entity per door local to the controller."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import AxisPacsConfigEntry, AxisPacsCoordinator
from .vapix import Door, DoorMode, DoorState, VapixError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AxisPacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a lock entity for each local door."""
    coordinator = entry.runtime_data
    async_add_entities(
        AxisPacsDoorLock(coordinator, door) for door in coordinator.doors.values()
    )


class AxisPacsDoorLock(CoordinatorEntity[AxisPacsCoordinator], LockEntity):
    """Represents the lock state of a single Axis door."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AxisPacsCoordinator, door: Door) -> None:
        super().__init__(coordinator)
        self._token = door.token
        identity = coordinator.identity
        self._attr_unique_id = f"{identity.serial}_{door.token}"
        self._attr_name = door.name or None
        # ``AccessDoor`` (momentary unlatch) is exposed via the OPEN feature.
        if door.capabilities.access:
            self._attr_supported_features = LockEntityFeature.OPEN
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity.serial)},
            connections={(CONNECTION_NETWORK_MAC, format_mac(identity.serial))},
            manufacturer=MANUFACTURER,
            model=identity.product_full_name or identity.model or None,
            name=identity.product_full_name or f"Axis {identity.serial}",
            sw_version=identity.firmware or None,
            serial_number=identity.serial,
            configuration_url=coordinator.client.base_url,
        )

    @property
    def _door_state(self) -> DoorState:
        return self.coordinator.data.get(self._token, DoorState())

    @property
    def is_locked(self) -> bool | None:
        return self._door_state.is_locked

    async def async_lock(self, **kwargs: Any) -> None:
        await self._command(self.coordinator.client.async_lock, DoorMode.LOCKED)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._command(self.coordinator.client.async_unlock, DoorMode.UNLOCKED)

    async def async_open(self, **kwargs: Any) -> None:
        await self._command(self.coordinator.client.async_access, DoorMode.ACCESSED)

    async def _command(self, func, optimistic_mode: DoorMode) -> None:
        try:
            await func(self._token)
        except VapixError as err:
            raise HomeAssistantError(f"Axis door command failed: {err}") from err
        self.coordinator.set_door_mode(self._token, optimistic_mode)
