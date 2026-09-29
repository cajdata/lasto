"""Indexes a run's audit log into the capture database's audit table, as the log grows (docs/architecture.md §4).

The JSON Lines file stays the record of truth (rule 11). The index is there for reading, and can always
be rebuilt from the file.
- Only complete lines are indexed. A line still being written, or one a crash tore, waits for its newline.
- A line that isn't an audit record (damaged on disk) is indexed as unreadable, with its text, so the
  index still matches the file line for line.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from lasto.storage import capture_db
from lasto.storage.capture_db import AuditLine


def _is_utc_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _parse(run_id: str, line: int, raw: bytes) -> AuditLine:
    text = raw.decode("utf-8", errors="replace")
    try:
        record = json.loads(text)
    except ValueError:
        record = None
    if isinstance(record, dict) and isinstance(record.get("event"), str) and _is_utc_time(record.get("utc")):
        return AuditLine(run_id, line, record["utc"], record["event"], text)
    return AuditLine(run_id, line, "", capture_db.UNREADABLE, text)


class AuditIndex:
    """Keeps one run's audit table caught up with its audit log, reading only what the log has added."""

    def __init__(self, conn: sqlite3.Connection, run_id: str, path: Path) -> None:
        self._conn = conn
        self._run_id = run_id
        self._path = path
        self._offset = 0  # where the next unread line starts in the file
        self._line = 0  # lines read so far
        self._indexed = capture_db.audit_lines_indexed(conn, run_id)

    def catch_up(self) -> int:
        """Index every complete line added since the last call. Returns how many were indexed."""
        try:
            with open(self._path, "rb") as file:
                file.seek(self._offset)
                data = file.read()
        except FileNotFoundError:
            return 0
        complete = data[: data.rfind(b"\n") + 1]
        line = self._line
        lines = []
        for raw in complete.split(b"\n")[:-1]:
            line += 1
            if line > self._indexed:
                lines.append(_parse(self._run_id, line, raw))
        capture_db.add_audit_lines(self._conn, lines)  # one transaction; if it fails, the next call reads these again
        self._line = line
        self._offset += len(complete)
        self._indexed = max(self._indexed, line)
        return len(lines)
