"""Rule 11: the audit log."""

import json

from lasto.safety.audit import Auditor, JsonlAuditSink, MemoryAuditSink
from lasto.safety.clock import SystemClock


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


def test_system_clock():
    clock = SystemClock()
    first = clock.monotonic()
    clock.sleep(-1)  # never negative
    clock.sleep(0.001)
    assert clock.monotonic() > first
    assert clock.utc_now().tzinfo is not None
