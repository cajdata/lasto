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
from collections import deque

import pytest
from helpers import events, open_polled

from lasto.safety.errors import KillSwitchTripped, SafetyViolation
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
LEAVES = (
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    types.ModuleType,
    type,
    str,
    bytes,
    int,
    float,
    bool,
    type(None),
)
REQUEST = bytes.fromhex("02010C0000000000")


def attributes(obj):
    if isinstance(obj, dict):
        return [(f"[{key!r}]", value) for key, value in list(obj.items())]
    if isinstance(obj, list | tuple | set | frozenset | deque):
        return [(f"[{index}]", value) for index, value in enumerate(list(obj))]
    found = [(f".{key}", value) for key, value in vars(obj).items()] if hasattr(obj, "__dict__") else []
    for cls in type(obj).__mro__:
        for slot in getattr(cls, "__slots__", ()):
            if isinstance(slot, str) and hasattr(obj, slot):
                found.append((f".{slot}", getattr(obj, slot)))
    return found


def paths_to(root, match):
    """Every attribute path from root to an object match() accepts, with the objects passed on the way."""
    hits, seen, queue = [], {id(root)}, deque([(root, "session", ())])
    while queue:
        obj, path, via = queue.popleft()
        for step, child in attributes(obj):
            if match(child):
                hits.append((path + step, via + (obj,), child))
                continue
            if isinstance(child, LEAVES) or id(child) in seen:
                continue
            seen.add(id(child))
            queue.append((child, path + step, via + (obj,)))
    return hits


def raw_can_write(dll):
    def match(obj):
        if obj is dll:
            return True
        return isinstance(obj, types.MethodType) and obj.__self__ is dll and obj.__func__.__name__ in {"CAN_Write", "_can_write"}

    return match


def the_writer(session):
    hits = paths_to(session, lambda obj: type(obj).__name__ == "Writer")
    if not hits:
        pytest.fail("the polled session holds no armored write function")
    return hits[0][2]


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
