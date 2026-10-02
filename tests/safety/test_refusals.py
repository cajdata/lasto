"""Rule 11: every refusal is audited where it is raised, exactly once."""

import ctypes
import dataclasses
import io
import json
import os
import subprocess
import sys

import pytest
from helpers import CHANNEL, HANDLE, GateHarness, events, open_polled, records_in

from lasto.safety import audit, isotp, pcan_dll
from lasto.safety import pcan_constants as pc
from lasto.safety import requests as rq
from lasto.safety import stn_policy
from lasto.safety.audit import REFUSALS, Auditor, MemoryAuditSink, RefusalLog, refuse
from lasto.safety.ecus import ENGINE
from lasto.safety.errors import InterfaceError, KillSwitchTripped, PassiveModeUnconfirmed, SafetyViolation
from lasto.safety.interlocks import Interlocks
from lasto.safety.killswitch import KillSwitch
from lasto.safety.pcan_active import open_active
from lasto.safety.pcan_dll import PcanChannel, load_readonly
from lasto.safety.pcan_passive import open_passive
from lasto.safety.ratelimit import PURPOSE_RATES, RateLimiter
from lasto.safety.requests import DtcKind, Purpose
from lasto.safety.session import open_passive_session
from lasto.safety.stn_port import StnAdapter, open_adapter
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
    ("Request copied", lambda: dataclasses.replace(LOCAL, payload=b"\x21\x02"), TypeError, "request_not_from_a_builder"),  # N6
    ("Request with a builder's token and a never-allowed service", lambda: rq.Request(ENGINE, b"\x04", P, rq._BUILDER), SafetyViolation, "service_never_allowed"),
    ("Request with a builder's token, to the functional ID", lambda: rq.Request(None, b"\x21\x01", P, rq._BUILDER), SafetyViolation, "manufacturer_service_on_functional_id"),
    ("Request with a builder's token and an empty payload", lambda: rq.Request(ENGINE, b"", P, rq._BUILDER), ValueError, "bad_payload"),
    ("Request with a builder's token and a text payload", lambda: rq.Request(ENGINE, "21", P, rq._BUILDER), ValueError, "bad_payload"),
    ("Request with a builder's token and a raw CAN ID target", lambda: rq.Request(0x7E0, b"\x21\x01", P, rq._BUILDER), TypeError, "bad_target"),
    ("Request with a builder's token and a bad purpose", lambda: rq.Request(ENGINE, b"\x21\x01", "snapshot", rq._BUILDER), TypeError, "bad_purpose"),
    ("builder: byte out of range", lambda: rq.read_pid([300], purpose=P), ValueError, "bad_argument"),
    ("builder: word out of range", lambda: rq.read_did(ENGINE, -1, purpose=P), ValueError, "bad_argument"),
    ("builder: raw CAN ID instead of an ECU", lambda: rq.read_local_id(0x7E0, 1, purpose=P), TypeError, "not_an_ecu_entry"),
    ("builder: bad purpose", lambda: rq.read_pid([1], purpose="logging"), TypeError, "bad_purpose"),
    ("builder: no PIDs", lambda: rq.read_pid([], purpose=P), ValueError, "pid_count"),
    ("builder: PIDs that aren't a list", lambda: rq.read_pid(12, purpose=P), TypeError, "bad_argument"),  # finding #9
    ("builder: probe PID as a float", lambda: rq.interlock_probe(13.0), ValueError, "bad_argument"),  # finding #9
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
    ("COM port name", lambda: open_adapter("/dev/ttyUSB0", auditor=Auditor(MemoryAuditSink(), FakeClock())), ValueError, "bad_port_name"),
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


def _faulty(**faults):
    dll = FakePcanDll(api_version=faults.pop("api_version", "4.7.0.11"))
    for name, value in faults.items():
        if name in ("fail_get", "fail_set"):
            getattr(dll, name).update(value)
        elif name == "condition":
            dll.channel(HANDLE).condition = value
        else:
            setattr(dll, name, value)
    return dll


def _reporting_on_a_bare_channel(dll):
    pcan = load_readonly(dll)
    pcan.initialize(HANDLE, pc.PCAN_BAUD_500K)
    PcanChannel(pcan, HANDLE, CHANNEL, "4.7.0.11").enable_reporting()


def _active(dll):
    clock = FakeClock()
    return open_active(CHANNEL, library=dll, auditor=Auditor(MemoryAuditSink(), clock), clock=clock)


