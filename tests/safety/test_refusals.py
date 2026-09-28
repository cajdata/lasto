"""Rule 11: every refusal is audited where it is raised, exactly once."""

import dataclasses

import pytest
from helpers import CHANNEL, HANDLE, GateHarness, events, open_polled

from lasto.safety import isotp
from lasto.safety import pcan_constants as pc
from lasto.safety import requests as rq
from lasto.safety import stn_policy
from lasto.safety.audit import REFUSALS, Auditor, MemoryAuditSink, RefusalLog, refuse
from lasto.safety.ecus import ENGINE
from lasto.safety.errors import KillSwitchTripped, PassiveModeUnconfirmed, SafetyViolation
from lasto.safety.interlocks import Interlocks
from lasto.safety.killswitch import KillSwitch
from lasto.safety.pcan_active import open_active
from lasto.safety.pcan_dll import load_readonly
from lasto.safety.pcan_passive import open_passive
from lasto.safety.ratelimit import PURPOSE_RATES, RateLimiter
from lasto.safety.requests import DtcKind, Purpose
from lasto.safety.session import open_passive_session
from lasto.safety.stn_port import StnAdapter, open_serial
from lasto.sim.clock import FakeClock
from lasto.sim.fake_pcan import FakePcanDll
from lasto.sim.fake_stn import FakeStnPort

P = Purpose.SNAPSHOT
LOCAL = rq.read_local_id(ENGINE, 0x01, purpose=P)


def attached(clock):
    sink = MemoryAuditSink()
    REFUSALS.attach(Auditor(sink, clock))
    return sink


# (what is refused, how, exception type, audited reason)
SITES = [
    ("Request made directly", lambda: rq.Request(ENGINE, b"\x21\x01", P), TypeError, "request_not_from_a_builder"),
    ("Request copied with a never-allowed service", lambda: dataclasses.replace(LOCAL, payload=b"\x04"), SafetyViolation, "service_never_allowed"),
    ("Request copied to the functional ID", lambda: dataclasses.replace(LOCAL, target=None), SafetyViolation, "manufacturer_service_on_functional_id"),
    ("Request copied with an empty payload", lambda: dataclasses.replace(LOCAL, payload=b""), ValueError, "bad_payload"),
    ("Request copied with a text payload", lambda: dataclasses.replace(LOCAL, payload="21"), ValueError, "bad_payload"),
    ("Request copied with a raw CAN ID target", lambda: dataclasses.replace(LOCAL, target=0x7E0), TypeError, "bad_target"),
    ("Request copied with a bad purpose", lambda: dataclasses.replace(LOCAL, purpose="snapshot"), TypeError, "bad_purpose"),
    ("builder: byte out of range", lambda: rq.read_pid([300], purpose=P), ValueError, "bad_argument"),
    ("builder: word out of range", lambda: rq.read_did(ENGINE, -1, purpose=P), ValueError, "bad_argument"),
    ("builder: raw CAN ID instead of an ECU", lambda: rq.read_local_id(0x7E0, 1, purpose=P), TypeError, "not_an_ecu_entry"),
    ("builder: bad purpose", lambda: rq.read_pid([1], purpose="logging"), TypeError, "bad_purpose"),
    ("builder: no PIDs", lambda: rq.read_pid([], purpose=P), ValueError, "pid_count"),
    ("builder: DTC kind as a number", lambda: rq.read_dtcs(0x04, purpose=P), TypeError, "bad_dtc_kind"),
    ("builder: too many 0x19 parameters", lambda: rq.read_dtc_information(ENGINE, 2, 1, 2, 3, 4, 5, 6, purpose=P), ValueError, "too_many_parameters"),
    ("builder: probe PID", lambda: rq.interlock_probe(0x05), ValueError, "not_a_probe_pid"),
    ("ISO-TP: request needs two frames", lambda: isotp.encode_single_frame(bytes(8), ext_address=None, padding=0), SafetyViolation, "not_single_frame"),
    ("kill switch", lambda: _tripped().check(request="engine 01 0C"), KillSwitchTripped, "kill_switch"),
    ("interlocks: unknown speed source", lambda: Interlocks().update_speed(0, 0.0, source="guess"), ValueError, "unknown_speed_source"),
    ("interlocks: moving", lambda: Interlocks().check(rq.read_dtcs(DtcKind.STORED, purpose=P), 0.0, frozenset()), SafetyViolation, "vehicle_not_confirmed_stationary"),
    ("rate above the ceiling", lambda: RateLimiter({**PURPOSE_RATES, Purpose.LOGGING: 50.0}), ValueError, "rate_above_ceiling"),
    ("channel name", lambda: pc.channel_handle("PCAN_USBBUS99"), ValueError, "bad_channel_name"),
    ("ReadOnlyPcan.set_value", lambda: load_readonly(FakePcanDll()).set_value(HANDLE, pc.PCAN_LISTEN_ONLY, 0), SafetyViolation, "pcan_setting_not_allowed"),
    ("the write function", lambda: _writer()(0x025, bytes(7), purpose="test", kind="request"), SafetyViolation, "frame_length"),
    ("COM port name", lambda: open_serial("/dev/ttyUSB0"), ValueError, "bad_port_name"),
    ("STN command", lambda: stn_policy.check_command("0100"), SafetyViolation, "adapter_hex_request"),
]


