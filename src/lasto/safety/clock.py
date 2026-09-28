"""Time source for the safety core, injectable so tests and the simulator control time.

Only the simulator may bring its own clock. On real hardware the listen
window, the rate slots, the write function's ceiling, and how old an
interlock reading may be are all timed by SystemClock itself, not a
subclass or a look-alike (finding N2); require_system_clock() refuses
anything else before the DLL is loaded. SystemClock keeps its own
references to time.monotonic and time.sleep, so rebinding those on the
time module doesn't steer it (finding P3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic as _monotonic
from time import sleep as _sleep
from typing import Protocol

from lasto.safety._frozen import SealedProtocolType, SealedType, freeze
from lasto.safety.audit import refuse
from lasto.safety.errors import SafetyViolation

# _monotonic and _sleep are taken once, at import, so code elsewhere that rebinds time.monotonic or time.sleep
# can't steer the safety core's timing (finding P3). This module is frozen, so they can't be rebound here either.


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
        return _monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        _sleep(max(0.0, seconds))


def require_system_clock(clock: object, *, request: str) -> None:
    """Refuse (audited) any clock but SystemClock itself. For real hardware; the simulator brings its own."""
    if type(clock) is not SystemClock:
        refuse(
            SafetyViolation("clock_not_the_system_clock", f"{type(clock).__name__}; real hardware runs on SystemClock"),
            transport="pcan",
            request=request,
        )


freeze(__name__)