def _failed_write(dll):
    _channel, writer = _active(dll)
    dll.write_status = pc.PCAN_ERROR_XMTFULL
    writer(0x7E0, bytes.fromhex("02010C0000000000"), purpose="test", kind="request")


# Finding #7: every refusal to open or use a PCAN channel. (what, faults, action, audited reason)
INTERFACE_REFUSALS = [
    ("32-bit Python", {}, lambda dll: pcan_dll.require_64_bit(4), "python_not_64_bit"),
    ("driver version unreadable", {"fail_get": {pc.PCAN_API_VERSION: pc.PCAN_ERROR_NODRIVER}}, lambda dll: pcan_dll.check_driver(load_readonly(dll)), "driver_version_unreadable"),
    ("driver not supported", {"api_version": "5.0.0.1"}, lambda dll: pcan_dll.check_driver(load_readonly(dll)), "driver_not_supported"),
    ("channel condition unreadable", {"fail_get": {pc.PCAN_CHANNEL_CONDITION: pc.PCAN_ERROR_ILLHW}}, lambda dll: pcan_dll.check_available(load_readonly(dll), HANDLE, CHANNEL), "channel_condition_unreadable"),
    ("channel in use", {"condition": pc.PCAN_CHANNEL_PCANVIEW}, lambda dll: pcan_dll.check_available(load_readonly(dll), HANDLE, CHANNEL), "channel_not_available"),
    ("error reporting refused", {"fail_set": {pc.PCAN_ALLOW_ERROR_FRAMES: pc.PCAN_ERROR_ILLPARAMVAL}}, _reporting_on_a_bare_channel, "reporting_not_enabled"),
    ("passive initialize failed", {"initialize_status": pc.PCAN_ERROR_ILLHW}, lambda dll: open_passive(CHANNEL, pcan=load_readonly(dll)), "initialize_failed"),
    ("polled listen-only not cleared", {"fail_set": {pc.PCAN_LISTEN_ONLY: pc.PCAN_ERROR_ILLPARAMVAL}}, _active, "listen_only_not_cleared"),
    ("polled initialize failed", {"initialize_status": pc.PCAN_ERROR_ILLHW}, _active, "initialize_failed"),
    ("polled channel not in normal mode", {"readback_listen_only": pc.PCAN_PARAMETER_ON}, _active, "not_in_normal_mode"),
    ("a write the driver refused", {}, _failed_write, "can_write_failed"),
]


@pytest.mark.parametrize(("what", "faults", "action", "reason"), INTERFACE_REFUSALS, ids=[r[0] for r in INTERFACE_REFUSALS])
def test_every_interface_refusal_is_audited_once(clock, what, faults, action, reason):
    sink = attached(clock)
    with pytest.raises(InterfaceError):
        action(_faulty(**faults))
    refusals = events(sink, "rejected")
    assert [(r["reason"], r["transport"]) for r in refusals] == [(reason, "pcan")]


def test_a_dll_that_will_not_load_is_audited(clock, monkeypatch):
    def missing(path):
        raise OSError("not found")

    monkeypatch.setattr(ctypes, "WinDLL", missing)
    sink = attached(clock)
    with pytest.raises(InterfaceError, match="could not load"):
        pcan_dll.load_library()
    assert [r["reason"] for r in events(sink, "rejected")] == ["dll_not_loaded"]


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
    assert reasons == ["adapter_acks_by_default", "bad_monitor_protocol", "adapter_unexpected_answer", "bad_monitor_protocol"]
    stn.close()
    stn2.close()


def test_refusals_with_no_session_open_are_held_for_the_next_audit_log(clock):
    with pytest.raises(ValueError):
        rq.read_pid([], purpose=P)
    with pytest.raises(SafetyViolation):
        rq.Request(ENGINE, b"\x11\x01", P, rq._BUILDER)
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


# ---- finding #8: a failing audit log loses nothing ----


class FailingSink:
    """Stores records until it has taken `good` of them, then raises on every write."""

    def __init__(self, good=0):
        self.records = []
        self.good = good

    def write(self, record):
        if len(self.records) >= self.good:
            raise OSError("disk full")
        self.records.append(record)


def _record(log, reason):
    log.record(transport="pcan", reason=reason, detail="", request="")


