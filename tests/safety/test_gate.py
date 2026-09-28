"""The transmit gate: rules 2, 3, 5-11 end to end, plus response handling and flow control."""

import pytest
from helpers import LOGGING_RPM_SPEED, GateHarness, LooksLikeTheEngineId, events, frame, open_polled, park

from lasto.safety import ecus
from lasto.safety import requests as rq
from lasto.safety.ecus import ENGINE, Ecu, EcuKind
from lasto.safety.errors import InterfaceError, KillSwitchTripped, SafetyViolation
from lasto.safety.frames import CanFrame, ErrorFrame
from lasto.safety.exchange import ExchangeState
from lasto.safety.gate import (
    CONSECUTIVE_FRAME_TIMEOUT,
    LATE_RESPONSE_GRACE,
    MAX_RESPONSE_PENDING,
    P2_STAR_TIMEOUT,
    P2_TIMEOUT,
    Gate,
)
from lasto.safety.killswitch import CONSECUTIVE_TIMEOUT_LIMIT
from lasto.safety.requests import DtcKind, Purpose

FC = bytes.fromhex("3000000000000000")
TRANSMISSION = Ecu("transmission", EcuKind.TRANSMISSION, 0x7E1, 0x7E9, None, "not approved")


def refused(gate, request, reason):
    with pytest.raises(SafetyViolation) as caught:
        gate.submit(request)
    assert caught.value.reason == reason


# ---- end to end through the simulator ----


def test_logging_request_round_trip(sim, auditor, sink):
    session = open_polled(sim, auditor)
    sim.vehicle.state.rpm = 750
    exchange = session.request(LOGGING_RPM_SPEED)
    assert exchange.state is ExchangeState.DONE and exchange.done
    assert exchange.responses == ((0x7E8, bytes.fromhex("410C0BB80D00")),)
    assert sim.dll.writes == [(0x51, 0x7E0, bytes.fromhex("03010C0D00000000"))]
    [record] = events(sink, "transmit")
    assert (record["can_id"], record["kind"], record["purpose"]) == ("0x7E0", "request", "logging")


def test_functional_request_collects_every_answer(sim, auditor):
    session = open_polled(sim, auditor, profile=[rq.read_pid([0x0D], purpose=Purpose.LOGGING)])
    exchange = session.request(rq.read_pid([0x0D], purpose=Purpose.LOGGING))
    assert exchange.state is ExchangeState.DONE
    assert sorted(can_id for can_id, _ in exchange.responses) == [0x7E8, 0x7E9]
    assert sim.dll.writes[0][1] == 0x7DF


def test_parked_multi_frame_read_sends_flow_control_on_the_request_id(sim, auditor, sink):
    session = open_polled(sim, auditor)
    park(session)
    exchange = session.request(rq.read_vehicle_info(0x02, purpose=Purpose.IDENTIFY, ecu=ENGINE))
    assert exchange.state is ExchangeState.DONE
    assert exchange.responses == ((0x7E8, b"\x49\x02\x01" + b"JTJBT20X060000001"),)
    assert (0x51, 0x7E0, FC) in sim.dll.writes
    assert [r["kind"] for r in events(sink, "transmit")].count("flow_control") == 1


def test_functional_multi_frame_answers_only_the_approved_ecu(sim, auditor):
    session = open_polled(sim, auditor)
    park(session)
    exchange = session.request(rq.read_vehicle_info(0x02, purpose=Purpose.IDENTIFY))
    assert exchange.state is ExchangeState.DONE
    assert [can_id for can_id, _ in exchange.responses] == [0x7E8]
    flow_controls = [w for w in sim.dll.writes if w[2] == FC]
    assert flow_controls == [(0x51, 0x7E0, FC)]  # never to the transmission's 0x7E1


def test_multi_frame_from_an_unapproved_ecu_is_abandoned(sim, auditor, sink):
    sim.vehicle.transmission.response_delay = 0.001  # the transmission's first frame arrives first
    session = open_polled(sim, auditor)
    park(session)
    exchange = session.request(rq.read_vehicle_info(0x02, purpose=Purpose.IDENTIFY))
    assert [can_id for can_id, _ in exchange.responses] == [0x7E8]
    assert events(sink, "response_abandoned")[0]["can_id"] == "0x7E9"


