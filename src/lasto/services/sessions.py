"""Captured sessions, for lasto log and later the GUI (docs/architecture.md §4 and §14.3).

Everything here reads the capture database read-only, in short queries, and reaches no hardware. Per-ID
statistics for a session are computed from its per-second rollups (id_seconds), without reading raw
frames.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from lasto.storage import capture_db
from lasto.storage.capture_lock import capture_running
from lasto.storage.live_db import LiveStatus, read_live
from lasto.storage.root import DataRoot

_ID_TEXT = re.compile(r"[0-9a-f-]{1,36}")


class SessionNotFound(LookupError):
    """No session matches, or more than one does."""


@dataclass(frozen=True)
class SessionSummary:
    id: str
    run_id: str
    state: str  # "open" while capturing, "closed", or "recovered"
    end_reason: str | None
    started_utc: str
    ended_utc: str | None
    seconds: float  # from its first frame to its last
    frames: int
    error_frames: int
    ids: int  # distinct CAN IDs
    stored_bytes: int

    @property
    def mb_per_hour(self) -> float | None:
        return None if self.seconds <= 0 else self.stored_bytes / 1e6 / (self.seconds / 3600)


@dataclass(frozen=True)
class RunInfo:
    id: str
    interface: str
    channel: str
    hardware: str | None
    api_version: str | None
    channel_version: str | None
    started_utc: str
    ended_utc: str | None
    end_reason: str | None


@dataclass(frozen=True)
class Event:
    host_utc: str
    hw_us: int | None
    kind: str
    detail: dict[str, object]


@dataclass(frozen=True)
class AuditEntry:
    line: int
    utc: str
    event: str
    record: dict[str, object]  # the whole record; an unreadable line is {"text": ...}


@dataclass(frozen=True)
class SessionDetail:
    summary: SessionSummary
    run: RunInfo
    events: tuple[Event, ...]  # the session's
    run_events: tuple[Event, ...]  # the run's, while no session was open
    audit: tuple[AuditEntry, ...]  # the run's audit log


@dataclass(frozen=True)
class IdStats:
    """One CAN ID over a session."""

    can_id: int
    extended: bool
    frames: int
    first_s: float  # seconds from the session's first frame
    last_s: float
    rate_hz: float | None  # frames per second over the whole session
    period_ms: float | None  # the mean time between its frames
    gap_min_ms: float | None
    gap_max_ms: float | None
    dlc_min: int
    dlc_max: int
    changed_bits: bytes  # 8 bytes: a bit set for every data bit that changed during the session


@contextmanager
def _reading(root: DataRoot) -> Iterator[sqlite3.Connection | None]:
    try:
        conn = capture_db.read_capture(root)
    except FileNotFoundError:
        yield None
        return
    try:
        yield conn
    finally:
        conn.close()


_SUMMARY = (
    "SELECT s.id, s.run_id, s.state, s.end_reason, s.started_utc, s.ended_utc,"
    " coalesce(s.last_hw_us - s.base_hw_us, 0) / 1e6, s.frames, s.error_frames,"
    " (SELECT count(*) FROM (SELECT DISTINCT can_id, extended FROM id_seconds WHERE session = s.key)),"
    " s.stored_bytes FROM sessions s"
)


def list_sessions(root: DataRoot) -> list[SessionSummary]:
    """Every session, newest first."""
    with _reading(root) as conn:
        if conn is None:
            return []
        return [SessionSummary(*row) for row in conn.execute(f"{_SUMMARY} ORDER BY s.started_utc DESC, s.key DESC")]


def find_session(root: DataRoot, which: str) -> SessionSummary:
    """A session by "last", its ID, or the first characters of its ID."""
    with _reading(root) as conn:
        return _find(conn, which)


def _find(conn: sqlite3.Connection | None, which: str) -> SessionSummary:
    if conn is None or conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0:
        raise SessionNotFound("no sessions have been captured yet")
    if which == "last":
        rows = conn.execute(f"{_SUMMARY} ORDER BY s.started_utc DESC, s.key DESC LIMIT 1").fetchall()
    elif _ID_TEXT.fullmatch(which):
        rows = conn.execute(f"{_SUMMARY} WHERE s.id LIKE ? || '%' ORDER BY s.key LIMIT 2", (which,)).fetchall()
    else:
        rows = []
    if not rows:
        raise SessionNotFound(f"no session matches {which!r}")
    if len(rows) > 1:
        raise SessionNotFound(f"{which!r} matches more than one session; give more of its ID")
    return SessionSummary(*rows[0])


def _events(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]) -> tuple[Event, ...]:
    return tuple(
        Event(host_utc, hw_us, kind, json.loads(detail)) for host_utc, hw_us, kind, detail in conn.execute(sql, parameters)
    )


def _audit_record(text: str) -> dict[str, object]:
    try:
        record = json.loads(text)
    except ValueError:
        return {"text": text}
    return record if isinstance(record, dict) else {"text": text}


def session_detail(root: DataRoot, which: str) -> SessionDetail:
    """A session with its run, its events, and its run's audit log. `which` is as for find_session."""
    with _reading(root) as conn:
        summary = _find(conn, which)
        assert conn is not None  # for the type checker: _find refuses None
        key = conn.execute("SELECT key FROM sessions WHERE id = ?", (summary.id,)).fetchone()[0]
        run = RunInfo(
            *conn.execute(
                "SELECT id, interface, channel, hardware, api_version, channel_version, started_utc, ended_utc,"
                " end_reason FROM runs WHERE id = ?",
                (summary.run_id,),
            ).fetchone()
        )
        events = _events(conn, "SELECT host_utc, hw_us, kind, detail FROM events WHERE session = ? ORDER BY id", (key,))
        run_events = _events(
            conn,
            "SELECT host_utc, hw_us, kind, detail FROM events WHERE run_id = ? AND session IS NULL ORDER BY id",
            (summary.run_id,),
        )
        audit = tuple(
            AuditEntry(line, utc, event, _audit_record(record))
            for line, utc, event, record in conn.execute(
                "SELECT line, utc, event, record FROM audit WHERE run_id = ? ORDER BY line", (summary.run_id,)
            )
        )
    return SessionDetail(summary, run, events, run_events, audit)