def _tripped():
    killswitch = KillSwitch()
    killswitch.trip("hotkey")
    return killswitch


def _writer():
    clock = FakeClock()
    _channel, writer = open_active(
        CHANNEL, library=FakePcanDll(), auditor=Auditor(MemoryAuditSink(), clock), clock=clock
    )
    return writer


@pytest.mark.parametrize(("what", "action", "error", "reason"), SITES, ids=[s[0] for s in SITES])
def test_every_refusal_site_is_audited_once(clock, what, action, error, reason):
    sink = attached(clock)
    with pytest.raises(error):
        action()
    refusals = events(sink, "rejected")
    assert [r["reason"] for r in refusals] == [reason]
    assert refusals[0]["transport"] in {"pcan", "stn", "request", "interlock"}


@pytest.mark.parametrize(
    ("can_id", "data", "kind", "reason"),
    [
        (0x025, bytes.fromhex("02010C0000000000"), "request", "can_id_not_allowlisted"),
        (0x7E0, bytes.fromhex("0000000000000000"), "request", "isotp_malformed"),
        (0x7E0, bytes.fromhex("0214000000000000"), "request", "service_never_allowed"),
        (0x7E0, bytes.fromhex("3000000000000000"), "flow_control", "flow_control_unsolicited"),
        (0x7E0, bytes.fromhex("02010C0000000000"), "consecutive", "unknown_frame_kind"),
    ],
)
def test_the_gates_last_check_audits_its_own_refusals(clock, auditor, sink, can_id, data, kind, reason):
    h = GateHarness(clock, auditor)
    with pytest.raises(SafetyViolation):
        h.gate._transmit(can_id, data, purpose="test", kind=kind)
    [refusal] = events(sink, "rejected")
    assert refusal["reason"] == reason
    assert refusal["request"].startswith(f"0x{can_id:03X}")
    assert h.writer.frames == []


def test_bad_logging_profiles_are_audited(clock, auditor, sink):
    with pytest.raises(ValueError):
        GateHarness(clock, auditor, profile=[rq.read_pid([0x0C], purpose=Purpose.SNAPSHOT)])
    assert events(sink, "rejected") == []  # nothing attached yet: held
    REFUSALS.attach(auditor)
    with pytest.raises(ValueError):
        GateHarness(clock, auditor, profile=["010C"])
    first, second = events(sink, "rejected")
    assert (first["reason"], "held_since" in first) == ("bad_logging_profile", True)
    assert (second["reason"], second["request"]) == ("bad_logging_profile", "str")


