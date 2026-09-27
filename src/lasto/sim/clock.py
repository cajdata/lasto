"""A clock that only moves when told to, so simulated sessions are deterministic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

DEFAULT_UTC_START = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: float = 1000.0, utc_start: datetime = DEFAULT_UTC_START) -> None:
        self._start = start
        self._now = start
        self._utc_start = utc_start

    def monotonic(self) -> float:
        return self._now

    def utc_now(self) -> datetime:
        return self._utc_start + timedelta(seconds=self._now - self._start)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += max(0.0, seconds)