def test_busy_answer_backs_off_the_next_request(sim, auditor):
    session = open_polled(sim, auditor)
    sim.vehicle.engine.script.append("busy")
    first = session.request(LOGGING_RPM_SPEED)
    assert (first.state, first.nrc) == (ExchangeState.NEGATIVE, 0x21)
    sent_after_busy = sim.clock.monotonic()
    second = session.request(LOGGING_RPM_SPEED)
    assert second.state is ExchangeState.DONE
    assert second.sent_at - sent_after_busy >= 0.19  # waited out the back-off


def test_response_pending_extends_the_wait(sim, auditor):
    session = open_polled(sim, auditor)
    sim.vehicle.engine.script.append("pending")
    exchange = session.request(LOGGING_RPM_SPEED)
    assert exchange.state is ExchangeState.DONE
    assert exchange.pending_extensions == 1


def test_silence_times_out_and_repeated_timeouts_trip_the_kill_switch(sim, auditor):
    session = open_polled(sim, auditor)
    sim.vehicle.engine.script.extend(["silent"] * CONSECUTIVE_TIMEOUT_LIMIT)
    for _ in range(CONSECUTIVE_TIMEOUT_LIMIT):
        assert session.request(LOGGING_RPM_SPEED).state is ExchangeState.TIMEOUT
    assert session.killswitch.cause == "repeated_timeouts"
    with pytest.raises(KillSwitchTripped):
        session.request(LOGGING_RPM_SPEED)


def test_repeated_negative_answers_trip_the_kill_switch(sim, auditor):
    session = open_polled(sim, auditor)
    sim.vehicle.engine.script.extend([0x22, 0x22, 0x22])
    for _ in range(3):
        session.request(LOGGING_RPM_SPEED)
    assert session.killswitch.cause == "repeated_negative_responses"
    assert sim.dll.channel(0x51).listen_only == 1  # the kill switched the channel to listen-only


def test_another_tester_trips_the_kill_switch(sim, auditor):
    from lasto.sim.tester import SimTester

    session = open_polled(sim, auditor)
    SimTester(sim.bus).request(0x7E0, b"\x21\x01")
    sim.clock.advance(0.01)
    session.pump()
    assert session.killswitch.cause == "foreign_tester"


# ---- direct gate checks ----


@pytest.fixture
def h(clock, auditor):
    return GateHarness(clock, auditor)


def test_profile_must_hold_logging_requests(clock, auditor):
    with pytest.raises(ValueError):
        GateHarness(clock, auditor, profile=[rq.read_pid([0x0C], purpose=Purpose.SNAPSHOT)])
    with pytest.raises(ValueError):
        GateHarness(clock, auditor, profile=["010C"])


def test_not_armed(clock, auditor, sink):
    h = GateHarness(clock, auditor, armed=False)
    refused(h.gate, LOGGING_RPM_SPEED, "gate_not_armed")
    assert events(sink, "rejected")[0]["reason"] == "gate_not_armed"
    assert h.writer.frames == []


def test_only_typed_requests(h):
    refused(h.gate, bytes.fromhex("02010C"), "not_a_typed_request")


def test_one_request_at_a_time(h):
    h.gate.submit(LOGGING_RPM_SPEED)
    assert h.gate.pending is not None
    refused(h.gate, LOGGING_RPM_SPEED, "request_outstanding")


def test_unapproved_ecu_is_refused(h):
    h.park()
    refused(h.gate, rq.read_pid([0x0C], purpose=Purpose.SNAPSHOT, ecu=TRANSMISSION), "ecu_not_approved")


@pytest.mark.parametrize("request_id", [0x7E0, "look-alike"])
def test_only_the_approved_entry_itself_reaches_the_bus(h, request_id):
    """Finding #4: a copy of the engine entry, or one whose ID only compares equal to 0x7E0, is refused."""
    rid = LooksLikeTheEngineId(0x7E1) if request_id == "look-alike" else request_id
    impostor = Ecu(ENGINE.name, ENGINE.kind, rid, ENGINE.response_id, ENGINE.ext_address, ENGINE.evidence)
    h.park()
    refused(h.gate, rq.read_local_id(impostor, 0x01, purpose=Purpose.SNAPSHOT), "ecu_not_approved")
    assert h.writer.frames == []


def test_interlocks_apply(h):
    refused(h.gate, rq.read_pid([0x05], purpose=Purpose.LOGGING, ecu=ENGINE), "not_in_logging_profile")
    refused(h.gate, rq.read_dtcs(DtcKind.STORED, purpose=Purpose.SNAPSHOT), "vehicle_not_confirmed_stationary")


