"""Finding #1: the gate is the only reachable way to transmit.

These tests look at a live polled session the way a caller could: every
attribute path from the session object, public or private (but never
through closures, frames, or modules). The raw CAN_Write may only be
reachable inside the armored write function the gate holds, and that
function must check everything on its own.
"""

import contextlib
import importlib
import threading
import types

import pytest
from helpers import LOGGING_RPM_SPEED, events, open_polled, paths_to, the_writer, watching_the_write_function

from lasto.safety.clock import SystemClock
from lasto.safety.errors import KillSwitchTripped, SafetyViolation
from lasto.safety.pcan_active import RequestCeiling
from lasto.safety.session import open_polled_session
from lasto.sim.clock import FakeClock
from lasto.sim.violations import VIOLATIONS

PUBLIC_MODULES = [
    "lasto.safety.session",
    "lasto.safety.requests",
    "lasto.safety.ecus",
    "lasto.safety.errors",
    "lasto.safety.frames",
    "lasto.safety.audit",
    "lasto.safety.clock",
    "lasto.safety.exchange",
    "lasto.safety.stn_port",
]
TRANSMIT_CAPABLE_MODULES = {"lasto.safety.pcan_active", "lasto.safety.gate"}
REQUEST = bytes.fromhex("02010C0000000000")


def raw_can_write(dll):
    def match(obj):
        if obj is dll:
            return True
        return isinstance(obj, types.MethodType) and obj.__self__ is dll and obj.__func__.__name__ in {"CAN_Write", "_can_write"}

    return match


def test_public_modules_expose_nothing_that_can_transmit():
    exposed = []
    for name in PUBLIC_MODULES:
        module = importlib.import_module(name)
        for attr, value in vars(module).items():
            origin = getattr(value, "__module__", None)
            if origin in TRANSMIT_CAPABLE_MODULES and origin != name:
                exposed.append(f"{name}.{attr} (from {origin})")
    assert exposed == []


def test_the_raw_can_write_is_reachable_only_inside_the_armored_writer(sim, auditor):
    session = open_polled(sim, auditor)
    hits = paths_to(session, raw_can_write(sim.dll))
    assert hits, "the gate should hold the one write function"
    outside = [path for path, via, _ in hits if not any(type(obj).__name__ == "Writer" for obj in via)]
    assert outside == []


def test_the_polled_readers_channel_cannot_write(sim, auditor):
    session = open_polled(sim, auditor)
    channel = session.reader._channel
    assert not any("write" in name.lower() for name in dir(channel))
    assert paths_to(channel, raw_can_write(sim.dll)) == []


@pytest.mark.parametrize(
    ("can_id", "data", "kind", "reason"),
    [
        (0x025, REQUEST, "request", "can_id_not_allowlisted"),
        (0x7E1, REQUEST, "request", "can_id_not_allowlisted"),
        (0x7E0, bytes.fromhex("0210030000000000"), "request", "service_never_allowed"),
        (0x7DF, bytes.fromhex("0221010000000000"), "request", "manufacturer_service_on_functional_id"),
        (0x7E0, bytes.fromhex("1008010203040506"), "request", "not_single_frame"),
        (0x7E0, REQUEST[:7], "request", "frame_length"),
        (0x7E0, bytearray(REQUEST), "request", "frame_length"),
        (0x7DF, bytes.fromhex("3000000000000000"), "flow_control", "flow_control_wrong_id"),
        (0x7E0, bytes.fromhex("3001000000000000"), "flow_control", "flow_control_malformed"),
        (0x7E0, REQUEST, "flow_control", "frame_kind_mismatch"),
        (0x7E0, bytes.fromhex("3000000000000000"), "request", "frame_kind_mismatch"),
    ],
)
def test_the_write_function_checks_everything_itself(sim, auditor, sink, can_id, data, kind, reason):
    session = open_polled(sim, auditor)
    writer = the_writer(session)
    before = list(sim.dll.writes)
    with pytest.raises(SafetyViolation):
        writer(can_id, data, purpose="test", kind=kind)
    assert sim.dll.writes == before
    assert [r["reason"] for r in events(sink, "rejected")] == [reason]


def test_the_runtime_watch_sees_who_calls_the_write_function(sim, auditor):
    """The watch the session fuzzing relies on: a request through the gate passes, a direct call is named."""
    session = open_polled(sim, auditor)
    writer = the_writer(session)
    with watching_the_write_function() as watch:
        session.request(LOGGING_RPM_SPEED)
        writer(0x7E0, REQUEST, purpose="test", kind="request")
    assert watch.calls == 2
    assert watch.strangers == ["test_the_runtime_watch_sees_who_calls_the_write_function"]


