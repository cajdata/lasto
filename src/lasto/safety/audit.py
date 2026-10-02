"""Audit log (rule 11): every transmitted frame, adapter command, and refusal.

Transmissions are written ahead: the record is stored before the frame or
command goes out, and if the write fails, nothing is transmitted.

Refusals are written where they're raised. Anything in the safety core that
refuses a request, frame, setting, or command calls refuse(), which records
the refusal in REFUSALS and then raises it. Open sessions attach their
Auditor to REFUSALS. A refusal raised while nothing is attached (a bad request
built before any session starts, say) is held and written to the next Auditor
that attaches, so none goes unrecorded. Process-wide safety events, such as a
kill-switch trip, go through REFUSALS the same way.
"""

from __future__ import annotations

import atexit
import json
import os
import stat
import sys
import threading
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn, Protocol, TextIO

from lasto.safety._frozen import SealedProtocolType, SealedType, freeze
from lasto.safety.errors import SafetyViolation

if TYPE_CHECKING:  # clock imports this module, to refuse a clock
    from lasto.safety.clock import Clock

# Refusals held while no audit log is attached, before the oldest are dropped (and counted).
REFUSAL_BACKLOG_LIMIT = 10_000


class AuditSink(Protocol, metaclass=SealedProtocolType):
    def write(self, record: dict[str, object]) -> None:
        """Store one record durably, or raise."""


