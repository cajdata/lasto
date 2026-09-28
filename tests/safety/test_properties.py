"""Property-based tests: whatever goes in, nothing outside the allowlists reaches the interface.

The expectations here are written from the project spec, not imported from the
safety core, so a mistake in the core's allowlists can't also hide in the test.
"""

import string

from helpers import CHANNEL, LOGGING_RPM_SPEED, GateHarness, frame, open_polled, watching_the_write_function
from hypothesis import given, settings
from hypothesis import strategies as st

from lasto.safety import requests as rq
from lasto.safety.audit import REFUSALS, Auditor, MemoryAuditSink
from lasto.safety.ecus import ENGINE, Ecu, EcuKind
from lasto.safety.errors import AdapterError, KillSwitchTripped, SafetyViolation
from lasto.safety.pcan_active import open_active
from lasto.safety.requests import DtcKind, Purpose
from lasto.safety.session import open_passive_session
from lasto.safety.stn_port import StnAdapter
from lasto.sim.clock import FakeClock
from lasto.sim.fake_pcan import FakePcanDll
from lasto.sim.fake_stn import FakeStnPort
from lasto.sim.pytest_plugin import fresh_kill_switch
from lasto.sim.tester import SimTester
from lasto.sim.vehicle import build_sim
from lasto.sim.violations import VIOLATIONS

SPEC_OBD = {0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A}
SPEC_MANUFACTURER = {0x21, 0x22, 0x1A, 0x13, 0x17, 0x18, 0x19}
ALLOWED_IDS = {0x7DF, 0x7E0}  # functional, plus the one approved physical ID
FC = bytes.fromhex("3000000000000000")
FORGED = [
    Ecu("transmission", EcuKind.TRANSMISSION, 0x7E1, 0x7E9, None, "forged"),
    Ecu("airbag", EcuKind.SRS, 0x780, 0x788, None, "forged"),
    Ecu("engine", EcuKind.ENGINE, 0x7E0, 0x7E8, 0x40, "forged: extended addressing"),
]
PARKED = {Purpose.SNAPSHOT, Purpose.IDENTIFY, Purpose.DISCOVERY}