def test_passive_open_refusals_are_audited(clock):
    sink = attached(clock)
    dll = FakePcanDll()
    dll.fail_set[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLPARAMVAL
    with pytest.raises(PassiveModeUnconfirmed):
        open_passive(CHANNEL, pcan=load_readonly(dll))
    dll = FakePcanDll()
    dll.readback_listen_only = pc.PCAN_PARAMETER_OFF
    with pytest.raises(PassiveModeUnconfirmed):
        open_passive(CHANNEL, pcan=load_readonly(dll))
    assert [r["reason"] for r in events(sink, "rejected")] == ["listen_only_not_set", "listen_only_not_confirmed"]


def test_failed_session_opens_are_logged(clock, auditor, sink):
    dll = FakePcanDll()
    dll.readback_listen_only = pc.PCAN_PARAMETER_OFF
    with pytest.raises(PassiveModeUnconfirmed):
        open_passive_session(CHANNEL, auditor=auditor, clock=clock, library=dll)
    [refused] = events(sink, "session_refused")
    assert (refused["mode"], refused["reason"]) == ("passive", "PassiveModeUnconfirmed")
    assert [r["reason"] for r in events(sink, "rejected")] == ["listen_only_not_confirmed"]
    with pytest.raises(ValueError):
        rq.read_pid([], purpose=P)
    assert len(events(sink, "rejected")) == 1  # the session detached when it refused to open


def test_failed_polled_opens_are_logged(sim, auditor, sink):
    sim.dll.channel(HANDLE).condition = pc.PCAN_CHANNEL_OCCUPIED
    with pytest.raises(Exception, match="isn't available"):
        open_polled(sim, auditor)
    assert events(sink, "session_refused")[0]["mode"] == "polled"


def test_stn_monitor_check_failures_are_audited(clock, auditor, sink):
    stn = StnAdapter(FakeStnPort(pp21=(0x00, True)), auditor)
    stn.reset()
    with pytest.raises(Exception, match="PP 21"):
        stn.start_can_monitor()
    with pytest.raises(ValueError):
        stn.start_can_monitor("32")
    stn2 = StnAdapter(FakeStnPort(protocol_report="0"), auditor)
    stn2.reset()
    with pytest.raises(Exception, match="STPR"):
        stn2.start_kline_monitor()
    with pytest.raises(ValueError):
        stn2.start_kline_monitor("25")
    reasons = [r["reason"] for r in events(sink, "rejected")]
    assert reasons == ["monitor_checks_failed", "bad_monitor_protocol", "monitor_checks_failed", "bad_monitor_protocol"]
    stn.close()
    stn2.close()


def test_refusals_with_no_session_open_are_held_for_the_next_audit_log(clock):
    with pytest.raises(ValueError):
        rq.read_pid([], purpose=P)
    with pytest.raises(SafetyViolation):
        dataclasses.replace(LOCAL, payload=b"\x11\x01")
    sink = attached(clock)
    held = events(sink, "rejected")
    assert [r["reason"] for r in held] == ["pid_count", "service_never_allowed"]
    assert all("held_since" in r for r in held)


def test_refusal_log_counts_attachments(clock):
    log = RefusalLog()
    sink = MemoryAuditSink()
    auditor = Auditor(sink, clock)
    log.attach(auditor)
    log.attach(auditor)  # a second session sharing the auditor
    log.detach(auditor)
    log.record(transport="pcan", reason="a", detail="", request="")
    log.detach(auditor)
    log.detach(auditor)  # detaching too often is harmless
    log.record(transport="pcan", reason="b", detail="", request="")
    assert [r["reason"] for r in sink.records] == ["a"]
    other = MemoryAuditSink()
    log.attach(Auditor(other, clock))
    assert [r["reason"] for r in other.records] == ["b"]


def test_every_attached_auditor_gets_the_refusal(clock):
    log = RefusalLog()
    first, second = MemoryAuditSink(), MemoryAuditSink()
    log.attach(Auditor(first, clock))
    log.attach(Auditor(second, clock))
    log.record(transport="stn", reason="x", detail="d", request="r")
    assert len(first.records) == len(second.records) == 1


def test_a_full_backlog_reports_what_it_dropped(clock):
    log = RefusalLog(backlog_limit=3)
    for i in range(5):
        log.record(transport="pcan", reason=f"r{i}", detail="", request="")
    sink = MemoryAuditSink()
    log.attach(Auditor(sink, clock))
    assert sink.records[0]["event"] == "refusals_dropped" and sink.records[0]["count"] == 2
    assert [r["reason"] for r in sink.records[1:]] == ["r2", "r3", "r4"]


def test_refuse_still_raises_when_the_audit_log_fails(clock):
    class BrokenSink:
        def write(self, record):
            raise OSError("disk full")

    REFUSALS.attach(Auditor(BrokenSink(), clock))
    with pytest.raises(SafetyViolation) as refused:
        refuse(SafetyViolation("service_never_allowed", "0x04"), transport="pcan")
    assert "could not write this refusal" in refused.value.__notes__[0]


def test_refuse_reason_override_and_default(clock):
    sink = attached(clock)
    with pytest.raises(KeyError):
        refuse(KeyError("x"), transport="pcan")
    with pytest.raises(KeyError):
        refuse(KeyError("y"), transport="pcan", reason="custom")
    assert [r["reason"] for r in events(sink, "rejected")] == ["KeyError", "custom"]


def test_polled_session_refusals_reach_its_audit_log(sim, auditor, sink):
    session = open_polled(sim, auditor)
    with pytest.raises(SafetyViolation):
        session.request(rq.read_local_id(ENGINE, 0x01, purpose=Purpose.LOGGING))
    with pytest.raises(ValueError):
        rq.read_pid([0x100], purpose=Purpose.LOGGING)
    assert [r["reason"] for r in events(sink, "rejected")] == ["not_in_logging_profile", "bad_argument"]
    session.close()
    with pytest.raises(ValueError):
        rq.read_pid([0x100], purpose=Purpose.LOGGING)
    assert len(events(sink, "rejected")) == 2  # detached on close


def test_sessions_that_share_an_auditor_stay_attached_until_both_close(sim, clock):
    sink = MemoryAuditSink()
    auditor = Auditor(sink, clock)
    session = open_passive_session(CHANNEL, auditor=auditor, clock=clock, library=sim.dll)
    stn = StnAdapter(FakeStnPort(), auditor)
    session.close()
    with pytest.raises(SafetyViolation):
        stn._command("STPX")
    stn.close()
    with pytest.raises(SafetyViolation):
        stn_policy.check_command("STPX")
    assert [r["reason"] for r in events(sink, "rejected")] == ["adapter_command_not_allowlisted"]
