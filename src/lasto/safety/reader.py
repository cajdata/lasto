"""Reads an open PCAN channel, watches bus state, and hands every item to subscribers.

In a polled session the reader is also a kill-switch trigger (rule 7): error
frames, error-passive or bus-off, receive overruns, interface failure, and a
subscriber that raises all trip the process kill switch. In a passive session listen-only mode
forces the controller error-passive, so bus state is only recorded. Instead
the reader re-reads listen-only on every status check, and at once whenever
the driver reports the controller (re)activated. If listen-only reads
anything but ON, or the interface fails, the reader stops trusting the
channel and stops; the session decides what happens next (rule 1).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from lasto.safety import pcan_constants as pc
from lasto.safety._frozen import SealedProtocolType, SealedType, freeze
from lasto.safety.audit import Auditor
from lasto.safety.clock import Clock
from lasto.safety.frames import ErrorFrame, ReadError, Received, StatusMessage
from lasto.safety.killswitch import KILL_SWITCH

STATUS_INTERVAL = 0.1

Subscriber = Callable[[Received], None]


class Channel(Protocol, metaclass=SealedProtocolType):
    def drain(self) -> list[Received]:
        """Everything waiting in the receive queue."""

    def status(self) -> int:
        """The channel's current PCAN status code."""

    def listen_only(self) -> bool:
        """Whether listen-only reads back as ON."""


class Reader(metaclass=SealedType):
    __slots__ = (
        "_auditor", "_channel", "_clock", "_failed_status", "_failure_reason", "_next_status", "_subscribers",
        "_trips_kill_switch", "_verify_listen_only",
    )  # fmt: skip

    def __init__(
        self,
        channel: Channel,
        clock: Clock,
        *,
        trips_kill_switch: bool = False,
        subscribers: Iterable[Subscriber] = (),
        verify_listen_only: bool = False,
        auditor: Auditor | None = None,
    ) -> None:
        self._channel = channel
        self._clock = clock
        self._trips_kill_switch = trips_kill_switch
        self._subscribers = list(subscribers)
        self._verify_listen_only = verify_listen_only
        self._auditor = auditor
        self._next_status = float("-inf")
        self._failure_reason: str | None = None
        self._failed_status: int | None = None

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    @property
    def failed_status(self) -> int | None:
        return self._failed_status

    @property
    def failed(self) -> bool:
        return self._failure_reason is not None

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
            self._check_listen_only()
        return items

    def _inspect(self, item: Received) -> None:
        if isinstance(item, ErrorFrame):
            self._trip("error_frame")
        elif isinstance(item, (StatusMessage, ReadError)):
            self._check_status(item.status)
            if isinstance(item, StatusMessage) and item.status == pc.PCAN_ERROR_OK and self._verify_listen_only:
                # The driver reports the controller (re)activated: check listen-only now, not at the next tick.
                self._check_listen_only()
                if not self.failed and self._auditor is not None:
                    self._auditor.event("listen_only_rechecked", trigger="controller_activated", listen_only=True)

    def _check_status(self, status: int) -> None:
        if status & pc.INTERFACE_FAILURE_BITS:
            self._fail("interface_failed", status)
        elif status & pc.OVERRUN_BITS:
            self._trip("receive_overrun")
        elif status & pc.BUS_FAULT_BITS:
            self._trip("bus_error_state")

    def _check_listen_only(self) -> None:
        if self._verify_listen_only and not self.failed and not self._channel.listen_only():
            self._fail("listen_only_lost", None)

    def _fail(self, reason: str, status: int | None) -> None:
        self._failure_reason = reason
        self._failed_status = status
        self._trip(reason)

    def _trip(self, cause: str) -> None:
        if self._trips_kill_switch:
            KILL_SWITCH.trip(cause)

    def _deliver(self, subscriber: Subscriber, item: Received) -> None:
        if not self._trips_kill_switch:
            subscriber(item)
            return
        try:
            subscriber(item)
        except Exception as exc:
            KILL_SWITCH.trip(f"subscriber_error:{type(exc).__name__}")


freeze(__name__)
