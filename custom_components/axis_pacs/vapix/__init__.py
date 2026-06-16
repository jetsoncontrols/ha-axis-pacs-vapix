"""Self-contained Axis VAPIX door-control client (Home Assistant independent)."""

from __future__ import annotations

from .client import AxisPacsClient, CannotConnect, InvalidAuth, VapixError
from .events import PullPointManager
from .models import (
    LOCKED_MODES,
    UNLOCKED_MODES,
    AccessPoint,
    AccessPolicy,
    AccessProfile,
    Credential,
    DeviceIdentity,
    Door,
    DoorCapabilities,
    DoorMode,
    DoorState,
    Notification,
    Schedule,
    User,
)

__all__ = [
    "LOCKED_MODES",
    "UNLOCKED_MODES",
    "AccessPoint",
    "AccessPolicy",
    "AccessProfile",
    "AxisPacsClient",
    "CannotConnect",
    "Credential",
    "DeviceIdentity",
    "Door",
    "DoorCapabilities",
    "DoorMode",
    "DoorState",
    "InvalidAuth",
    "Notification",
    "PullPointManager",
    "Schedule",
    "User",
    "VapixError",
]