def test_one_failing_audit_log_does_not_keep_a_refusal_from_the_others(clock):
    log = RefusalLog()
    broken, good = FailingSink(), MemoryAuditSink()
    log.attach(Auditor(broken, clock))
    log.attach(Auditor(good, clock))
    with pytest.raises(OSError):
        _record(log, "a")  # the failure still surfaces, for refuse() to note on the error
    assert [r["reason"] for r in good.records] == ["a"]


def test_a_refusal_no_audit_log_could_take_is_held_for_the_next(clock):
    log = RefusalLog()
    broken = Auditor(FailingSink(), clock)
    log.attach(broken)
    with pytest.raises(OSError):
        _record(log, "a")
    with pytest.raises(OSError):
        log.event("kill_switch", cause="hotkey")  # events too
    log.detach(broken)
    good = MemoryAuditSink()
    log.attach(Auditor(good, clock))
    assert [(r["event"], r.get("reason")) for r in good.records] == [("rejected", "a"), ("kill_switch", None)]


def test_a_flush_that_fails_part_way_keeps_the_rest_and_does_not_attach(clock):
    log = RefusalLog()
    for reason in ("a", "b", "c"):
        _record(log, reason)  # nothing attached: held
    flaky = FailingSink(good=1)
    with pytest.raises(OSError):
        log.attach(Auditor(flaky, clock))
    assert [r["reason"] for r in flaky.records] == ["a"]
    _record(log, "d")  # the flaky log didn't attach, so this is held too
    good = MemoryAuditSink()
    log.attach(Auditor(good, clock))
    assert [r["reason"] for r in good.records] == ["b", "c", "d"]


def test_a_failed_flush_keeps_the_count_of_what_was_dropped(clock):
    log = RefusalLog(backlog_limit=1)
    _record(log, "a")
    _record(log, "b")  # "a" is dropped
    with pytest.raises(OSError):
        log.attach(Auditor(FailingSink(), clock))
    good = MemoryAuditSink()
    log.attach(Auditor(good, clock))
    assert good.records[0]["event"] == "refusals_dropped" and good.records[0]["count"] == 1
    assert [r["reason"] for r in good.records[1:]] == ["b"]


def test_records_still_held_at_exit_are_reported(clock):
    log = RefusalLog()
    _record(log, "a")
    log.event("kill_switch", cause="hotkey")
    report = io.StringIO()
    log.report_held(report)
    lines = report.getvalue().splitlines()
    assert "2 audit records" in lines[0]
    assert [json.loads(line)["kind"] for line in lines[1:]] == ["rejected", "kill_switch"]
    empty = io.StringIO()
    RefusalLog().report_held(empty)
    assert empty.getvalue() == ""
    full = RefusalLog(backlog_limit=1)
    _record(full, "a")
    _record(full, "b")
    report = io.StringIO()
    full.report_held(report)
    assert "1 audit records were never written to an audit log; 1 more were dropped" in report.getvalue()


HELD_AT_EXIT = """
from lasto.safety import requests
try:
    requests.read_pid([], purpose=requests.Purpose.LOGGING)  # refused with no session open: held
except ValueError:
    pass
"""


def test_a_process_that_exits_with_refusals_held_reports_them():
    result = subprocess.run([sys.executable, "-c", HELD_AT_EXIT], capture_output=True, text=True, check=True)
    assert "1 audit records" in result.stderr and "pid_count" in result.stderr


# ---- held records on disk (Phase 2, step B2) ----


def test_held_records_go_to_disk_as_they_are_held(clock, tmp_path):
    log = RefusalLog()
    log.hold_on_disk(tmp_path / "held.jsonl")
    _record(log, "a")
    log.event("kill_switch", cause="hotkey")
    on_disk = records_in(tmp_path / "held.jsonl")
    assert [(r["kind"], r.get("reason")) for r in on_disk] == [("rejected", "a"), ("kill_switch", None)]
    assert all("held_since" in r for r in on_disk)
    log.attach(Auditor(MemoryAuditSink(), clock))
    _record(log, "b")  # an audit log took it, so it wasn't held
    assert len(records_in(tmp_path / "held.jsonl")) == 2
    log.reset()


def test_a_record_no_audit_log_could_take_goes_to_disk(clock, tmp_path):
    log = RefusalLog()
    log.hold_on_disk(tmp_path / "held.jsonl")
    log.attach(Auditor(FailingSink(), clock))
    with pytest.raises(OSError):
        _record(log, "a")
    assert [r["reason"] for r in records_in(tmp_path / "held.jsonl")] == ["a"]
    log.reset()