def test_ids_carrying_broadcast_traffic_are_refused(clock, auditor):
    h = GateHarness(clock, auditor, broadcast_ids=[0x7E0])
    refused(h.gate, LOGGING_RPM_SPEED, "can_id_carries_broadcast")


def test_kill_switch_blocks_everything(h, sink):
    h.killswitch.trip("hotkey")
    with pytest.raises(KillSwitchTripped):
        h.gate.submit(LOGGING_RPM_SPEED)
    assert events(sink, "rejected")[0]["reason"] == "kill_switch"
    assert h.writer.frames == []


def test_kill_during_a_rate_limit_wait_stops_the_frame(clock, auditor):
    h = GateHarness(clock, auditor)
    h.gate.submit(LOGGING_RPM_SPEED)
    h.gate.abort()
    original_sleep = clock.sleep

    def sleep_then_kill(seconds):
        original_sleep(seconds)
        h.killswitch.trip("hotkey")

    clock.sleep = sleep_then_kill
    with pytest.raises(KillSwitchTripped):
        h.gate.submit(LOGGING_RPM_SPEED)
    assert len(h.writer.frames) == 1


def test_rate_limit_waits_briefly_and_refuses_long_waits(h, clock):
    h.gate.submit(LOGGING_RPM_SPEED)
    h.gate.abort()
    before = clock.monotonic()
    h.gate.submit(LOGGING_RPM_SPEED)  # waits out the 50 ms spacing (plus the pacing margin)
    assert clock.monotonic() - before == pytest.approx(0.05, abs=0.002)
    h.gate.abort()
    for _ in range(6):
        h.limiter.backoff(clock.monotonic())
    refused(h.gate, LOGGING_RPM_SPEED, "rate_limited")


def test_the_shared_spacing_counts_from_the_end_of_the_write(h, clock):
    h.writer.duration = 0.03  # a slow write
    h.gate.submit(LOGGING_RPM_SPEED)
    first_done = clock.monotonic()
    h.gate.abort()
    h.gate.submit(LOGGING_RPM_SPEED)
    assert h.writer.times[1] - first_done >= 1 / 20


def test_logging_flat_out_stays_under_the_write_functions_own_ceiling(sim, auditor, sink):
    session = open_polled(sim, auditor)
    for _ in range(70):
        assert session.request(LOGGING_RPM_SPEED).state is ExchangeState.DONE
    assert events(sink, "rejected") == []
    sent = [record["mono"] for record in events(sink, "transmit")]
    assert len(sent) == 70
    assert sent[-1] - sent[0] < 70 / 19  # flat out: close to the ceiling, not far below it
    for i, start in enumerate(sent):
        assert sum(1 for t in sent[i:] if t - start < 1.0) <= 20


def test_write_failure_trips_the_kill_switch(h):
    h.writer.fail = InterfaceError("adapter unplugged")
    with pytest.raises(InterfaceError):
        h.gate.submit(LOGGING_RPM_SPEED)
    assert h.killswitch.cause == "interface_write_failed"
    assert h.gate.pending is None


@pytest.mark.parametrize(
    ("can_id", "data", "reason"),
    [
        (0x7E0, bytes.fromhex("02010C00000000"), "frame_length"),
        (0x7E1, bytes.fromhex("02010C0000000000"), "can_id_not_allowlisted"),
        (0x025, bytes.fromhex("02010C0000000000"), "can_id_not_allowlisted"),
        (0x7E0, bytes.fromhex("0000000000000000"), "isotp_malformed"),
        (0x7E0, bytes.fromhex("1008010203040506"), "not_single_frame"),
        (0x7E0, bytes.fromhex("0210030000000000"), "service_never_allowed"),
        (0x7E0, bytes.fromhex("0105000000000000"), "service_not_allowlisted"),
        (0x7DF, bytes.fromhex("0221010000000000"), "manufacturer_service_on_functional_id"),
        ("7E0", bytes.fromhex("02010C0000000000"), "can_id_not_allowlisted"),
        (True, bytes.fromhex("02010C0000000000"), "can_id_not_allowlisted"),
        (0x7E0, bytearray.fromhex("02010C0000000000"), "frame_length"),
        (0x7E0, FC, "frame_kind_mismatch"),
    ],
)
def test_last_check_before_the_wire(h, can_id, data, reason):
    with pytest.raises(SafetyViolation) as caught:
        h.gate._transmit(can_id, data, purpose="test", kind="request")
    assert caught.value.reason == reason
    assert h.writer.frames == []


