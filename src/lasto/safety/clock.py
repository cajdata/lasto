"""Time source for the safety core, injectable so tests and the simulator control time."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float:
        """Seconds from an arbitrary start; never goes backwards."""

    def utc_now(self) -> datetime:
        """Current wall-clock time in UTC."""

    def sleep(self, seconds: float) -> None:
        """Block for the given number of seconds (no-op for zero or less)."""


class SystemClock:
    """The real clock."""

    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))
