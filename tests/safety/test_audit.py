"""Rule 11: the audit log."""

import json
import os

import pytest
from helpers import events

from lasto.safety.audit import REFUSALS, Auditor, JsonlAuditSink, MemoryAuditSink
from lasto.safety.clock import SystemClock
from lasto.safety.errors import SafetyViolation


def test_records(clock):
    sink = MemoryAuditSink()
    auditor = Auditor(sink, clock)
    auditor.transmit(transport="pcan", can_id=0x7E0, data=bytes.fromhex("02010C0000000000"), purpose="logging", kind="request")
    auditor.adapter_command(command="STCMM0")
    auditor.rejected(transport="pcan", reason="service_never_allowed", detail="0x04", request="engine 04")
    auditor.event("kill_switch", cause="hotkey")
    transmit, adapter, rejected, kill = sink.records
    assert transmit["event"] == "transmit"
    assert transmit["can_id"] == "0x7E0"
    assert transmit["data"] == "02 01 0C 00 00 00 00 00"
    assert transmit["utc"] == "2026-09-26T18:00:00+00:00"
    assert transmit["mono"] == 1000.0
    assert adapter == adapter | {"event": "adapter_command", "transport": "stn", "command": "STCMM0"}
    assert rejected["reason"] == "service_never_allowed"
    assert kill["cause"] == "hotkey"


def test_jsonl_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    auditor = Auditor(sink, SystemClock())
    auditor.event("session_opened", mode="passive")
    auditor.event("session_closed", mode="passive")
    sink.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["session_opened", "session_closed"]


def test_the_audit_log_must_be_a_regular_file(auditor, sink):
    """Guard v2 (Phase 3, A5): checked with fstat once open. A descriptor passes the guard's path check (its path was
    checked when it was opened), so without this one on a pipe or a console would take the audit log."""
    REFUSALS.attach(auditor)
    read_end, write_end = os.pipe()
    try:
        with pytest.raises(SafetyViolation) as refused:
            JsonlAuditSink(write_end)
        assert refused.value.reason == "audit_file_not_regular"
        with pytest.raises(OSError):
            os.fstat(write_end)  # closed with the refused file
    finally:
        os.close(read_end)
    [refusal] = events(sink, "rejected")
    assert refusal["request"] == f"open the audit log {write_end!r}"


def test_system_clock():
    clock = SystemClock()
    first = clock.monotonic()
    clock.sleep(-1)  # never negative
    clock.sleep(0.001)
    assert clock.monotonic() > first
    assert clock.utc_now().tzinfo is not None