def test_the_held_records_file_is_set_once(clock, tmp_path):
    sink = attached(clock)
    log = RefusalLog()
    log.hold_on_disk(tmp_path / "held.jsonl")
    with pytest.raises(SafetyViolation) as refused:
        log.hold_on_disk(tmp_path / "elsewhere.jsonl")
    assert refused.value.reason == "held_records_file_already_set"
    assert [(r["reason"], r["transport"]) for r in events(sink, "rejected")] == [("held_records_file_already_set", "core")]
    assert not (tmp_path / "elsewhere.jsonl").exists()
    log.reset()


def test_the_held_records_file_must_be_a_regular_file(clock, tmp_path):
    """Guard v2 (Phase 3, A5): checked with fstat once open, as for the audit log itself."""
    sink = attached(clock)
    log = RefusalLog()
    read_end, write_end = os.pipe()
    try:
        with pytest.raises(SafetyViolation) as refused:
            log.hold_on_disk(write_end)
        assert refused.value.reason == "audit_file_not_regular"
    finally:
        os.close(read_end)
    assert [(r["reason"], r["request"]) for r in events(sink, "rejected")] == [
        ("audit_file_not_regular", f"hold records in {write_end}")
    ]
    log.hold_on_disk(tmp_path / "held.jsonl")  # the refused file was never set
    log.reset()


def test_a_failed_disk_write_keeps_the_record_held_and_says_so(clock, tmp_path):
    log = RefusalLog()
    log.hold_on_disk(tmp_path / "held.jsonl")
    log._held_file.close()  # the disk goes away under it
    _record(log, "a")
    report = io.StringIO()
    log.report_held(report)
    assert "1 audit records were never written to an audit log" in report.getvalue()
    assert f"1 of them couldn't be written to {tmp_path / 'held.jsonl'}" in report.getvalue()
    good = MemoryAuditSink()
    log.attach(Auditor(good, clock))
    assert [r["reason"] for r in good.records] == ["a"]  # still held in memory, for the next audit log
    log.reset()


def test_the_exit_report_says_where_the_held_records_are(tmp_path):
    log = RefusalLog()
    log.hold_on_disk(tmp_path / "held.jsonl")
    _record(log, "a")
    report = io.StringIO()
    log.report_held(report)
    assert f"they are also in {tmp_path / 'held.jsonl'}" in report.getvalue()
    log.reset()


def test_reset_closes_the_held_records_file_for_the_next_test(tmp_path):
    log = RefusalLog()
    log.hold_on_disk(tmp_path / "first.jsonl")
    log.reset()
    log.hold_on_disk(tmp_path / "second.jsonl")  # allowed again after a reset (between tests)
    _record(log, "a")
    assert records_in(tmp_path / "first.jsonl") == [] and len(records_in(tmp_path / "second.jsonl")) == 1
    log.reset()


def test_the_public_function_sets_the_process_refusal_log(tmp_path):
    audit.hold_on_disk(tmp_path / "held.jsonl")  # the test plugin's reset closes it after the test
    with pytest.raises(ValueError):
        rq.read_pid([], purpose=P)  # nothing attached: held
    assert [r["reason"] for r in records_in(tmp_path / "held.jsonl")] == ["pid_count"]


HELD_THEN_CRASH = """
import os, sys
from lasto.safety import audit, requests
from lasto.safety.serial_guard import allow_writes_in

allow_writes_in(os.path.dirname(sys.argv[1]))  # guard v2: the folder this process writes in, as lasto's commands do
audit.hold_on_disk(sys.argv[1])
try:
    requests.read_pid([], purpose=requests.Purpose.LOGGING)  # refused with no session open: held
except ValueError:
    pass
os._exit(1)  # a crash: no exit handlers, no stderr report
"""


def test_a_held_record_survives_a_crash(tmp_path):
    """The point of the file: records held in memory are lost when the process dies before it can report them."""
    path = tmp_path / "held.jsonl"
    result = subprocess.run([sys.executable, "-c", HELD_THEN_CRASH, str(path)], capture_output=True, text=True)
    assert result.returncode == 1 and result.stderr == ""
    assert [r["reason"] for r in records_in(path)] == ["pid_count"]


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