def test_the_write_function_checks_the_kill_switch_and_audits_first(sim, auditor, sink):
    session = open_polled(sim, auditor)
    writer = the_writer(session)
    writer(0x7E0, REQUEST, purpose="test", kind="request")
    assert sim.dll.writes[-1] == (0x51, 0x7E0, REQUEST)
    assert events(sink, "transmit")[-1]["data"] == "02 01 0C 00 00 00 00 00"
    session.killswitch.trip("hotkey")
    with pytest.raises(KillSwitchTripped):
        writer(0x7E0, REQUEST, purpose="test", kind="request")
    assert len(sim.dll.writes) == 1


# Rule 6, from the spec: a hard ceiling of 20 requests per second.
CEILING = 20
FC = bytes.fromhex("3000000000000000")


def test_even_a_leaked_writer_holds_requests_to_the_hard_ceiling(sim, auditor, sink):
    session = open_polled(sim, auditor)
    writer = the_writer(session)
    for _ in range(CEILING):
        writer(0x7E0, REQUEST, purpose="test", kind="request")
    with pytest.raises(SafetyViolation):
        writer(0x7E0, REQUEST, purpose="test", kind="request")
    assert len(sim.dll.writes) == CEILING
    assert [r["reason"] for r in events(sink, "rejected")] == ["request_ceiling"]
    assert len(events(sink, "transmit")) == CEILING  # the refused frame was never recorded as sent
    sim.clock.advance(0.999)
    with pytest.raises(SafetyViolation):
        writer(0x7E0, REQUEST, purpose="test", kind="request")
    sim.clock.advance(0.001)  # the first request is now a full second old
    writer(0x7E0, REQUEST, purpose="test", kind="request")
    assert len(sim.dll.writes) == CEILING + 1


def test_flow_control_is_exempt_from_the_ceiling_and_uses_none_of_it(sim, auditor):
    session = open_polled(sim, auditor)
    writer = the_writer(session)
    with VIOLATIONS.expect() as oracle:  # unsolicited on purpose: whether a first frame waits is the gate's check
        for _ in range(5):
            writer(0x7E0, FC, purpose="test", kind="flow_control")
        for _ in range(CEILING):
            writer(0x7E0, REQUEST, purpose="test", kind="request")
        writer(0x7E0, FC, purpose="test", kind="flow_control")
    assert [data for _, _, data in sim.dll.writes].count(REQUEST) == CEILING
    assert [data for _, _, data in sim.dll.writes].count(FC) == 6
    assert len(oracle) == 6 and all(item.startswith("flow control no ECU asked for") for item in oracle)


def test_the_ceiling_counts_every_write_function_in_the_process(sim, auditor, sink):
    """Finding N3: two polled sessions (two channels onto the same bus) share the 20 per second."""
    first = open_polled(sim, auditor)
    second = open_polled_session(
        "PCAN_USBBUS2", profile=[LOGGING_RPM_SPEED], auditor=auditor, clock=sim.clock, library=sim.dll, listen_seconds=0.05
    )
    writers = [the_writer(first), the_writer(second)]
    for index in range(CEILING):
        writers[index % 2](0x7E0, REQUEST, purpose="test", kind="request")
    for writer in writers:
        with pytest.raises(SafetyViolation) as refused:
            writer(0x7E0, REQUEST, purpose="test", kind="request")
        assert refused.value.reason == "request_ceiling"
    assert len(sim.dll.writes) == CEILING


def test_on_real_hardware_the_process_shares_one_window(auditor):
    """Every SystemClock instance counts against the same window; a simulated clock is a separate world."""
    ceiling = RequestCeiling()
    first, second = SystemClock(), SystemClock()
    now = first.monotonic()
    for index in range(CEILING):
        ceiling.record(first if index % 2 else second, now)
    with pytest.raises(SafetyViolation):
        ceiling.admit(SystemClock(), now, "a third session's request")
    ceiling.admit(FakeClock(), now, "a simulated session's request")  # not refused


def test_concurrent_callers_cannot_slip_past_the_ceiling(sim, auditor):
    session = open_polled(sim, auditor)
    writer = the_writer(session)
    start = threading.Barrier(40)

    def call():
        start.wait()
        with contextlib.suppress(SafetyViolation):
            writer(0x7E0, REQUEST, purpose="test", kind="request")

    threads = [threading.Thread(target=call) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(sim.dll.writes) == CEILING
