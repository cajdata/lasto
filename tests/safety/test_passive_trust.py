"""Rule 1, continued: a passive session never trusts a channel that changed under it.

Listen-only is re-read on every status check. If it reads anything but ON,
or the channel fails, resets, or reconnects, the session logs why, closes the
channel (so the driver's automatic resume can't carry the capture on), and
either reopens it through the full open_passive sequence or ends.
"""

import pytest
from helpers import CHANNEL, HANDLE, events

from lasto.safety import pcan_constants as pc
from lasto.safety import session as session_module
from lasto.safety.clock import SystemClock
from lasto.safety.reader import STATUS_INTERVAL
from lasto.safety.session import MAX_REOPENS_PER_SESSION, REOPEN_ATTEMPTS, open_passive_session
from lasto.sim.pytest_plugin import HardwareFirewallError

OFF, ON = pc.PCAN_PARAMETER_OFF, pc.PCAN_PARAMETER_ON


def passive(sim, auditor):
    return open_passive_session(CHANNEL, auditor=auditor, clock=sim.clock, library=sim.dll)


def run(session, sim, seconds=0.3):
    frames = []
    end = sim.clock.monotonic() + seconds
    while sim.clock.monotonic() < end:
        frames += session.pump()
        sim.clock.advance(0.01)
    return frames


def assert_reopened_cleanly(sim, session, sink):
    state = sim.dll.channel(HANDLE)
    assert state.initialized and state.listen_only == ON
    assert state.listen_only_at_initialize == ON  # set before initializing, like the first time
    assert events(sink, "passive_channel_reopened")[-1]["listen_only_confirmed"] is True
    assert not session.ended
    assert sim.dll.writes == []


def test_listen_only_silently_turning_off_is_caught_and_the_channel_reopened(sim, auditor, sink):
    session = passive(sim, auditor)
    run(session, sim)
    sim.dll.channel(HANDLE).listen_only = OFF  # the controller left listen-only without any error
    run(session, sim, STATUS_INTERVAL * 2)
    [distrusted] = events(sink, "passive_channel_distrusted")
    assert distrusted["reason"] == "listen_only_lost"
    assert sim.dll.calls.count("CAN_Uninitialize") == 1  # closed, not resumed
    assert_reopened_cleanly(sim, session, sink)
    assert session.reopens == 1
    assert len(run(session, sim)) > 50  # capture continues


