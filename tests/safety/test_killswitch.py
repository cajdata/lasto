"""Rule 7: the kill switch latches for good; repeated NRCs and timeouts trip it."""

import pytest
from helpers import events

from lasto.safety.errors import KillSwitchTripped
from lasto.safety.killswitch import (
    CONSECUTIVE_TIMEOUT_LIMIT,
    DISCOVERY_NRC_LIMIT,
    KillSwitch,
    NrcMonitor,
)
from lasto.safety.requests import Purpose


def test_latches_and_notifies_once(auditor, sink):
    calls = []
    killswitch = KillSwitch(auditor)
    killswitch.add_listener(calls.append)
    assert not killswitch.tripped
    killswitch.check()
    assert killswitch.trip("hotkey") is True
    assert killswitch.trip("error_frame") is False
    assert (killswitch.tripped, killswitch.cause, calls) == (True, "hotkey", ["hotkey"])
    with pytest.raises(KillSwitchTripped) as tripped:
        killswitch.check()
    assert (tripped.value.cause, tripped.value.reason) == ("hotkey", "kill_switch")
    assert [record["cause"] for record in events(sink, "kill_switch")] == ["hotkey"]


def test_a_failing_listener_still_leaves_it_latched_and_audited(auditor, sink):
    killswitch = KillSwitch(auditor)

    def broken(cause):
        raise RuntimeError("listener bug")

    killswitch.add_listener(broken)
    with pytest.raises(RuntimeError):
        killswitch.trip("bus_error_state")
    assert killswitch.tripped
    assert len(events(sink, "kill_switch")) == 1


def test_without_an_auditor():
    killswitch = KillSwitch()
    assert killswitch.trip("hotkey")


def test_three_consecutive_nrcs_on_one_request_trip_it():
    killswitch = KillSwitch()
    monitor = NrcMonitor(killswitch)
    monitor.record_negative("a", 0x31, Purpose.LOGGING, 0.0)
    monitor.record_negative("a", 0x31, Purpose.LOGGING, 1.0)
    assert not killswitch.tripped
    monitor.record_negative("a", 0x31, Purpose.LOGGING, 2.0)
    assert killswitch.cause == "repeated_negative_responses"


def test_a_positive_answer_resets_the_consecutive_count():
    killswitch = KillSwitch()
    monitor = NrcMonitor(killswitch)
    for _ in range(4):
        monitor.record_negative("a", 0x22, Purpose.SNAPSHOT, 0.0)
        monitor.record_negative("a", 0x22, Purpose.SNAPSHOT, 0.0)
        monitor.record_positive("a", "engine")
        # also trim the window so only the consecutive rule is in play
        monitor._window.clear()
    assert not killswitch.tripped


def test_ten_nrcs_within_thirty_seconds_trip_it():
    killswitch = KillSwitch()
    monitor = NrcMonitor(killswitch)
    for i in range(9):
        monitor.record_negative(f"r{i}", 0x31, Purpose.LOGGING, float(i))
    assert not killswitch.tripped
    monitor.record_negative("r9", 0x31, Purpose.LOGGING, 9.0)
    assert killswitch.tripped


def test_old_nrcs_fall_out_of_the_window():
    killswitch = KillSwitch()
    monitor = NrcMonitor(killswitch)
    for i in range(20):
        monitor.record_negative(f"r{i}", 0x31, Purpose.LOGGING, i * 4.0)
    assert not killswitch.tripped


def test_discovery_not_supported_answers_have_their_own_limit():
    killswitch = KillSwitch()
    monitor = NrcMonitor(killswitch)
    for i in range(DISCOVERY_NRC_LIMIT - 1):
        monitor.record_negative("same", 0x31, Purpose.DISCOVERY, i * 0.1)
    assert not killswitch.tripped
    monitor.record_negative("same", 0x12, Purpose.DISCOVERY, 30.0)
    assert killswitch.tripped


def test_discovery_window_trims():
    killswitch = KillSwitch()
    monitor = NrcMonitor(killswitch)
    for i in range(DISCOVERY_NRC_LIMIT * 2):
        monitor.record_negative("same", 0x11, Purpose.DISCOVERY, i * 1.0)
    assert not killswitch.tripped


def test_other_nrcs_in_discovery_count_normally():
    killswitch = KillSwitch()
    monitor = NrcMonitor(killswitch)
    for _ in range(3):
        monitor.record_negative("x", 0x22, Purpose.DISCOVERY, 0.0)
    assert killswitch.tripped


def test_repeated_timeouts_trip_it_and_answers_reset_them():
    killswitch = KillSwitch()
    monitor = NrcMonitor(killswitch)
    for _ in range(CONSECUTIVE_TIMEOUT_LIMIT - 1):
        monitor.record_timeout("engine")
    monitor.record_positive("any", "engine")
    for _ in range(CONSECUTIVE_TIMEOUT_LIMIT - 1):
        monitor.record_timeout("engine")
    assert not killswitch.tripped
    monitor.record_timeout("engine")
    assert killswitch.cause == "repeated_timeouts"
