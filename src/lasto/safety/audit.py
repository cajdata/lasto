"""Audit log (rule 11): every transmitted frame, adapter command, and refusal.

Transmissions are written ahead: the record is stored before the frame or
command goes out, and if the write fails, nothing is transmitted.

Refusals are written where they're raised. Anything in the safety core that
refuses a request, frame, setting, or command calls refuse(), which records
the refusal in REFUSALS and then raises it. Open sessions attach their
Auditor to REFUSALS. A refusal raised while nothing is attached (a bad request
built before any session starts, say) is held and written to the next Auditor
that attaches, so none goes unrecorded.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import UTC, datetime
from typing import NoReturn, Protocol

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
        self._file = open(path, "a", encoding="utf-8", newline="\n")  # held open for the session

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

    def rejected(self, *, transport: str, reason: str, detail: str, request: str, **extra: object) -> None:
        self._write(
            "rejected", {"transport": transport, "reason": reason, "detail": detail, "request": request, **extra}
        )

    def event(self, name: str, **fields: object) -> None:
        self._write(name, fields)


class RefusalLog:
    """Where every refusal in the safety core is recorded."""

    BACKLOG_LIMIT = 10_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attached: dict[int, tuple[Auditor, int]] = {}
        self._backlog: deque[dict[str, object]] = deque(maxlen=self.BACKLOG_LIMIT)
        self._dropped = 0

    def attach(self, auditor: Auditor) -> None:
        """Send refusals to this auditor, starting with any held while nothing was attached.

        Attachments are counted, so two sessions sharing one auditor each attach and detach.
        """
        with self._lock:
            key = id(auditor)
            if key in self._attached:
                self._attached[key] = (auditor, self._attached[key][1] + 1)
                return
            self._attached[key] = (auditor, 1)
            held, self._backlog = list(self._backlog), deque(maxlen=self.BACKLOG_LIMIT)
            dropped, self._dropped = self._dropped, 0
        if dropped:
            auditor.event("refusals_dropped", count=dropped, detail="the held backlog was full")
        for fields in held:
            auditor.rejected(**fields)  # type: ignore[arg-type]

    def detach(self, auditor: Auditor) -> None:
        with self._lock:
            key = id(auditor)
            if key not in self._attached:
                return
            count = self._attached[key][1] - 1
            if count:
                self._attached[key] = (auditor, count)
            else:
                del self._attached[key]

    def record(self, *, transport: str, reason: str, detail: str, request: str) -> None:
        with self._lock:
            auditors = [auditor for auditor, _count in self._attached.values()]
            if not auditors:
                if len(self._backlog) == self.BACKLOG_LIMIT:
                    self._dropped += 1
                self._backlog.append(
                    {
                        "transport": transport,
                        "reason": reason,
                        "detail": detail,
                        "request": request,
                        "held_since": datetime.now(UTC).isoformat(),
                    }
                )
                return
        for auditor in auditors:
            auditor.rejected(transport=transport, reason=reason, detail=detail, request=request)

    def reset(self) -> None:
        """Detach everything and forget held refusals (between tests)."""
        with self._lock:
            self._attached = {}
            self._backlog = deque(maxlen=self.BACKLOG_LIMIT)
            self._dropped = 0


REFUSALS = RefusalLog()


def refuse(error: BaseException, *, transport: str, request: str = "", reason: str | None = None) -> NoReturn:
    """Record a refusal in the audit log, then raise it. The only way the safety core refuses anything."""
    why = reason if reason is not None else getattr(error, "reason", type(error).__name__)
    try:
        REFUSALS.record(transport=transport, reason=why, detail=str(error), request=request)
    except Exception as audit_error:
        error.add_note(f"lasto could not write this refusal to the audit log: {audit_error!r}")
    raise error
