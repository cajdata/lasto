"""Opening passive and polled sessions."""

import pytest
from helpers import CHANNEL, HANDLE, LOGGING_RPM_SPEED, events, open_polled

from lasto.safety import pcan_constants as pc
from lasto.safety.audit import Auditor
from lasto.safety.clock import SystemClock
from lasto.safety.errors import KillSwitchTripped, SafetyViolation
from lasto.safety.exchange import ExchangeState
from lasto.safety.session import LISTEN_WINDOW, open_passive_session, open_polled_session
from lasto.sim.clock import FakeClock
from lasto.sim.pytest_plugin import HardwareFirewallError
from lasto.sim.tester import SimTester


class BrokenSink:
    def write(self, record):
        raise OSError("disk full")


def test_passive_session_records_everything_and_never_writes(sim, auditor, sink, clock):
    seen = []
    session = open_passive_session(CHANNEL, auditor=auditor, clock=clock, library=sim.dll, subscribers=[seen.append])
    clock.advance(0.5)
    items = session.pump()
    assert len(items) > 50 and seen == items
    assert session.reader is not None
    session.close()
    opened = events(sink, "session_opened")[0]
    assert (opened["mode"], opened["listen_only_confirmed"], opened["channel"]) == ("passive", True, CHANNEL)
    assert events(sink, "session_closed")[0]["mode"] == "passive"
    assert sim.dll.writes == []
    assert "CAN_Write" not in sim.dll.looked_up


def test_passive_session_closes_the_channel_if_the_audit_log_fails(sim, clock):
    with pytest.raises(OSError):
        open_passive_session(CHANNEL, auditor=Auditor(BrokenSink(), clock), clock=clock, library=sim.dll)
    assert not sim.dll.channel(HANDLE).initialized


def test_polled_session_listens_before_it_may_transmit(sim, auditor, sink):
    start = sim.clock.monotonic()
    session = open_polled(sim, auditor, listen_seconds=LISTEN_WINDOW)
    assert sim.clock.monotonic() - start >= LISTEN_WINDOW
    assert sim.dll.writes == []
    assert not session.killswitch.tripped
    assert session.reader is not None
    assert session.request(LOGGING_RPM_SPEED).state is ExchangeState.DONE
    session.close()
    assert events(sink, "session_closed")[0] == events(sink, "session_closed")[0] | {"mode": "polled", "kill_cause": None}
    assert not sim.dll.channel(HANDLE).initialized


def test_polled_session_refuses_to_start_when_another_tester_is_talking(sim, auditor):
    SimTester(sim.bus).request(0x7DF, b"\x01\x0c", delay=0.01)
    with pytest.raises(KillSwitchTripped) as tripped:
        open_polled(sim, auditor)
    assert tripped.value.cause == "foreign_tester"
    assert sim.dll.writes == []
    assert not sim.dll.channel(HANDLE).initialized


def test_polled_session_closes_the_channel_if_the_audit_log_fails(sim, clock):
    with pytest.raises(OSError):
        open_polled(sim, Auditor(BrokenSink(), clock))
    assert not sim.dll.channel(HANDLE).initialized


@pytest.mark.parametrize("mode", ["passive", "polled"])
def test_closing_a_session_records_whether_the_channel_really_closed(sim, auditor, sink, mode):
    """Finding #5."""
    if mode == "passive":
        session = open_passive_session(CHANNEL, auditor=auditor, clock=sim.clock, library=sim.dll)
    else:
        session = open_polled(sim, auditor)
    sim.dll.uninitialize_status = pc.PCAN_ERROR_ILLOPERATION
    session.close()
    [closed] = events(sink, "session_closed")
    assert closed["channel_uninitialized"] is False
    assert len(events(sink, "channel_close_failed")) == 1


def test_a_polled_channel_closed_after_a_kill_records_whether_it_really_closed(sim, auditor, sink):
    session = open_polled(sim, auditor)
    sim.dll.fail_set[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLOPERATION
    sim.dll.uninitialize_status = pc.PCAN_ERROR_ILLOPERATION
    session.killswitch.trip("hotkey")
    [closed] = events(sink, "polled_channel_closed")
    assert closed["uninitialized"] is False
    assert len(events(sink, "channel_close_failed")) == 1


@pytest.mark.parametrize("listen_seconds", [0, LISTEN_WINDOW - 0.01, -1.0, float("nan"), "2"])
def test_on_real_hardware_the_listen_window_has_a_minimum(auditor, sink, listen_seconds):
    """Finding D: with the real DLL, a polled session can't skip or shorten its listen for other testers."""
    with pytest.raises(SafetyViolation):  # refused before the DLL is even loaded
        open_polled_session(
            CHANNEL, profile=[LOGGING_RPM_SPEED], auditor=auditor, clock=SystemClock(), listen_seconds=listen_seconds
        )
    assert [r["reason"] for r in events(sink, "rejected")] == ["listen_window_too_short"]
    assert events(sink, "session_refused")[0]["reason"] == "listen_window_too_short"


def test_on_real_hardware_the_full_listen_window_passes_that_check(auditor):
    with pytest.raises(HardwareFirewallError):  # it gets as far as loading the DLL, which tests can't
        open_polled_session(
            CHANNEL, profile=[LOGGING_RPM_SPEED], auditor=auditor, clock=SystemClock(), listen_seconds=LISTEN_WINDOW
        )


class SlowerSystemClock(SystemClock):
    """Looks like the system clock, but a subclass could report any time it likes."""

    __slots__ = ()


def _polled(clock, auditor):
    open_polled_session(CHANNEL, profile=[LOGGING_RPM_SPEED], auditor=auditor, clock=clock)


def _passive(clock, auditor):
    open_passive_session(CHANNEL, auditor=auditor, clock=clock)


@pytest.mark.parametrize("opener", [_polled, _passive])
@pytest.mark.parametrize("make_clock", [FakeClock, SlowerSystemClock])
def test_on_real_hardware_timing_comes_from_the_system_clock(auditor, sink, opener, make_clock):
    """Finding N2: rate slots, the ceiling, the listen window, and reading ages can't run on a caller's clock."""
    with pytest.raises(SafetyViolation):  # refused before the DLL is even loaded
        opener(make_clock(), auditor)
    assert [r["reason"] for r in events(sink, "rejected")] == ["clock_not_the_system_clock"]
    assert events(sink, "session_refused")[0]["reason"] == "clock_not_the_system_clock"


@pytest.mark.parametrize("opener", [_polled, _passive])
def test_on_real_hardware_the_system_clock_passes_that_check(auditor, opener):
    with pytest.raises(HardwareFirewallError):  # as far as loading the DLL, which tests can't
        opener(SystemClock(), auditor)


def test_the_simulator_may_use_its_own_clock(sim, auditor):
    session = open_polled(sim, auditor)  # library= given: the simulated clock is the right one
    assert not session.killswitch.tripped


def test_kill_switches_the_channel_to_listen_only(sim, auditor, sink):
    session = open_polled(sim, auditor)
    session.killswitch.trip("hotkey")
    assert sim.dll.channel(HANDLE).listen_only == pc.PCAN_PARAMETER_ON
    record = events(sink, "kill_listen_only")[0]
    assert (record["cause"], record["listen_only_confirmed"]) == ("hotkey", True)
    with pytest.raises(KillSwitchTripped):
        session.request(LOGGING_RPM_SPEED)
    # Capture keeps running after a polled-mode kill.
    sim.clock.advance(0.1)
    assert session.pump()
