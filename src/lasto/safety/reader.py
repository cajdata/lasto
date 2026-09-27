"""Reads an open PCAN channel, watches bus state, and hands every item to subscribers.

In a polled session the reader is also a kill-switch trigger (rule 7): error
frames, error-passive or bus-off, receive overruns, interface failure, and a
subscriber that raises all trip it. In a passive session listen-only mode
forces the controller error-passive, so bus state is only recorded, and an
interface failure stops the reader.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from lasto.safety import pcan_constants as pc
from lasto.safety.clock import Clock
from lasto.safety.frames import ErrorFrame, ReadError, Received, StatusMessage
from lasto.safety.killswitch import KillSwitch

STATUS_INTERVAL = 0.25

Subscriber = Callable[[Received], None]


class Channel(Protocol):
    def drain(self) -> list[Received]:
        """Everything waiting in the receive queue."""

    def status(self) -> int:
        """The channel's current PCAN status code."""


class Reader:
    def __init__(
        self,
        channel: Channel,
        clock: Clock,
        *,
        killswitch: KillSwitch | None = None,
        subscribers: Iterable[Subscriber] = (),
    ) -> None:
        self._channel = channel
        self._clock = clock
        self._killswitch = killswitch
        self._subscribers = list(subscribers)
        self._next_status = float("-inf")
        self.failed_status: int | None = None

    @property
    def failed(self) -> bool:
        return self.failed_status is not None

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def poll_once(self) -> list[Received]:
        if self.failed:
            return []
        items = self._channel.drain()
        for item in items:
            self._inspect(item)
            for subscriber in self._subscribers:
                self._deliver(subscriber, item)
        now = self._clock.monotonic()
        if not self.failed and now >= self._next_status:
            self._next_status = now + STATUS_INTERVAL
            self._check_status(self._channel.status())
        return items

    def _inspect(self, item: Received) -> None:
        if isinstance(item, ErrorFrame):
            self._trip("error_frame")
        elif isinstance(item, (StatusMessage, ReadError)):
            self._check_status(item.status)

    def _check_status(self, status: int) -> None:
        if status & pc.INTERFACE_FAILURE_BITS:
            self.failed_status = status
            self._trip("interface_failed")
        elif status & pc.OVERRUN_BITS:
            self._trip("receive_overrun")
        elif status & pc.BUS_FAULT_BITS:
            self._trip("bus_error_state")

    def _trip(self, cause: str) -> None:
        if self._killswitch is not None:
            self._killswitch.trip(cause)

    def _deliver(self, subscriber: Subscriber, item: Received) -> None:
        if self._killswitch is None:
            subscriber(item)
            return
        try:
            subscriber(item)
        except Exception as exc:
            self._killswitch.trip(f"subscriber_error:{type(exc).__name__}")
