"""The kill switch and the negative-response counter that feeds it (rule 7).

There is one kill switch for the whole process, KILL_SWITCH. Once tripped,
it stays tripped for the life of the process: nothing retries, and no
polled session can open again until lasto restarts. The gate, the write
function, the polled reader, the NRC monitor, and the hotkey all use it
directly; none takes a kill switch as a parameter, so none can be handed a
fresh one. Passive capture keeps running, because it can't transmit.

Every trip reaches every open audit log (or the next one, if none is open).
The only reset is reset_for_tests(), for the test suite, which runs as one
process: it works only while the test hardware firewall is installed, and
every call is audited.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Hashable

from lasto.safety._frozen import SealedType, freeze
from lasto.safety.audit import REFUSALS, refuse
from lasto.safety.errors import KillSwitchTripped, SafetyViolation
from lasto.safety.pcan_dll import hardware_firewall_installed
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


class KillSwitch(metaclass=SealedType):
    __slots__ = ("_cause", "_listeners", "_lock")

    def __init__(self) -> None:
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

    def remove_listener(self, listener: Callable[[str], None]) -> None:
        """Stop notifying a listener (a session that closed). Removing one that isn't there does nothing."""
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def trip(self, cause: str) -> bool:
        """Latch, notify every listener, and audit. Returns False if it was already tripped.

        Every listener runs even if one raises (one session's failure can't keep another in
        normal mode); the first error is raised afterwards.
        """
        with self._lock:
            if self._cause is not None:
                return False
            self._cause = cause
            listeners = list(self._listeners)
        errors: list[Exception] = []
        try:
            for listener in listeners:
                try:
                    listener(cause)
                except Exception as exc:
                    errors.append(exc)
        finally:
            REFUSALS.event("kill_switch", cause=cause)
        if errors:
            raise errors[0]
        return True

    def check(self, request: str = "") -> None:
        """Refuse (audited) if tripped."""
        cause = self._cause
        if cause is not None:
            refuse(KillSwitchTripped(cause), transport="pcan", request=request)

    def _reset(self) -> None:
        with self._lock:
            previous, self._cause = self._cause, None
            self._listeners.clear()
        REFUSALS.event("kill_switch_reset", previous_cause=previous)


# The one kill switch for the process.
KILL_SWITCH = KillSwitch()


def _check_reset_allowed(*, firewall_installed: bool) -> None:
    if not firewall_installed:
        refuse(
            SafetyViolation(
                "kill_switch_reset_refused", "the kill switch resets only in a test run with the hardware firewall"
            ),
            transport="core",
            request="reset the kill switch",
        )


def reset_for_tests() -> None:
    """Clear KILL_SWITCH between tests. Audited; refused (and audited) outside a firewalled test run.

    Only lasto.sim.pytest_plugin calls this; tests/safety/test_killswitch.py holds that and proves the guards.
    """
    _check_reset_allowed(firewall_installed=hardware_firewall_installed())
    KILL_SWITCH._reset()


def _trim(window: deque[float], now: float, span: float) -> None:
    while window and now - window[0] > span:
        window.popleft()


class NrcMonitor(metaclass=SealedType):
    """Counts negative responses and timeouts, and trips the kill switch when they repeat."""

    __slots__ = ("_consecutive", "_discovery_window", "_timeouts", "_window")

    def __init__(self) -> None:
        self._consecutive: dict[Hashable, int] = {}
        self._window: deque[float] = deque()
        self._discovery_window: deque[float] = deque()
        self._timeouts: dict[Hashable, int] = {}

    def record_negative(self, request_key: Hashable, nrc: int, purpose: Purpose, now: float) -> None:
        if purpose is Purpose.DISCOVERY and nrc in DISCOVERY_NOT_SUPPORTED:
            self._discovery_window.append(now)
            _trim(self._discovery_window, now, DISCOVERY_WINDOW_SECONDS)
            if len(self._discovery_window) >= DISCOVERY_NRC_LIMIT:
                KILL_SWITCH.trip("repeated_negative_responses")
            return
        count = self._consecutive.get(request_key, 0) + 1
        self._consecutive[request_key] = count
        self._window.append(now)
        _trim(self._window, now, NRC_WINDOW_SECONDS)
        if count >= CONSECUTIVE_NRC_LIMIT or len(self._window) >= WINDOW_NRC_LIMIT:
            KILL_SWITCH.trip("repeated_negative_responses")

    def record_positive(self, request_key: Hashable, target_key: Hashable) -> None:
        self._consecutive.pop(request_key, None)
        self._timeouts.pop(target_key, None)

    def record_timeout(self, target_key: Hashable) -> None:
        count = self._timeouts.get(target_key, 0) + 1
        self._timeouts[target_key] = count
        if count >= CONSECUTIVE_TIMEOUT_LIMIT:
            KILL_SWITCH.trip("repeated_timeouts")


freeze(__name__)