def scaled(examples):
    """Fewer examples for the slow whole-session tests, scaled up under the thorough profile."""
    return max(examples, settings().max_examples // 25)


def assert_allowed(can_id, data):
    assert len(data) == 8
    assert can_id in ALLOWED_IDS
    if data[0] >> 4 == 0:
        assert 1 <= data[0] <= 7
        service = data[1]
        assert service in SPEC_OBD | SPEC_MANUFACTURER
        if can_id == 0x7DF:
            assert service in SPEC_OBD
    else:
        assert (can_id, data) == (0x7E0, FC)


def fresh_harness(**options):
    """A gate with its own audit sink attached to the refusal log (and nothing else attached)."""
    fresh_kill_switch()  # many examples run in one test
    REFUSALS.reset()
    clock = FakeClock()
    sink = MemoryAuditSink()
    harness = GateHarness(clock, Auditor(sink, clock), **options)
    harness.sink = sink
    return harness


def refusals(sink):
    return [record for record in sink.records if record["event"] == "rejected"]


can_ids = st.one_of(st.integers(0, 0x1FFFFFFF), st.sampled_from([0x7DF, 0x7E0, 0x7E1, 0x7E8, 0x025, 0x700]))
payloads = st.one_of(
    st.binary(max_size=12),
    st.builds(lambda pci, service, rest: bytes([pci, service]) + rest, st.integers(0, 255), st.integers(0, 255), st.binary(max_size=6)),
    st.builds(lambda service, rest: (bytes([len(rest) + 1, service]) + rest).ljust(8, b"\0"), st.integers(0, 255), st.binary(max_size=6)),
)


@given(can_id=can_ids, data=payloads, kind=st.sampled_from(["request", "flow_control", "first", ""]), waiting=st.booleans())
def test_fuzzed_frames_at_the_last_check_before_the_wire(can_id, data, kind, waiting):
    h = fresh_harness()
    if waiting:
        h.gate.submit(LOGGING_RPM_SPEED)
        h.gate.on_frame(frame(0x7E8, 0x10, 0x0E, 0x41, 0x0C, 0x0B, 0xB8, 0x0D, 0x00))
    before = list(h.writer.frames)
    audited_before = len(refusals(h.sink))
    refused = False
    try:
        h.gate._transmit(can_id, data, purpose="fuzz", kind=kind)
    except SafetyViolation:
        refused = True
        assert h.writer.frames == before
    for sent in h.writer.frames:
        assert_allowed(*sent)
    assert len(refusals(h.sink)) - audited_before == (1 if refused else 0)  # every refusal audited, once


@given(can_id=can_ids, data=payloads, kind=st.sampled_from(["request", "flow_control", "first", ""]))
def test_fuzzed_frames_straight_into_the_write_function(can_id, data, kind):
    """Called directly, around the gate, the one holder of CAN_Write still sends nothing the spec forbids."""
    fresh_kill_switch()  # many examples run in one test
    REFUSALS.reset()
    clock = FakeClock()
    sink = MemoryAuditSink()
    auditor = Auditor(sink, clock)
    REFUSALS.attach(auditor)
    dll = FakePcanDll()
    _channel, writer = open_active(CHANNEL, library=dll, auditor=auditor, clock=clock)
    refused = False
    with VIOLATIONS.expect() as oracle:
        try:
            writer(can_id, data, purpose="fuzz", kind=kind)
        except SafetyViolation:
            refused = True
    for _handle, sent_id, sent in dll.writes:
        assert_allowed(sent_id, sent)
    assert refused == (dll.writes == [])
    assert len(refusals(sink)) == (1 if refused else 0)
    assert len([r for r in sink.records if r["event"] == "transmit"]) == len(dll.writes)  # audited before writing
    # The write function doesn't track first frames, so whether one is waiting for flow control is the gate's check.
    assert all(item.startswith("flow control no ECU asked for") for item in oracle)


@given(steps=st.lists(st.tuples(st.floats(0, 0.15), st.sampled_from(["request", "flow_control"])), max_size=90))
def test_no_second_ever_holds_more_than_20_requests_from_the_write_function(steps):
    """The write function's own ceiling, from the spec: 20 requests per second. It refuses only when full."""
    fresh_kill_switch()  # many examples run in one test
    REFUSALS.reset()
    clock = FakeClock()
    sink = MemoryAuditSink()
    auditor = Auditor(sink, clock)
    REFUSALS.attach(auditor)
    dll = FakePcanDll()
    _channel, writer = open_active(CHANNEL, library=dll, auditor=auditor, clock=clock)
    sent: list[float] = []
    flow_controls = 0
    with VIOLATIONS.expect() as oracle:  # unsolicited flow control on purpose (see above)
        for gap, kind in steps:
            clock.advance(gap)
            now = clock.monotonic()
            in_last_second = sum(1 for t in sent if now - t < 1.0)
            try:
                writer(0x7E0, bytes.fromhex("02010C0000000000") if kind == "request" else FC, purpose="fuzz", kind=kind)
            except SafetyViolation:
                assert (kind, in_last_second) == ("request", 20)  # refused only at the ceiling
                continue
            if kind == "request":
                assert in_last_second < 20
                sent.append(now)
            else:
                flow_controls += 1
    assert len(dll.writes) == len(sent) + flow_controls
    for i, start in enumerate(sent):
        assert sum(1 for t in sent[i:] if t - start < 1.0) <= 20
    assert all(item.startswith("flow control no ECU asked for") for item in oracle)


BUILDERS = [
    lambda a, p, e: rq.read_pid(a[: 1 + a[6] % 7], purpose=p, ecu=e),
    lambda a, p, e: rq.read_freeze_frame(a[0], frame=a[1], purpose=p, ecu=e),
    lambda a, p, e: rq.read_dtcs(list(DtcKind)[a[0] % 3], purpose=p, ecu=e),
    lambda a, p, e: rq.read_mode06(a[0], purpose=p, ecu=e),
    lambda a, p, e: rq.read_vehicle_info(a[0], purpose=p, ecu=e),
    lambda a, p, e: rq.read_local_id(e, a[0], purpose=p),
    lambda a, p, e: rq.read_did(e, a[0] * 300 + a[1], purpose=p),
    lambda a, p, e: rq.read_ecu_id(e, a[0], purpose=p),
    lambda a, p, e: rq.read_dtcs_kwp(e, group=a[0] * 256 + a[1], purpose=p),
    lambda a, p, e: rq.read_dtc_status(e, a[0] * 256 + a[1], purpose=p),
    lambda a, p, e: rq.read_dtcs_by_status(e, status=a[0], group=a[1] * 256, purpose=p),
    lambda a, p, e: rq.read_dtc_information(e, a[0], *a[1 : 1 + a[6] % 7], purpose=p),
    lambda a, p, e: rq.interlock_probe(a[0], ecu=e),
]


@given(
    index=st.integers(0, len(BUILDERS) - 1),
    args=st.lists(st.one_of(st.integers(-3, 300), st.sampled_from([0x0C, 0x0D, 0x42, 0x02, 0xD9])), min_size=7, max_size=7),
    purpose=st.sampled_from(Purpose),
    ecu=st.sampled_from([None, ENGINE, *FORGED]),
    moving=st.booleans(),
    volts=st.sampled_from([11.5, 12.6]),
)
def test_fuzzed_typed_requests(index, args, purpose, ecu, moving, volts):
    h = fresh_harness(profile=[LOGGING_RPM_SPEED, rq.read_pid([0x0D], purpose=Purpose.LOGGING)])
    h.park(volts=volts)
    if moving:
        h.interlocks.update_speed(60, h.clock.monotonic(), source="poll")
    try:
        request = BUILDERS[index](args, purpose, ecu)
    except (ValueError, TypeError, SafetyViolation):
        assert len(refusals(h.sink)) == 1  # the builder's refusal, audited once
        return
    refused = False
    try:
        h.gate.submit(request)
    except SafetyViolation:
        refused = True
        assert h.writer.frames == []
    assert len(refusals(h.sink)) == (1 if refused else 0)
    for sent in h.writer.frames:
        assert_allowed(*sent)
    if h.writer.frames:
        assert ecu is None or ecu == ENGINE
        assert not (request.purpose in PARKED and (moving or volts < 12.0))


incoming = st.lists(
    st.tuples(
        st.sampled_from([0x7E8, 0x7E9, 0x7EF, 0x025, 0x7DF, 0x7E0, 0x7E1]),
        st.one_of(
            st.binary(min_size=1, max_size=8),
            st.builds(lambda n: bytes([0x10, n, 0x41, 0x0D, 0, 0, 0, 0]), st.integers(0, 40)),
            st.builds(lambda s, b: bytes([0x20 | s]) + b, st.integers(0, 15), st.binary(max_size=7)),
        ),
        st.floats(0, 0.3),
    ),
    max_size=25,
)


@given(frames=incoming, functional=st.booleans())
def test_fuzzed_responses_only_ever_draw_valid_flow_control(frames, functional):
    request = rq.read_pid([0x0D], purpose=Purpose.LOGGING) if functional else LOGGING_RPM_SPEED
    h = fresh_harness(profile=[request])
    sent_at_kill = []
    h.killswitch.add_listener(lambda cause: sent_at_kill.append(len(h.writer.frames)))
    h.gate.submit(request)
    first_frames = 0
    for can_id, data, gap in frames:
        received = frame(can_id, *data)  # padded to 8 bytes, as on the bus
        if can_id == 0x7E8 and received.data[0] >> 4 == 1:
            first_frames += 1
        h.gate.on_frame(received)
        h.clock.advance(gap)
        h.gate.poll()
    request_frame, *flow_controls = h.writer.frames
    assert_allowed(*request_frame)
    assert all(sent == (0x7E0, FC) for sent in flow_controls)
    assert len(flow_controls) <= first_frames
    if sent_at_kill:
        assert len(h.writer.frames) == sent_at_kill[0]  # nothing after a kill


ADAPTER_WORDS = ["STCMM 1", "ATSP0", "STPX", "ATPP 21 SV 00", "STI", "STVR", "ATZ", "STMA", "0100", "ATSH7E0", "STP22", "ATSW92"]


@given(
    command=st.one_of(
        st.text(alphabet=st.characters(codec="ascii"), max_size=24),
        st.text(alphabet=string.ascii_letters + string.digits + " ,", max_size=16),
        st.builds(lambda w, spaces: " ".join(w) if spaces else w.lower(), st.sampled_from(ADAPTER_WORDS), st.booleans()),
    )
)
def test_fuzzed_adapter_commands_never_reach_the_adapter_unless_allowed(command):
    REFUSALS.reset()
    port = FakeStnPort()
    sink = MemoryAuditSink()
    stn = StnAdapter(port, Auditor(sink, FakeClock()))
    stn.reset()
    before = len(port.commands)
    refused = False
    try:
        stn._command(command)
    except SafetyViolation:
        refused = True
    except AdapterError:
        pass
    for sent in port.commands[before:]:
        assert sent.startswith(("AT", "ST"))
        assert sent not in {"ATZ", "ATWS", "STMA", "STM"}
    assert len(refusals(sink)) == (1 if refused else 0)
    # The autouse fixture fails the test if the simulated adapter saw anything dangerous.


@settings(max_examples=scaled(60))
@given(steps=st.lists(st.sampled_from(["reset", "identify", "voltage", "pps", "can", "can33", "kline", "read", "stop"]), max_size=12))
def test_any_sequence_of_adapter_operations_stays_silent(steps):
    REFUSALS.reset()
    port = FakeStnPort(monitor_lines=["7E8 03 41 0D 00"] * 3, monitor_ends=True)
    stn = StnAdapter(port, Auditor(MemoryAuditSink(), FakeClock()))
    operations = {
        "reset": stn.reset,
        "identify": stn.identify,
        "voltage": stn.read_voltage,
        "pps": stn.programmable_parameters,
        "can": stn.start_can_monitor,
        "can33": lambda: stn.start_can_monitor("33"),
        "kline": stn.start_kline_monitor,
        "read": stn.read_monitor_line,
        "stop": stn.stop_monitor,
    }
    for step in steps:
        try:
            operations[step]()
        except (SafetyViolation, AdapterError):
            pass


@settings(max_examples=scaled(40))
@given(
    injections=st.lists(
        st.one_of(
            st.tuples(st.just("frame"), st.integers(0, 0x7FF), st.binary(max_size=8)),
            st.tuples(st.just("error"), st.integers(0, 8), st.binary(max_size=4)),
            st.tuples(st.just("status"), st.sampled_from([0x4, 0x8, 0x10, 0x40000, 0x1400]), st.just(b"")),
            st.tuples(st.just("read_error"), st.sampled_from([0x2, 0x40, 0x1400]), st.just(b"")),
        ),
        max_size=20,
    ),
    run_for=st.floats(0, 0.3),
)
def test_passive_sessions_never_transmit(injections, run_for):
    REFUSALS.reset()
    sim = build_sim()
    session = open_passive_session(CHANNEL, auditor=Auditor(MemoryAuditSink(), sim.clock), clock=sim.clock, library=sim.dll)
    for kind, value, data in injections:
        if kind == "frame":
            sim.dll.inject_frame(0x51, value, data)
        elif kind == "error":
            sim.dll.inject_error_frame(0x51, value, data)
        elif kind == "status":
            sim.dll.inject_status(0x51, value)
        else:
            sim.dll.inject_read_error(0x51, value)
    sim.clock.advance(run_for)
    for _ in range(5):
        session.pump()
    assert sim.dll.writes == []
    assert "CAN_Write" not in sim.dll.looked_up


@settings(max_examples=scaled(30))
@given(
    plan=st.lists(
        st.tuples(
            st.sampled_from(["rpm", "functional", "vin", "local", "probe", "dtcs"]),
            st.sampled_from([None, "busy", "pending", "silent", 0x31, 0x22, b"\x7f\x01\x78", b"\x62\x00"]),
            st.booleans(),
        ),
        max_size=8,
    )
)
def test_polled_sessions_only_send_allowed_frames(plan):
    fresh_kill_switch()  # many examples run in one test
    REFUSALS.reset()
    sim = build_sim()
    functional = rq.read_pid([0x0D], purpose=Purpose.LOGGING)
    session = open_polled(sim, Auditor(MemoryAuditSink(), sim.clock), profile=[LOGGING_RPM_SPEED, functional])
    requests = {
        "rpm": LOGGING_RPM_SPEED,
        "functional": functional,
        "vin": rq.read_vehicle_info(0x02, purpose=Purpose.IDENTIFY, ecu=ENGINE),
        "local": rq.read_local_id(ENGINE, 0xD9, purpose=Purpose.SNAPSHOT),
        "probe": rq.interlock_probe(0x0D),
        "dtcs": rq.read_dtcs(DtcKind.STORED, purpose=Purpose.SNAPSHOT),
    }
    with watching_the_write_function() as watch:
        for name, behavior, intruder in plan:
            if behavior is not None:
                sim.vehicle.engine.script.append(behavior)
            if intruder:
                SimTester(sim.bus).request(0x7E0, b"\x21\x01", delay=0.001)
            try:
                session.request(requests[name])
            except (SafetyViolation, KillSwitchTripped):
                pass
    for handle, can_id, data in sim.dll.writes:
        assert handle == 0x51
        assert_allowed(can_id, data)
    # Every frame reached the write function from the gate, requests and flow control alike.
    assert watch.strangers == [] and watch.calls >= len(sim.dll.writes)
    # The simulator's own oracle judged every write too (autouse fixture).