def bus_stats(root: DataRoot, which: str) -> list[IdStats]:
    """Every CAN ID in a session: how often it came, how regularly, how long, and which bits changed."""
    with _reading(root) as conn:
        summary = _find(conn, which)
        assert conn is not None  # for the type checker: _find refuses None
        key, base = conn.execute("SELECT key, base_hw_us FROM sessions WHERE id = ?", (summary.id,)).fetchone()
        changed: dict[tuple[int, int], int] = {}
        for can_id, extended, bits in conn.execute(
            "SELECT can_id, extended, changed_bits FROM id_seconds WHERE session = ?", (key,)
        ):
            changed[can_id, extended] = changed.get((can_id, extended), 0) | int.from_bytes(bits, "big")
        rows = conn.execute(
            "SELECT can_id, extended, sum(frames), min(first_hw_us), max(last_hw_us), min(gap_min_us), max(gap_max_us),"
            " min(dlc_min), max(dlc_max) FROM id_seconds WHERE session = ? GROUP BY can_id, extended"
            " ORDER BY extended, can_id",
            (key,),
        ).fetchall()
    stats = []
    for can_id, extended, frames, first, last, gap_min, gap_max, dlc_min, dlc_max in rows:
        stats.append(
            IdStats(
                can_id=can_id,
                extended=bool(extended),
                frames=frames,
                first_s=(first - base) / 1e6,
                last_s=(last - base) / 1e6,
                rate_hz=frames / summary.seconds if summary.seconds > 0 else None,
                period_ms=(last - first) / (frames - 1) / 1000 if frames > 1 else None,
                gap_min_ms=None if gap_min is None else gap_min / 1000,
                gap_max_ms=None if gap_max is None else gap_max / 1000,
                dlc_min=dlc_min,
                dlc_max=dlc_max,
                changed_bits=changed[can_id, extended].to_bytes(8, "big"),
            )
        )
    return stats


def running_capture(root: DataRoot) -> LiveStatus | None:
    """The live status of the capture running on this data folder, if one is."""
    if not capture_running(root):
        return None
    return read_live(root)