def test_a_failed_listen_only_readback_counts_as_lost(sim, auditor, sink):
    session = passive(sim, auditor)
    sim.dll.fail_get[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLPARAMTYPE
    original_sleep = sim.clock.sleep

    def sleep_and_recover(seconds):
        original_sleep(seconds)
        sim.dll.fail_get.pop(pc.PCAN_LISTEN_ONLY, None)  # the readback works again after the pause

    sim.clock.sleep = sleep_and_recover
    sim.clock.advance(STATUS_INTERVAL)
    session.pump()
    assert events(sink, "passive_channel_distrusted")[0]["reason"] == "listen_only_lost"
    assert_reopened_cleanly(sim, session, sink)


def test_a_readback_that_keeps_failing_ends_the_session(sim, auditor, sink):
    session = passive(sim, auditor)
    sim.dll.fail_get[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLPARAMTYPE
    sim.clock.advance(STATUS_INTERVAL)
    session.pump()
    failures = events(sink, "passive_reopen_failed")
    assert {f["reason"] for f in failures} == {"PassiveModeUnconfirmed"}  # each reopen refused to trust it
    assert session.ended
    assert not sim.dll.channel(HANDLE).initialized  # nothing left running


def test_driver_resume_after_a_replug_without_listen_only_is_never_trusted(sim, auditor, sink):
    session = passive(sim, auditor)
    run(session, sim)
    sim.dll.driver_resumes(HANDLE, listen_only=OFF, announce=True)
    session.pump()  # the "controller activated" message triggers the check at once, not at the next tick
    assert events(sink, "passive_channel_distrusted")[0]["reason"] == "listen_only_lost"
    assert_reopened_cleanly(sim, session, sink)


def test_controller_activated_with_listen_only_still_on_is_logged_and_capture_continues(sim, auditor, sink):
    session = passive(sim, auditor)
    run(session, sim)
    sim.dll.driver_resumes(HANDLE, listen_only=ON, announce=True)
    run(session, sim)
    assert events(sink, "listen_only_rechecked")[0]["trigger"] == "controller_activated"
    assert events(sink, "passive_channel_distrusted") == []
    assert session.reopens == 0


def test_unplug_ends_the_session_when_the_adapter_does_not_come_back(sim, auditor, sink):
    session = passive(sim, auditor)
    run(session, sim)
    sim.dll.unplug(HANDLE)
    run(session, sim)
    distrusted = events(sink, "passive_channel_distrusted")[0]
    assert (distrusted["reason"], distrusted["status"]) == ("interface_failed", "0x1400")
    failures = events(sink, "passive_reopen_failed")
    assert [f["attempt"] for f in failures] == list(range(1, REOPEN_ATTEMPTS + 1))
    assert session.ended and "could not reopen" in session.end_reason
    assert events(sink, "session_ended")[0]["reason"] == session.end_reason
    assert session.pump() == []
    session.close()  # already ended: nothing more happens
    assert events(sink, "session_closed") == []


def test_unplug_and_replug_reopens_from_scratch(sim, auditor, sink):
    session = passive(sim, auditor)
    run(session, sim)
    sim.dll.unplug(HANDLE)
    sleeps = []
    original_sleep = sim.clock.sleep

    def sleep_and_replug(seconds):
        sleeps.append(seconds)
        original_sleep(seconds)
        if len(sleeps) == 2:
            sim.dll.plug_back(HANDLE)  # back before the second attempt

    sim.clock.sleep = sleep_and_replug
    session.pump()
    session.pump()
    assert sleeps == [session_module.REOPEN_DELAY, session_module.REOPEN_DELAY]  # never a tight loop
    assert len(events(sink, "passive_reopen_failed")) == 1
    assert events(sink, "passive_channel_reopened")[0]["attempt"] == 2
    assert_reopened_cleanly(sim, session, sink)


def test_a_flapping_channel_ends_the_session_after_the_per_session_cap(sim, auditor, sink):
    session = passive(sim, auditor)
    for _ in range(MAX_REOPENS_PER_SESSION + 1):
        sim.dll.channel(HANDLE).listen_only = OFF
        sim.clock.advance(STATUS_INTERVAL)
        session.pump()
    assert session.reopens == MAX_REOPENS_PER_SESSION
    assert session.ended and "reopened 10 times" in session.end_reason
    assert len(events(sink, "passive_channel_distrusted")) == MAX_REOPENS_PER_SESSION + 1


def test_a_channel_that_will_not_close_ends_the_session_without_reopening(sim, auditor, sink):
    """Finding #5: if the untrusted channel can't be uninitialized it may still be on the bus; say so and stop."""
    session = passive(sim, auditor)
    sim.dll.uninitialize_status = pc.PCAN_ERROR_ILLOPERATION
    sim.dll.channel(HANDLE).listen_only = OFF
    sim.clock.advance(STATUS_INTERVAL)
    session.pump()
    assert session.ended and "could not close" in session.end_reason
    assert len(events(sink, "channel_close_failed")) == 1
    assert events(sink, "passive_reopen_failed") == []  # nothing is reopened on top of a channel still open


def test_close_and_properties(sim, auditor, sink):
    session = passive(sim, auditor)
    assert (session.ended, session.end_reason, session.reopens) == (False, None, 0)
    assert session.reader is not None
    session.close()
    session.close()
    assert (session.ended, session.end_reason) == (True, "closed")
    assert len(events(sink, "session_closed")) == 1


def test_default_binding_is_the_real_dll_which_tests_cannot_reach(auditor, sink):
    with pytest.raises(HardwareFirewallError):
        open_passive_session(CHANNEL, auditor=auditor, clock=SystemClock())
    assert events(sink, "session_refused")[0]["reason"] == "HardwareFirewallError"