def test_unknown_frame_kinds_are_refused(h):
    with pytest.raises(SafetyViolation) as caught:
        h.gate._transmit(0x7E0, bytes.fromhex("02010C0000000000"), purpose="test", kind="first")
    assert caught.value.reason == "unknown_frame_kind"


def test_flow_control_only_answers_a_waiting_first_frame(h):
    def fc(can_id=0x7E0, data=FC):
        with pytest.raises(SafetyViolation) as caught:
            h.gate._transmit(can_id, data, purpose="test", kind="flow_control")
        return caught.value.reason

    assert fc() == "flow_control_unsolicited"  # nothing pending
    exchange = h.gate.submit(LOGGING_RPM_SPEED)
    assert fc() == "flow_control_unsolicited"  # pending, but no first frame
    exchange._rx_id = 0x7E9
    assert fc() == "flow_control_wrong_id"  # responder isn't approved
    exchange._rx_id = 0x7E8
    assert fc(can_id=0x7DF) == "flow_control_wrong_id"
    assert fc(data=bytes.fromhex("3001000000000000")) == "flow_control_malformed"
    exchange._flow_control_sent = True
    assert fc() == "flow_control_unsolicited"  # at most one per first frame
    assert h.writer.frames == [(0x7E0, bytes.fromhex("03010C0D00000000"))]


def test_flow_control_is_refused_on_a_broadcast_id(clock, auditor, sink):
    h = GateHarness(clock, auditor, profile=[rq.read_pid([0x0D], purpose=Purpose.LOGGING)], broadcast_ids=[0x7E0])
    h.gate.submit(rq.read_pid([0x0D], purpose=Purpose.LOGGING))
    h.gate.on_frame(frame(0x7E8, 0x10, 0x14, 0x49, 0x02, 0x01, 0x41, 0x42, 0x43))
    assert h.killswitch.cause == "flow_control_refused"
    [refusal] = events(sink, "rejected")
    assert (refusal["reason"], refusal["request"]) == ("can_id_carries_broadcast", "0x7E0 30 00 00 00 00 00 00 00")
    assert len(h.writer.frames) == 1  # only the request


def test_frames_that_are_not_diagnostic_are_ignored(h):
    h.gate.submit(LOGGING_RPM_SPEED)
    for item in (
        ErrorFrame(1, b"", 0),
        CanFrame(0x7E8, bytes(8), 0, extended=True),
        CanFrame(0x7E8, bytes(8), 0, rtr=True),
        frame(0x025, 0x07, 0xFF),
    ):
        h.gate.on_frame(item)
    assert not h.killswitch.tripped
    assert h.gate.pending is not None


def test_answers_nobody_asked_for_trip_the_kill_switch(h):
    h.gate.on_frame(frame(0x7E8, 0x03, 0x41, 0x0D, 0x00))
    assert h.killswitch.cause == "unexpected_response"


def test_a_late_answer_to_the_last_request_is_tolerated_and_logged(h, clock, sink):
    h.gate.submit(LOGGING_RPM_SPEED)
    clock.advance(P2_TIMEOUT)
    h.gate.poll()
    clock.advance(0.2)
    h.gate.on_frame(frame(0x7E8, 0x03, 0x41, 0x0D, 0x00))
    assert not h.killswitch.tripped
    [late] = events(sink, "late_response_ignored")
    assert (late["can_id"], late["data"]) == ("0x7E8", "03 41 0D 00 00 00 00 00")
    assert late["seconds_after_last_request_ended"] == pytest.approx(0.2)
    h.gate.on_frame(frame(0x7E9, 0x03, 0x41, 0x0D, 0x00))  # a different ECU is not "late"
    assert h.killswitch.cause == "unexpected_response"


def test_too_late_is_unexpected(h, clock):
    h.gate.submit(LOGGING_RPM_SPEED)
    clock.advance(P2_TIMEOUT)
    h.gate.poll()
    clock.advance(LATE_RESPONSE_GRACE + 0.01)
    h.gate.on_frame(frame(0x7E8, 0x03, 0x41, 0x0D, 0x00))
    assert h.killswitch.cause == "unexpected_response"


