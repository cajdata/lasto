"""The kill switch and the negative-response counter that feeds it (rule 7).

Once tripped, the kill switch stays tripped for the life of the process.
Nothing resets it and nothing retries.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Hashable

from lasto.safety.audit import Auditor, refuse
from lasto.safety.errors import KillSwitchTripped
from lasto.safety.requests import Purpose

CONSECUTIVE_NRC_LIMIT = 3
WINDOW_NRC_LIMIT = 10
NRC_WINDOW_SECONDS = 30.0

# In discovery, "not supported" answers are expected results, so they count against their own
# larger limit. Revisit in Phase 6.
DISCOVERY_NOT_SUPPORTED = frozenset({0x11, 0x12, 0x31})
DISCOVERY_NRC_LIMIT = 300
DISCOVERY_WINDOW_SECONDS = 60.0

CONSECUTIVE_TIMEOUT_LIMIT = 5


class KillSwitch:
    def __init__(self, auditor: Auditor | None = None) -> None:
        self._auditor = auditor
        self._lock = threading.Lock()
        self._cause: str | None = None
        self._listeners: list[Callable[[str], None]] = []

    @property
    def tripped(self) -> bool:
        return self._cause is not None

    @property
    def cause(self) -> str | None:
        return self._cause

    def add_listener(self, listener: Callable[[str], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def trip(self, cause: str) -> bool:
        """Latch and notify listeners. Returns False if it was already tripped."""
        with self._lock:
            if self._cause is not None:
                return False
            self._cause = cause
            listeners = list(self._listeners)
        try:
            for listener in listeners:
                listener(cause)
        finally:
            if self._auditor is not None:
                self._auditor.event("kill_switch", cause=cause)
        return True

    def check(self, request: str = "") -> None:
        """Refuse (audited) if tripped."""
        cause = self._cause
        if cause is not None:
            refuse(KillSwitchTripped(cause), transport="pcan", request=request)


def _trim(window: deque[float], now: float, span: float) -> None:
    while window and now - window[0] > span:
        window.popleft()


class NrcMonitor:
    """Counts negative responses and timeouts, and trips the kill switch when they repeat."""

    def __init__(self, killswitch: KillSwitch) -> None:
        self._killswitch = killswitch
        self._consecutive: dict[Hashable, int] = {}
        self._window: deque[float] = deque()
        self._discovery_window: deque[float] = deque()
        self._timeouts: dict[Hashable, int] = {}

    def record_negative(self, request_key: Hashable, nrc: int, purpose: Purpose, now: float) -> None:
        if purpose is Purpose.DISCOVERY and nrc in DISCOVERY_NOT_SUPPORTED:
            self._discovery_window.append(now)
            _trim(self._discovery_window, now, DISCOVERY_WINDOW_SECONDS)
            if len(self._discovery_window) >= DISCOVERY_NRC_LIMIT:
                self._killswitch.trip("repeated_negative_responses")
            return
        count = self._consecutive.get(request_key, 0) + 1
        self._consecutive[request_key] = count
        self._window.append(now)
        _trim(self._window, now, NRC_WINDOW_SECONDS)
        if count >= CONSECUTIVE_NRC_LIMIT or len(self._window) >= WINDOW_NRC_LIMIT:
            self._killswitch.trip("repeated_negative_responses")

    def record_positive(self, request_key: Hashable, target_key: Hashable) -> None:
        self._consecutive.pop(request_key, None)
        self._timeouts.pop(target_key, None)

    def record_timeout(self, target_key: Hashable) -> None:
        count = self._timeouts.get(target_key, 0) + 1
        self._timeouts[target_key] = count
        if count >= CONSECUTIVE_TIMEOUT_LIMIT:
            self._killswitch.trip("repeated_timeouts")
