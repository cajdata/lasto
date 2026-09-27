"""Audit log (rule 11): every transmitted frame, adapter command, and rejection.

Transmissions are written ahead: the record is stored before the frame or
command goes out, and if the write fails, nothing is transmitted.
"""

from __future__ import annotations

import json
import os
from typing import Protocol

from lasto.safety.clock import Clock


class AuditSink(Protocol):
    def write(self, record: dict[str, object]) -> None:
        """Store one record durably, or raise."""


class MemoryAuditSink:
    """Keeps records in memory (tests and the simulator)."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)


class JsonlAuditSink:
    """Append-only JSON Lines file. Each record is flushed and fsynced before write() returns."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._file = open(path, "a", encoding="utf-8", newline="\n")  # noqa: SIM115 - held open for the session

    def write(self, record: dict[str, object]) -> None:
        self._file.write(json.dumps(record, sort_keys=True) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        self._file.close()


class Auditor:
    def __init__(self, sink: AuditSink, clock: Clock) -> None:
        self._sink = sink
        self._clock = clock

    def _write(self, event: str, fields: dict[str, object]) -> None:
        record: dict[str, object] = {
            "event": event,
            "utc": self._clock.utc_now().isoformat(),
            "mono": round(self._clock.monotonic(), 6),
        }
        record.update(fields)
        self._sink.write(record)

    def transmit(self, *, transport: str, can_id: int, data: bytes, purpose: str, kind: str) -> None:
        self._write(
            "transmit",
            {
                "transport": transport,
                "can_id": f"0x{can_id:03X}",
                "data": data.hex(" ").upper(),
                "purpose": purpose,
                "kind": kind,
            },
        )

    def adapter_command(self, *, command: str) -> None:
        self._write("adapter_command", {"transport": "stn", "command": command})

    def rejected(self, *, transport: str, reason: str, detail: str, request: str) -> None:
        self._write("rejected", {"transport": transport, "reason": reason, "detail": detail, "request": request})

    def event(self, name: str, **fields: object) -> None:
        self._write(name, fields)