def test_a_physical_request_only_accepts_its_ecu(h):
    h.gate.submit(LOGGING_RPM_SPEED)
    h.gate.on_frame(frame(0x7E9, 0x03, 0x41, 0x0D, 0x00))
    assert h.killswitch.cause == "unexpected_response"


@pytest.mark.parametrize(
    ("data", "cause"),
    [
        ((0x00, 0x41), "malformed_response"),
        ((0x30, 0x00, 0x00), "unexpected_flow_control"),
        ((0x03, 0x7F, 0x22, 0x31), "unexpected_response"),  # NRC for another service
        ((0x02, 0x7F, 0x01), "unexpected_response"),  # truncated NRC
        ((0x03, 0x62, 0x0C, 0x00), "unexpected_response"),  # wrong positive service
    ],
)
def test_bad_responses_trip_the_kill_switch(h, data, cause):
    h.gate.submit(LOGGING_RPM_SPEED)
    h.gate.on_frame(frame(0x7E8, *data))
    assert h.killswitch.cause == cause
    assert h.gate.pending is None


def test_response_pending_limit(h):
    exchange = h.gate.submit(LOGGING_RPM_SPEED)
    for _ in range(MAX_RESPONSE_PENDING):
        h.gate.on_frame(frame(0x7E8, 0x03, 0x7F, 0x01, 0x78))
    assert exchange.state is ExchangeState.PENDING
    assert exchange.deadline == pytest.approx(h.clock.monotonic() + P2_STAR_TIMEOUT)
    h.gate.on_frame(frame(0x7E8, 0x03, 0x7F, 0x01, 0x78))
    assert exchange.state is ExchangeState.TIMEOUT


def test_multi_frame_reassembly_and_sequence_errors(h, sink):
    exchange = h.gate.submit(LOGGING_RPM_SPEED)
    h.gate.on_frame(frame(0x7E8, 0x10, 0x0E, 0x41, 0x0C, 0x0B, 0xB8, 0x0D, 0x00))
    assert h.writer.frames[-1] == (0x7E0, FC)
    assert exchange.deadline == pytest.approx(h.clock.monotonic() + CONSECUTIVE_FRAME_TIMEOUT)
    h.gate.on_frame(frame(0x7E8, 0x10, 0x0E, 1, 2, 3, 4, 5, 6))  # a second first frame is ignored
    assert [f for f in h.writer.frames if f[1] == FC] == [(0x7E0, FC)]
    h.gate.on_frame(frame(0x7E8, 0x21, 0x05, 0x50, 0x0F, 0x20, 0x11, 0x33, 0x42))
    assert exchange.state is ExchangeState.PENDING  # 13 of 14 bytes so far
    h.gate.on_frame(frame(0x7E8, 0x23, 0, 0, 0, 0, 0, 0, 0))  # sequence 3, expected 2
    assert exchange.state is ExchangeState.ABORTED
    assert events(sink, "isotp_sequence_error")
    assert not h.killswitch.tripped


def test_consecutive_frames_outside_the_accepted_transfer_are_ignored(clock, auditor):
    request = rq.read_pid([0x0D], purpose=Purpose.LOGGING)
    h = GateHarness(clock, auditor, profile=[request])
    exchange = h.gate.submit(request)
    h.gate.on_frame(frame(0x7E8, 0x21, 1, 2, 3, 4, 5, 6, 7))  # no first frame yet
    h.gate.on_frame(frame(0x7E8, 0x10, 0x09, 0x41, 0x0D, 0x00, 0x0C, 0x0B, 0xB8))
    h.gate.on_frame(frame(0x7E9, 0x21, 9, 9, 9, 9, 9, 9, 9))  # another ECU's frame
    assert exchange.state is ExchangeState.PENDING
    assert exchange._rx_data == bytearray(bytes.fromhex("410D000C0BB8"))
    assert not h.killswitch.tripped


def test_multi_frame_completes(h):
    exchange = h.gate.submit(LOGGING_RPM_SPEED)
    h.gate.on_frame(frame(0x7E8, 0x10, 0x09, 0x41, 0x0C, 0x0B, 0xB8, 0x0D, 0x00))
    h.gate.on_frame(frame(0x7E8, 0x21, 0x05, 0x50, 0xAA))
    assert exchange.state is ExchangeState.DONE
    assert exchange.responses == ((0x7E8, bytes.fromhex("410C0BB80D000550AA")),)


