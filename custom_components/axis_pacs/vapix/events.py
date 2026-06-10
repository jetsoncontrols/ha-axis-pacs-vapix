"""ONVIF WS-Eventing PullPoint manager — live door events without polling.

Runs a long-lived loop that creates a PullPoint subscription, long-polls
``PullMessages``, and dispatches each notification to a callback. It renews the
subscription periodically and rebuilds it (with a state resync) on any failure,
so a device reboot or dropped subscription self-heals. Home Assistant
independent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .client import AxisPacsClient, VapixError
from .models import Notification

_LOGGER = logging.getLogger(__name__)

# Subscription lifetime requested from the device; renewed well within it.
INITIAL_TERMINATION = "PT600S"
RENEW_TERMINATION = "PT600S"
RENEW_INTERVAL = 300.0  # seconds between Renew calls
# Server-side long-poll: returns as soon as events arrive, else after Timeout.
PULL_TIMEOUT = "PT30S"
PULL_LIMIT = 40
PULL_HTTP_TIMEOUT = 45.0  # must exceed the server PULL_TIMEOUT
RECONNECT_BACKOFF = 10.0

EventCallback = Callable[[Notification], None]
ResyncCallback = Callable[[], Awaitable[None]]


class PullPointManager:
    """Owns a PullPoint subscription and the pull loop."""

    def __init__(
        self,
        client: AxisPacsClient,
        on_event: EventCallback,
        on_resync: ResyncCallback,
    ) -> None:
        self._client = client
        self._on_event = on_event
        self._on_resync = on_resync
        self._address: str | None = None
        self._sub_id: str | None = None
        self._running = True

    async def async_run(self) -> None:
        """Long-lived loop. Intended to run as a background task (cancellable)."""
        last_renew = 0.0
        while self._running:
            try:
                if self._sub_id is None:
                    self._sub_id, self._address = (
                        await self._client.async_create_pull_point(INITIAL_TERMINATION)
                    )
                    last_renew = time.monotonic()
                    _LOGGER.debug("PullPoint subscription %s established", self._sub_id)
                    # Re-read current state so we don't miss transitions that
                    # happened while we were disconnected.
                    await self._on_resync()

                notifications = await self._client.async_pull_messages(
                    self._address, self._sub_id, PULL_TIMEOUT, PULL_LIMIT, PULL_HTTP_TIMEOUT
                )
                for notification in notifications:
                    self._on_event(notification)

                if time.monotonic() - last_renew > RENEW_INTERVAL:
                    await self._client.async_renew(
                        self._address, self._sub_id, RENEW_TERMINATION
                    )
                    last_renew = time.monotonic()
            except asyncio.CancelledError:
                raise
            except VapixError as err:
                _LOGGER.debug("PullPoint error; will resubscribe: %s", err)
                self._sub_id = None
                await asyncio.sleep(RECONNECT_BACKOFF)
            except Exception:  # noqa: BLE001 - keep the loop alive on anything
                _LOGGER.exception("Unexpected PullPoint error; will resubscribe")
                self._sub_id = None
                await asyncio.sleep(RECONNECT_BACKOFF)

    async def async_stop(self) -> None:
        """Stop the loop and best-effort unsubscribe."""
        self._running = False
        if self._sub_id and self._address:
            try:
                await self._client.async_unsubscribe(self._address, self._sub_id)
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                pass
            self._sub_id = None
