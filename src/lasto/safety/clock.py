"""Time source for the safety core, injectable so tests and the simulator control time.

Only the simulator may bring its own clock. On real hardware the listen
window, the rate slots, the write function's ceiling, and how old an
interlock reading may be are all timed by SystemClock itself, not a
subclass or a look-alike (finding N2); require_system_clock() refuses
anything else before the DLL is loaded.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol

from lasto.safety._frozen import SealedProtocolType, SealedType, freeze
from lasto.safety.audit import refuse
from lasto.safety.errors import SafetyViolation


class Clock(Protocol, metaclass=SealedProtocolType):
    def monotonic(self) -> float:
        """Seconds from an arbitrary start; never goes backwards."""

    def utc_now(self) -> datetime:
        """Current wall-clock time in UTC."""

    def sleep(self, seconds: float) -> None:
        """Block for the given number of seconds (no-op for zero or less)."""


class SystemClock(metaclass=SealedType):
    """The real clock."""

    __slots__ = ()

    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


def require_system_clock(clock: object, *, request: str) -> None:
    """Refuse (audited) any clock but SystemClock itself. For real hardware; the simulator brings its own."""
    if type(clock) is not SystemClock:
        refuse(
            SafetyViolation("clock_not_the_system_clock", f"{type(clock).__name__}; real hardware runs on SystemClock"),
            transport="pcan",
            request=request,
        )


freeze(__name__)