def test_functional_timeouts_are_not_counted(clock, auditor):
    request = rq.read_pid([0x0D], purpose=Purpose.LOGGING)
    h = GateHarness(clock, auditor, profile=[request])
    for _ in range(CONSECUTIVE_TIMEOUT_LIMIT + 1):
        exchange = h.gate.submit(request)
        clock.advance(P2_TIMEOUT)
        h.gate.poll()
        assert exchange.state is ExchangeState.TIMEOUT
        clock.advance(0.1)
    assert not h.killswitch.tripped


def test_functional_transfer_cut_off_at_the_deadline(clock, auditor):
    request = rq.read_pid([0x0D], purpose=Purpose.LOGGING)
    h = GateHarness(clock, auditor, profile=[request])
    exchange = h.gate.submit(request)
    h.gate.on_frame(frame(0x7E9, 0x03, 0x41, 0x0D, 0x00))
    h.gate.on_frame(frame(0x7E8, 0x10, 0x09, 0x41, 0x0D, 0x00, 0x0C, 0x0B, 0xB8))
    clock.advance(CONSECUTIVE_FRAME_TIMEOUT)
    h.gate.poll()
    assert exchange.state is ExchangeState.TIMEOUT


def test_poll_waits_for_the_deadline(h, clock):
    h.gate.poll()  # nothing pending
    exchange = h.gate.submit(LOGGING_RPM_SPEED)
    clock.advance(P2_TIMEOUT / 2)
    h.gate.poll()
    assert exchange.state is ExchangeState.PENDING and not exchange.done
    clock.advance(P2_TIMEOUT)
    h.gate.poll()
    assert exchange.state is ExchangeState.TIMEOUT


def test_abort(h):
    h.gate.abort()  # nothing pending
    exchange = h.gate.submit(LOGGING_RPM_SPEED)
    with h.gate.lock:
        h.gate.abort()
    assert exchange.state is ExchangeState.ABORTED


@pytest.mark.parametrize(
    ("payload", "attribute", "value"),
    [
        ((0x03, 0x41, 0x0D, 0x00), "_speed", 0.0),
        ((0x04, 0x41, 0x0C, 0x0B, 0xB8), "_rpm", 750.0),
        ((0x04, 0x41, 0x42, 0x31, 0x38), "_voltage", 12.6),
    ],
)
def test_probe_answers_feed_the_interlocks(clock, auditor, payload, attribute, value):
    h = GateHarness(clock, auditor)
    h.gate.submit(rq.interlock_probe(0x0D, ecu=ENGINE))
    h.gate.on_frame(frame(0x7E8, *payload))
    assert getattr(h.interlocks, attribute).value == pytest.approx(value)


@pytest.mark.parametrize(
    "payload",
    [
        (0x02, 0x41, 0x0D),  # no data
        (0x04, 0x41, 0x0D, 0x00, 0x00),  # wrong length for speed
        (0x03, 0x41, 0x0C, 0x0B),  # wrong length for RPM
        (0x03, 0x41, 0x42, 0x31),  # wrong length for voltage
        (0x03, 0x41, 0x05, 0x7B),  # coolant: not an interlock input
    ],
)
def test_other_answers_do_not_feed_the_interlocks(clock, auditor, payload):
    h = GateHarness(clock, auditor)
    exchange = h.gate.submit(rq.interlock_probe(0x0D, ecu=ENGINE))
    h.gate.on_frame(frame(0x7E8, *payload))
    assert exchange.state is ExchangeState.DONE
    assert h.interlocks._speed is h.interlocks._rpm is h.interlocks._voltage is None


def test_local_id_answers_do_not_feed_the_interlocks(clock, auditor):
    h = GateHarness(clock, auditor)
    h.park()
    exchange = h.gate.submit(rq.read_local_id(ENGINE, 0x0D, purpose=Purpose.SNAPSHOT))
    speed_before = h.interlocks._speed
    h.gate.on_frame(frame(0x7E8, 0x03, 0x61, 0x0D, 0x09))
    assert exchange.state is ExchangeState.DONE
    assert h.interlocks._speed is speed_before


def test_gate_only_knows_the_approved_request_ids(h):
    assert h.gate._request_ids == {0x7DF} | {ecu.request_id for ecu in ecus.APPROVED_ECUS}
    assert isinstance(h.gate, Gate)
