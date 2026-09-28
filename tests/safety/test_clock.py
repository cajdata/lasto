"""Finding P3: the system clock can't be steered by rebinding the time module's functions.

SystemClock times every rule on real hardware: the listen window, the rate
slots, the write function's ceiling, and how old an interlock reading may be.
It looked time.monotonic and time.sleep up on the time module at every call,
so a line like `time.monotonic = ...` anywhere in the process would have held
an old speed-0 reading fresh, or loosened the pacing and the ceiling.
"""

from __future__ import annotations

import time

from lasto.safety.clock import SystemClock

real_monotonic = time.monotonic


def test_a_stopped_time_monotonic_does_not_stop_the_system_clock(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    clock = SystemClock()
    first = clock.monotonic()
    time.sleep(0.02)
    assert clock.monotonic() - first >= 0.015
    assert abs(clock.monotonic() - real_monotonic()) < 0.5


def test_a_fast_time_monotonic_does_not_speed_up_the_system_clock(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() * 1000)
    assert abs(SystemClock().monotonic() - real_monotonic()) < 0.5


def test_a_time_sleep_that_does_nothing_does_not_skip_the_system_clocks_waits(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    start = real_monotonic()
    SystemClock().sleep(0.05)
    assert real_monotonic() - start >= 0.04


def test_the_system_clock_still_tells_time():
    clock = SystemClock()
    start = clock.monotonic()
    clock.sleep(0.02)
    clock.sleep(-1.0)  # nothing to wait for
    assert clock.monotonic() - start >= 0.015
    assert clock.utc_now().tzinfo is not None
