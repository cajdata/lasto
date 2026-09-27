"""Request pacing with a hard ceiling (rule 6).

Every purpose has its own rate, none of which may exceed the ceiling, and all
requests together are also held to the ceiling. A busy-repeat negative
response (NRC 0x21) adds an exponential back-off.
"""

from __future__ import annotations

from lasto.safety.requests import Purpose

HARD_CEILING_PER_SECOND = 20.0

PURPOSE_RATES = {
    Purpose.LOGGING: 20.0,
    Purpose.SNAPSHOT: 5.0,
    Purpose.IDENTIFY: 5.0,
    Purpose.DISCOVERY: 5.0,
    Purpose.INTERLOCK_PROBE: 2.0,
}

BACKOFF_START = 0.2
BACKOFF_MAX = 5.0


class RateLimiter:
    def __init__(self, rates: dict[Purpose, float] | None = None) -> None:
        rates = PURPOSE_RATES if rates is None else rates
        self._interval: dict[Purpose, float] = {}
        for purpose in Purpose:
            rate = rates[purpose]
            if not 0 < rate <= HARD_CEILING_PER_SECOND:
                raise ValueError(f"{purpose.value} rate {rate}/s must be above 0 and at most {HARD_CEILING_PER_SECOND}/s")
            self._interval[purpose] = 1.0 / rate
        self._next_any = float("-inf")
        self._next = dict.fromkeys(Purpose, float("-inf"))
        self._backoff = 0.0
        self._backoff_until = float("-inf")

    def delay(self, purpose: Purpose, now: float) -> float:
        """Seconds until a request for this purpose may be sent."""
        return max(0.0, self._next_any - now, self._next[purpose] - now, self._backoff_until - now)

    def commit(self, purpose: Purpose, now: float) -> None:
        self._next_any = now + 1.0 / HARD_CEILING_PER_SECOND
        self._next[purpose] = now + self._interval[purpose]

    def backoff(self, now: float) -> None:
        self._backoff = BACKOFF_START if self._backoff == 0 else min(BACKOFF_MAX, self._backoff * 2)
        self._backoff_until = now + self._backoff

    def clear_backoff(self) -> None:
        self._backoff = 0.0