class MemoryAuditSink(metaclass=SealedType):
    """Keeps records in memory (tests and the simulator)."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[dict[str, object]] = []

    @property
    def records(self) -> list[dict[str, object]]:
        return self._records

    def write(self, record: dict[str, object]) -> None:
        self._records.append(record)


def _open_regular_file(path: str | os.PathLike[str], *, request: str) -> TextIO:
    """Open `path` to append, then refuse (audited) anything fstat doesn't report as a regular file (guard v2, A5).

    Guard v2 checks a path before it opens, but a descriptor passes that check, and this holds whatever the check
    missed: an audit file never lands on a device, a console, or a pipe.
    """
    file = open(path, "a", encoding="utf-8", newline="\n")
    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
        file.close()
        refuse(SafetyViolation("audit_file_not_regular", f"{path!r} isn't a regular file"), transport="core", request=request)
    return file


class JsonlAuditSink(metaclass=SealedType):
    """Append-only JSON Lines file. Each record is flushed and fsynced before write() returns."""

    __slots__ = ("_file",)

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._file = _open_regular_file(path, request=f"open the audit log {path!r}")  # held open for the session

    def write(self, record: dict[str, object]) -> None:
        self._file.write(json.dumps(record, sort_keys=True) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        self._file.close()


class Auditor(metaclass=SealedType):
    __slots__ = ("_clock", "_sink")

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


def _write(auditor: Auditor, kind: str, fields: dict[str, object]) -> None:
    if kind == "rejected":
        auditor.rejected(**fields)  # type: ignore[arg-type]
    else:
        auditor.event(kind, **fields)


class RefusalLog(metaclass=SealedType):
    """Where every refusal in the safety core is recorded, and every process-wide safety event (a kill).

    Both go to every attached auditor, or are held for the next one to attach.
    A failing audit log loses nothing (finding #8):
    - a record goes to every attached auditor even if one of them fails, and
      one that none of them could take is held;
    - an auditor that attaches takes the held records one at a time; if a
      write fails, the rest stay held and the auditor isn't attached;
    - anything still held when the process exits is reported on stderr;
    - once hold_on_disk() names a file, every record is also appended to it,
      and fsynced, the moment it is held, so a crash can't lose one the file
      took. One the file couldn't take (a full disk) stays held in memory, is
      counted, and is reported at exit, but a crash before then loses it.
    """

    __slots__ = ("_attached", "_backlog", "_backlog_limit", "_dropped", "_held_file", "_held_path", "_held_unwritten", "_lock")

    def __init__(self, *, backlog_limit: int = REFUSAL_BACKLOG_LIMIT) -> None:
        self._lock = threading.Lock()
        self._attached: dict[int, tuple[Auditor, int]] = {}
        self._backlog_limit = backlog_limit
        # Held records: ("rejected", fields) or (event name, fields).
        self._backlog: deque[tuple[str, dict[str, object]]] = deque(maxlen=backlog_limit)
        self._dropped = 0
        self._held_file: TextIO | None = None
        self._held_path = ""
        self._held_unwritten = 0  # held records the file couldn't take

    def hold_on_disk(self, path: str | os.PathLike[str]) -> None:
        """Also append every held record to this file, fsynced as it is held. Set once; a second call is refused."""
        with self._lock:
            already = self._held_path
        if already:
            refuse(
                SafetyViolation("held_records_file_already_set", f"held records already go to {already}"),
                transport="core",
                request=f"hold records in {path}",
            )
        held_file = _open_regular_file(path, request=f"hold records in {path}")
        with self._lock:
            self._held_file, self._held_path = held_file, str(path)

    def attach(self, auditor: Auditor) -> None:
        """Send refusals to this auditor, starting with any held while nothing was attached.

        Attachments are counted, so two sessions sharing one auditor each attach and detach. If
        writing the held records fails, the rest stay held, the auditor isn't attached, and the
        error is raised.
        """
        key = id(auditor)
        while True:
            with self._lock:
                if key in self._attached:
                    self._attached[key] = (auditor, self._attached[key][1] + 1)
                    return
                if not self._backlog and not self._dropped:
                    self._attached[key] = (auditor, 1)
                    return
                held, self._backlog = list(self._backlog), deque(maxlen=self._backlog_limit)
                dropped, self._dropped = self._dropped, 0
            self._flush(auditor, held, dropped)

    def _flush(self, auditor: Auditor, held: list[tuple[str, dict[str, object]]], dropped: int) -> None:
        written = 0
        try:
            if dropped:
                auditor.event("refusals_dropped", count=dropped, detail="the held backlog was full")
                dropped = 0
            for kind, fields in held:
                _write(auditor, kind, fields)
                written += 1
        except BaseException:
            with self._lock:
                # Back in front of anything held since; the oldest go first if that overflows the backlog.
                merged = [*held[written:], *self._backlog]
                overflow = max(0, len(merged) - self._backlog_limit)
                self._backlog = deque(merged[overflow:], maxlen=self._backlog_limit)
                self._dropped += dropped + overflow
            raise

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
        self._deliver("rejected", {"transport": transport, "reason": reason, "detail": detail, "request": request})

    def event(self, name: str, **fields: object) -> None:
        """A safety event every open audit log should have, such as a kill-switch trip."""
        self._deliver(name, fields)

    def _deliver(self, kind: str, fields: dict[str, object]) -> None:
        """Write to every attached auditor. Held if none could take it; the first failure is raised afterwards."""
        with self._lock:
            auditors = [auditor for auditor, _count in self._attached.values()]
            if not auditors:
                self._hold(kind, fields)
                return
        failures: list[Exception] = []
        for auditor in auditors:
            try:
                _write(auditor, kind, fields)
            except Exception as exc:
                failures.append(exc)
        if len(failures) == len(auditors):
            with self._lock:
                self._hold(kind, fields)
        if failures:
            raise failures[0]

    def _hold(self, kind: str, fields: dict[str, object]) -> None:
        if len(self._backlog) == self._backlog_limit:
            self._dropped += 1
        record = {**fields, "held_since": datetime.now(UTC).isoformat()}
        self._backlog.append((kind, record))
        if self._held_file is None:
            return
        try:
            self._held_file.write(json.dumps({"kind": kind, **record}, sort_keys=True, default=str) + "\n")
            self._held_file.flush()
            os.fsync(self._held_file.fileno())
        except (OSError, ValueError):  # the disk failed, or the file was closed under it
            self._held_unwritten += 1  # still held in memory, and reported at exit

    def report_held(self, stream: TextIO | None = None) -> None:
        """Write anything still held to stderr (or `stream`). Runs at exit, so no held record goes unseen."""
        with self._lock:
            held, dropped, path, unwritten = list(self._backlog), self._dropped, self._held_path, self._held_unwritten
        if not held and not dropped:
            return
        out = sys.stderr if stream is None else stream
        more = f"; {dropped} more were dropped" if dropped else ""
        out.write(f"lasto: {len(held)} audit records were never written to an audit log{more}:\n")
        if path:
            missing = f", but {unwritten} of them couldn't be written to {path}" if unwritten else ""
            out.write(f"lasto: they are also in {path}{missing}\n")
        for kind, fields in held:
            out.write(json.dumps({"kind": kind, **fields}, sort_keys=True, default=str) + "\n")

    def reset(self) -> None:
        """Detach everything, forget held refusals, and close the held-records file (between tests)."""
        with self._lock:
            self._attached = {}
            self._backlog = deque(maxlen=self._backlog_limit)
            self._dropped = 0
            held_file, self._held_file, self._held_path, self._held_unwritten = self._held_file, None, "", 0
        if held_file is not None:
            held_file.close()


REFUSALS = RefusalLog()
atexit.register(REFUSALS.report_held)


def hold_on_disk(path: str | os.PathLike[str]) -> None:
    """Keep every record held while no audit log is attached in this file too, appended and fsynced as it is
    held, so a crash can't lose one the file took. One it couldn't take stays held in memory, counted, and is
    reported at exit. Called once per process, at startup; a second call is refused."""
    REFUSALS.hold_on_disk(path)


def refuse(error: BaseException, *, transport: str, request: str = "", reason: str | None = None) -> NoReturn:
    """Record a refusal in the audit log, then raise it. The only way the safety core refuses anything."""
    why = reason if reason is not None else getattr(error, "reason", type(error).__name__)
    try:
        REFUSALS.record(transport=transport, reason=why, detail=str(error), request=request)
    except Exception as audit_error:
        error.add_note(f"lasto could not write this refusal to the audit log: {audit_error!r}")
    raise error


def require_durable(auditor: object, *, request: str) -> None:
    """Refuse (audited) any audit log but an Auditor writing a JsonlAuditSink. For real hardware (finding L5).

    That sink fsyncs each record before write() returns, which is what makes the transmit record
    write-ahead. Any other sink, a subclass or a look-alike included, could hold records in memory or drop
    them, so rule 11 would depend on the caller. The simulator may use any sink.
    """
    if type(auditor) is Auditor and type(auditor._sink) is JsonlAuditSink:
        return
    given = type(auditor._sink).__name__ if type(auditor) is Auditor else type(auditor).__name__  # type: ignore[attr-defined]
    refuse(
        SafetyViolation(
            "audit_log_not_durable", f"{given}; on real hardware the audit log is a JsonlAuditSink, fsynced per record"
        ),
        transport="pcan",
        request=request,
    )


freeze(__name__)
