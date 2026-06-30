"""Data models for the Axis PACS VAPIX client.

This module is deliberately free of any Home Assistant imports so the client can
be exercised standalone (see ``scripts/devcheck.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class DoorMode(StrEnum):
    """ONVIF ``tdc:DoorMode`` — the logical lock state of a door.

    This is the authoritative lock-state source for Axis controllers; on units
    without physical door/lock monitors it is the only state reported.
    """

    UNKNOWN = "Unknown"
    LOCKED = "Locked"
    UNLOCKED = "Unlocked"
    ACCESSED = "Accessed"
    BLOCKED = "Blocked"
    LOCKED_DOWN = "LockedDown"
    LOCKED_OPEN = "LockedOpen"
    DOUBLE_LOCKED = "DoubleLocked"

    @classmethod
    def parse(cls, value: str | None) -> DoorMode:
        """Map a raw ONVIF string to a member, defaulting to ``UNKNOWN``."""
        if value:
            for member in cls:
                if member.value == value:
                    return member
        return cls.UNKNOWN


# Modes that mean the door is secured -> Home Assistant "locked".
LOCKED_MODES = frozenset(
    {DoorMode.LOCKED, DoorMode.BLOCKED, DoorMode.DOUBLE_LOCKED, DoorMode.LOCKED_DOWN}
)
# Modes that mean the door is released -> Home Assistant "unlocked".
# ``Accessed`` is the momentary buzz-in state; it auto-relocks shortly after.
UNLOCKED_MODES = frozenset(
    {DoorMode.UNLOCKED, DoorMode.LOCKED_OPEN, DoorMode.ACCESSED}
)


@dataclass(slots=True)
class DoorCapabilities:
    """Per-door capability flags from ``GetDoorInfoList``.

    Peer (remote) doors in a cluster report an *empty* ``<Capabilities/>``
    element; locally-owned doors report populated flags. See :meth:`is_empty`.
    """

    access: bool = False
    lock: bool = False
    unlock: bool = False
    block: bool = False
    double_lock: bool = False
    lock_down: bool = False
    lock_open: bool = False
    door_monitor: bool = False
    lock_monitor: bool = False
    double_lock_monitor: bool = False
    alarm: bool = False
    tamper: bool = False
    fault: bool = False
    warning: bool = False
    configurable: bool = False

    @classmethod
    def from_attrib(cls, attrib: dict[str, str]) -> DoorCapabilities:
        """Build from the XML attributes of a ``<tdc:Capabilities>`` element."""

        def flag(name: str) -> bool:
            return attrib.get(name, "false").lower() == "true"

        return cls(
            access=flag("Access"),
            lock=flag("Lock"),
            unlock=flag("Unlock"),
            block=flag("Block"),
            double_lock=flag("DoubleLock"),
            lock_down=flag("LockDown"),
            lock_open=flag("LockOpen"),
            door_monitor=flag("DoorMonitor"),
            lock_monitor=flag("LockMonitor"),
            double_lock_monitor=flag("DoubleLockMonitor"),
            alarm=flag("Alarm"),
            tamper=flag("Tamper"),
            fault=flag("Fault"),
            warning=flag("Warning"),
            configurable=flag("Configurable"),
        )

    @property
    def is_empty(self) -> bool:
        """True when no capability flags are set (a remote/peer door)."""
        return not any(
            (
                self.access,
                self.lock,
                self.unlock,
                self.block,
                self.double_lock,
                self.lock_down,
                self.lock_open,
                self.door_monitor,
                self.lock_monitor,
                self.double_lock_monitor,
                self.alarm,
                self.tamper,
                self.fault,
                self.warning,
                self.configurable,
            )
        )


@dataclass(slots=True)
class Door:
    """A door definition returned by ``GetDoorInfoList``."""

    token: str
    name: str
    description: str = ""
    capabilities: DoorCapabilities = field(default_factory=DoorCapabilities)

    @property
    def mac(self) -> str:
        """The owning controller's MAC, parsed from the door token.

        Token format is ``Axis-<mac>:<id>`` e.g.
        ``Axis-accc8e25cbd9:1704972801.338188000``.
        """
        head = self.token.split(":", 1)[0]
        if head.lower().startswith("axis-"):
            head = head[len("axis-") :]
        return head.lower()

    def is_local_to(self, serial: str) -> bool:
        """True when this door is owned by the controller with ``serial``.

        In a cluster, ``GetDoorInfoList`` returns every door across all peers;
        only doors whose token MAC equals this controller's serial are local.
        """
        return bool(self.mac) and self.mac == serial.lower()


@dataclass(slots=True)
class DoorState:
    """Runtime door state (currently just the logical mode)."""

    mode: DoorMode = DoorMode.UNKNOWN

    @property
    def is_locked(self) -> bool | None:
        """Tri-state lock status: True/False, or None when unknown."""
        if self.mode in LOCKED_MODES:
            return True
        if self.mode in UNLOCKED_MODES:
            return False
        return None


@dataclass(slots=True)
class DeviceIdentity:
    """Controller identity from ``param.cgi`` (Brand + Properties)."""

    serial: str
    model: str = ""
    product_full_name: str = ""
    product_short_name: str = ""
    firmware: str = ""
    product_type: str = ""


@dataclass(slots=True)
class User:
    """A cardholder — an Axis ``axudb`` user, the person a credential belongs to.

    The user/credential/profile database is replicated cluster-wide on Axis
    controllers, so users are *not* local to a single controller.
    """

    token: str
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    description: str = ""


@dataclass(slots=True)
class Credential:
    """An Axis access credential (``pacsaxis``): a person's PIN and/or card(s).

    ``id_data`` maps identifier names (``PIN``, ``Card``, ``CardNr``) to their raw
    values. The controller stores and returns PINs/card numbers in plain text.
    Like :class:`User`, credentials are cluster-wide, not per-controller.
    """

    token: str
    user_token: str = ""
    description: str = ""
    enabled: bool = False
    status: str = ""
    id_data: dict[str, str] = field(default_factory=dict)
    access_profile_tokens: list[str] = field(default_factory=list)

    @property
    def pin(self) -> str | None:
        return self.id_data.get("PIN") or None

    @property
    def card(self) -> str | None:
        return self.id_data.get("Card") or self.id_data.get("CardNr") or None

    @property
    def has_pin(self) -> bool:
        return bool(self.id_data.get("PIN"))


@dataclass(slots=True)
class TcrCredential:
    """A credential as seen through the ONVIF ``tcr`` service (same DB as
    :class:`Credential`, identical tokens) — the only view that carries the
    validity window. ``identifiers`` echoes the device's CredentialIdentifier
    list verbatim so a ModifyCredential can preserve PIN/card exactly.

    ``valid_from``/``valid_to`` are ISO-8601 strings (date-only is honoured;
    ``ValiditySupportsTimeValue=false``); empty/None means unbounded that end.
    """

    token: str
    description: str = ""
    holder: str = ""
    valid_from: str | None = None
    valid_to: str | None = None
    # Each: {"type": "pt:PIN"|"pt:Card", "format": "...", "exempted": "false",
    #        "value": "1234"} — preserved as-is on modify.
    identifiers: list[dict[str, str]] = field(default_factory=list)
    access_profile_tokens: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AccessPolicy:
    """One rule inside an :class:`AccessProfile`: a schedule at an access point."""

    schedule_token: str
    entity_token: str


@dataclass(slots=True)
class AccessProfile:
    """A named set of (schedule + access point) rules a credential can reference."""

    token: str
    name: str = ""
    description: str = ""
    policies: list[AccessPolicy] = field(default_factory=list)

    @property
    def entity_tokens(self) -> list[str]:
        return [p.entity_token for p in self.policies]


@dataclass(slots=True)
class Schedule:
    """A time schedule (``standard_always`` is the built-in always-on schedule)."""

    token: str
    name: str = ""
    description: str = ""


@dataclass(slots=True)
class AccessPoint:
    """A reader side of a door — the entity an :class:`AccessProfile` grants at.

    ``entity_token`` is the door this access point belongs to (when
    ``entity_type`` is ``tdc:Door``), so it maps an access point back to a door.
    """

    token: str
    name: str = ""
    description: str = ""
    entity_type: str = ""
    entity_token: str = ""

    @property
    def door_token(self) -> str:
        return self.entity_token if self.entity_type.endswith("Door") else ""


@dataclass(slots=True)
class Notification:
    """A single ONVIF event notification parsed from ``PullMessages``."""

    topic: str
    source: dict[str, str] = field(default_factory=dict)
    data: dict[str, str] = field(default_factory=dict)
    utc_time: str | None = None

    @property
    def door_token(self) -> str | None:
        return self.source.get("DoorToken")

    @property
    def state(self) -> str | None:
        return self.data.get("State")
