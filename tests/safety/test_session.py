"""Opening passive and polled sessions."""

import pytest
from helpers import CHANNEL, HANDLE, LOGGING_RPM_SPEED, events, open_polled

from lasto.safety import pcan_constants as pc
from lasto.safety.audit import Auditor
from lasto.safety.errors import KillSwitchTripped
from lasto.safety.exchange import ExchangeState
from lasto.safety.session import LISTEN_WINDOW, open_passive_session
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
