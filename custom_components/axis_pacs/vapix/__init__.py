"""Self-contained Axis VAPIX door-control client (Home Assistant independent)."""

from __future__ import annotations

from .client import AxisPacsClient, CannotConnect, InvalidAuth, VapixError
from .events import PullPointManager
from .models import (
    LOCKED_MODES,
    UNLOCKED_MODES,
    DeviceIdentity,
    Door,
    DoorCapabilities,
    DoorMode,
    DoorState,
    Notification,
)

__all__ = [
    "LOCKED_MODES",
    "UNLOCKED_MODES",
    "AxisPacsClient",
    "CannotConnect",
    "DeviceIdentity",
    "Door",
    "DoorCapabilities",
    "DoorMode",
    "DoorState",
    "InvalidAuth",
    "Notification",
    "PullPointManager",
    "VapixError",
]
