"""Rule 7: the kill switch latches for the life of the process; repeated NRCs and timeouts trip it."""

import importlib
import json
import subprocess
import sys

import pytest
from helpers import CHANNEL, events, open_polled
from scan import SRC, TESTS, sources, uses

from lasto.safety.audit import REFUSALS
from lasto.safety.errors import KillSwitchTripped, SafetyViolation
from lasto.safety.killswitch import (
    CONSECUTIVE_TIMEOUT_LIMIT,
    DISCOVERY_NRC_LIMIT,
    KILL_SWITCH,
    KillSwitch,
    NrcMonitor,
)
from lasto.safety.requests import Purpose
from lasto.safety.session import open_passive_session
from lasto.sim.pytest_plugin import fresh_kill_switch
from lasto.sim.vehicle import build_sim


def test_latches_and_notifies_once(auditor, sink):
    REFUSALS.attach(auditor)
    calls = []
    killswitch = KillSwitch()
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
    REFUSALS.attach(auditor)
    killswitch = KillSwitch()

    def broken(cause):
        raise RuntimeError("listener bug")

    killswitch.add_listener(broken)
    with pytest.raises(RuntimeError):
        killswitch.trip("bus_error_state")
    assert killswitch.tripped
    assert len(events(sink, "kill_switch")) == 1


def test_removing_a_listener():
    killswitch = KillSwitch()
    heard = []
    killswitch.add_listener(heard.append)
    killswitch.remove_listener(heard.append)
    killswitch.remove_listener(heard.append)  # already gone: harmless
    killswitch.trip("hotkey")
    assert heard == []


def test_three_consecutive_nrcs_on_one_request_trip_it():
    killswitch = KILL_SWITCH
    monitor = NrcMonitor()
    monitor.record_negative("a", 0x31, Purpose.LOGGING, 0.0)
    monitor.record_negative("a", 0x31, Purpose.LOGGING, 1.0)
    assert not killswitch.tripped
    monitor.record_negative("a", 0x31, Purpose.LOGGING, 2.0)
    assert killswitch.cause == "repeated_negative_responses"


def test_a_positive_answer_resets_the_consecutive_count():
    killswitch = KILL_SWITCH
    monitor = NrcMonitor()
    for _ in range(4):
        monitor.record_negative("a", 0x22, Purpose.SNAPSHOT, 0.0)
        monitor.record_negative("a", 0x22, Purpose.SNAPSHOT, 0.0)
        monitor.record_positive("a", "engine")
        # also trim the window so only the consecutive rule is in play
        monitor._window.clear()
    assert not killswitch.tripped


def test_ten_nrcs_within_thirty_seconds_trip_it():
    killswitch = KILL_SWITCH
    monitor = NrcMonitor()
    for i in range(9):
        monitor.record_negative(f"r{i}", 0x31, Purpose.LOGGING, float(i))
    assert not killswitch.tripped
    monitor.record_negative("r9", 0x31, Purpose.LOGGING, 9.0)
    assert killswitch.tripped


def test_old_nrcs_fall_out_of_the_window():
    killswitch = KILL_SWITCH
    monitor = NrcMonitor()
    for i in range(20):
        monitor.record_negative(f"r{i}", 0x31, Purpose.LOGGING, i * 4.0)
    assert not killswitch.tripped


def test_discovery_not_supported_answers_have_their_own_limit():
    killswitch = KILL_SWITCH
    monitor = NrcMonitor()
    for i in range(DISCOVERY_NRC_LIMIT - 1):
        monitor.record_negative("same", 0x31, Purpose.DISCOVERY, i * 0.1)
    assert not killswitch.tripped
    monitor.record_negative("same", 0x12, Purpose.DISCOVERY, 30.0)
    assert killswitch.tripped


def test_discovery_window_trims():
    killswitch = KILL_SWITCH
    monitor = NrcMonitor()
    for i in range(DISCOVERY_NRC_LIMIT * 2):
        monitor.record_negative("same", 0x11, Purpose.DISCOVERY, i * 1.0)
    assert not killswitch.tripped


def test_other_nrcs_in_discovery_count_normally():
    killswitch = KILL_SWITCH
    monitor = NrcMonitor()
    for _ in range(3):
        monitor.record_negative("x", 0x22, Purpose.DISCOVERY, 0.0)
    assert killswitch.tripped


def test_every_listener_runs_even_if_one_fails():
    killswitch = KillSwitch()
    heard = []

    def broken(cause):
        raise RuntimeError("listener bug")

    killswitch.add_listener(broken)
    killswitch.add_listener(heard.append)
    with pytest.raises(RuntimeError):
        killswitch.trip("hotkey")
    assert heard == ["hotkey"]  # one session's failing listener can't stop another's switch to listen-only


# ---- one kill switch for the whole process (finding C) ----


def _process_kill_switch():
    return importlib.import_module("lasto.safety.killswitch").KILL_SWITCH


def test_every_polled_session_shares_one_kill_switch(sim, auditor):
    first = open_polled(sim, auditor)
    first.close()
    second = open_polled(build_sim(), auditor)
    first.killswitch.trip("hotkey")  # through a closed session's handle, even
    assert second.killswitch.tripped and second.killswitch.cause == "hotkey"
    assert _process_kill_switch().cause == "hotkey"


def test_a_session_hands_out_only_trip_tripped_and_cause(sim, auditor):
    """Finding N7: listeners and the internal check stay inside the safety core."""
    handle = open_polled(sim, auditor).killswitch
    public = {name for name in dir(handle) if not name.startswith("_")}
    assert public - {"sealed_class"} == {"trip", "tripped", "cause"}  # sealed_class: the frozen mark, read-only
    assert handle.sealed_class is type(handle)
    assert (handle.tripped, handle.cause) == (False, None)
    assert handle.trip("hotkey") is True and handle.trip("error_frame") is False
    assert (handle.tripped, handle.cause) == (True, "hotkey")


