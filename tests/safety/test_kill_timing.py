"""Finding A: every kill trigger is seen before anything is sent.

A polled session reads everything already waiting (and asks for the bus
status) before each request goes out, keeps reading while it waits for a
rate-limit slot, and checks the interlocks again at the moment of sending.
"""

import pytest
from helpers import HANDLE, LOGGING_RPM_SPEED, GateHarness, open_polled

from lasto.safety import pcan_constants as pc
from lasto.safety import requests as rq
from lasto.safety.errors import KillSwitchTripped, SafetyViolation
from lasto.safety.gate import WAIT_SLICE
from lasto.safety.requests import DtcKind, Purpose
from lasto.sim.tester import SimTester

# ---- A: nothing is sent before what already arrived has been read ----


def test_an_error_frame_waiting_in_the_queue_stops_the_next_request(sim, auditor):
    session = open_polled(sim, auditor)
    sim.dll.inject_error_frame(HANDLE)  # arrived, not read yet
    with pytest.raises(KillSwitchTripped) as tripped:
        session.request(LOGGING_RPM_SPEED)
    assert tripped.value.cause == "error_frame"
    assert sim.dll.writes == []


def test_another_testers_request_waiting_on_the_bus_stops_the_next_request(sim, auditor):
    session = open_polled(sim, auditor)
    SimTester(sim.bus).request(0x7DF, b"\x01\x0c")
    with pytest.raises(KillSwitchTripped) as tripped:
        session.request(LOGGING_RPM_SPEED)
    assert tripped.value.cause == "foreign_tester"
    assert sim.dll.writes == []


def test_bus_off_shown_only_by_the_status_stops_the_next_request(sim, auditor):
    session = open_polled(sim, auditor)
    session.pump()  # the reader's regular status check just ran, so the next is 100 ms away
    sim.dll.channel(HANDLE).status = pc.PCAN_ERROR_BUSOFF  # no status message queued
    with pytest.raises(KillSwitchTripped) as tripped:
        session.request(LOGGING_RPM_SPEED)
    assert tripped.value.cause == "bus_error_state"
    assert sim.dll.writes == []


def test_a_tester_that_starts_during_the_rate_limit_wait_stops_the_frame(sim, auditor):
    session = open_polled(sim, auditor)
    session.request(LOGGING_RPM_SPEED)  # the next request now waits out the spacing
    SimTester(sim.bus).request(0x7DF, b"\x01\x0c", delay=0.02)  # arrives during that wait
    with pytest.raises(KillSwitchTripped) as tripped:
        session.request(LOGGING_RPM_SPEED)
    assert tripped.value.cause == "foreign_tester"
    assert len(sim.dll.writes) == 1


def test_the_gate_keeps_reading_while_it_waits_and_reads_last_right_before_sending(clock, auditor):
    h = GateHarness(clock, auditor)
    h.gate.submit(LOGGING_RPM_SPEED)
    h.gate.abort()
    h.limiter.backoff(clock.monotonic())  # the next slot is 0.2 s away
    h.drained.clear()
    start = clock.monotonic()
    h.gate.submit(LOGGING_RPM_SPEED)
    gaps = [later - earlier for earlier, later in zip([start, *h.drained], h.drained)]
    assert len(h.drained) >= 10 and max(gaps) <= WAIT_SLICE + 1e-9
    assert h.writer.times[-1] == h.drained[-1]  # nothing happened between the last read and the frame


def test_a_kill_during_the_wait_stops_it_at_once(clock, auditor):
    h = GateHarness(clock, auditor)
    h.gate.submit(LOGGING_RPM_SPEED)
    h.gate.abort()
    h.limiter.backoff(clock.monotonic())
    start = clock.monotonic()
    h.on_drain = lambda: h.killswitch.trip("error_frame") if clock.monotonic() - start > 0.05 else None
    with pytest.raises(KillSwitchTripped):
        h.gate.submit(LOGGING_RPM_SPEED)
    assert clock.monotonic() - start < 0.05 + WAIT_SLICE  # not the whole 0.2 s
    assert len(h.writer.frames) == 1


def test_the_interlocks_are_checked_again_when_the_frame_goes_out(clock, auditor):
    h = GateHarness(clock, auditor)
    h.park()  # speed 0, read now; a polled speed counts for 1 s
    clock.advance(0.5)
    for _ in range(3):
        h.limiter.backoff(clock.monotonic())  # the next slot is 0.8 s away
    with pytest.raises(SafetyViolation) as refused:
        h.gate.submit(rq.read_dtcs(DtcKind.STORED, purpose=Purpose.SNAPSHOT))
    assert refused.value.reason == "vehicle_not_confirmed_stationary"  # 1.3 s old by the time it would go out
    assert h.writer.frames == []