def test_after_a_kill_no_polled_session_opens_until_the_process_restarts(sim, auditor, sink):
    session = open_polled(sim, auditor)
    session.killswitch.trip("hotkey")
    session.close()
    initializations = sim.dll.calls.count("CAN_Initialize")
    for target in (sim, build_sim()):  # the same adapter, or any other
        with pytest.raises(KillSwitchTripped) as refused:
            open_polled(target, auditor)
        assert refused.value.cause == "hotkey"
    assert sim.dll.calls.count("CAN_Initialize") == initializations  # refused before touching the adapter
    assert [r["reason"] for r in events(sink, "session_refused")] == ["kill_switch", "kill_switch"]
    assert [r["reason"] for r in events(sink, "rejected")] == ["kill_switch", "kill_switch"]


def test_passive_capture_still_opens_after_a_kill(sim, auditor):
    polled = open_polled(sim, auditor)
    polled.killswitch.trip("hotkey")
    polled.close()
    passive = open_passive_session(CHANNEL, auditor=auditor, clock=sim.clock, library=sim.dll)
    sim.clock.advance(0.2)
    assert passive.pump()
    passive.close()


def test_a_closed_session_no_longer_hears_kills(sim, auditor, sink):
    open_polled(sim, auditor).close()
    open_polled(build_sim(), auditor).killswitch.trip("hotkey")
    assert len(events(sink, "kill_listen_only")) == 1  # only the session still open switched to listen-only


def test_a_kill_with_no_session_open_reaches_the_next_audit_log(sim, auditor, sink):
    _process_kill_switch().trip("hotkey")  # nothing is attached yet, so the record is held
    with pytest.raises(KillSwitchTripped):
        open_polled(sim, auditor)
    [kill] = events(sink, "kill_switch")
    assert kill["cause"] == "hotkey" and "held_since" in kill


def test_a_reset_is_audited(auditor, sink):
    module = importlib.import_module("lasto.safety.killswitch")
    REFUSALS.attach(auditor)
    module.KILL_SWITCH.trip("hotkey")
    fresh_kill_switch()  # the plugin's reset: allowed, because this process has the test hardware firewall
    assert not module.KILL_SWITCH.tripped
    [reset] = events(sink, "kill_switch_reset")
    assert reset["previous_cause"] == "hotkey"


def test_the_reset_refuses_without_the_firewall(auditor, sink):
    module = importlib.import_module("lasto.safety.killswitch")
    REFUSALS.attach(auditor)
    module.KILL_SWITCH.trip("hotkey")
    with pytest.raises(SafetyViolation) as refused:
        module._check_reset_allowed(firewall_installed=False)
    assert refused.value.reason == "kill_switch_reset_refused"
    assert module.KILL_SWITCH.cause == "hotkey"
    assert [r["reason"] for r in events(sink, "rejected")] == ["kill_switch_reset_refused"]


RESET_WITHOUT_FIREWALL = """
import json
from lasto.safety.audit import REFUSALS, Auditor, MemoryAuditSink
from lasto.safety.clock import SystemClock
from lasto.safety.errors import SafetyViolation
from lasto.safety.killswitch import KILL_SWITCH, reset_for_tests

# The test hardware firewall isn't loaded here. Nothing below loads a DLL or opens a port.
sink = MemoryAuditSink()
REFUSALS.attach(Auditor(sink, SystemClock()))
KILL_SWITCH.trip("hotkey")
try:
    reset_for_tests()
    outcome = "reset"
except SafetyViolation as error:
    outcome = error.reason
print(json.dumps({"outcome": outcome, "cause": KILL_SWITCH.cause, "records": [[r["event"], r.get("reason")] for r in sink.records]}))
"""


def test_outside_a_test_run_the_reset_refuses_and_the_kill_stays_latched():
    result = subprocess.run([sys.executable, "-c", RESET_WITHOUT_FIREWALL], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {
        "outcome": "kill_switch_reset_refused",
        "cause": "hotkey",
        "records": [["kill_switch", None], ["rejected", "kill_switch_reset_refused"]],
    }


def test_only_the_pytest_plugin_resets_the_kill_switch():
    """In src/ only the plugin mentions the reset. In tests/, only this file, which proves its guards, and the
    public-API test, which declares the plugin's one allowance."""
    mentions = {
        path.relative_to(SRC.parent.parent).as_posix()
        for path in [*SRC.rglob("*.py"), *TESTS.rglob("*.py")]
        if "reset_for_tests" in path.read_text(encoding="utf-8")
    }
    assert mentions == {
        "src/lasto/safety/killswitch.py",
        "src/lasto/sim/pytest_plugin.py",
        "tests/safety/test_killswitch.py",
        "tests/safety/test_structure_reach.py",
    }


def test_nothing_in_src_loads_the_test_plugin():
    loaders = {name for name, tree in sources().items() if ("module", "lasto.sim.pytest_plugin") in uses(tree)}
    assert loaders <= {"lasto.sim.pytest_plugin"}


def test_repeated_timeouts_trip_it_and_answers_reset_them():
    killswitch = KILL_SWITCH
    monitor = NrcMonitor()
    for _ in range(CONSECUTIVE_TIMEOUT_LIMIT - 1):
        monitor.record_timeout("engine")
    monitor.record_positive("any", "engine")
    for _ in range(CONSECUTIVE_TIMEOUT_LIMIT - 1):
        monitor.record_timeout("engine")
    assert not killswitch.tripped
    monitor.record_timeout("engine")
    assert killswitch.cause == "repeated_timeouts"
