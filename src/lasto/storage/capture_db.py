"""The capture database: runs, sessions, and what the recorder writes each second.

Only capture processes write it, with synchronous FULL, so a commit survives power loss. Everyone
else reads it read-only (docs/architecture.md §14.3).
- A run is one `lasto drive` process, holding one channel open.
- A session is one drive within a run. In armed mode a session ends after 60 s of bus silence, and a
  new one starts when traffic resumes.
- Runs, sessions, and vehicles have random UUIDs. The tables written every second refer to a session
  by its integer key, so a UUID isn't repeated on every row.
- Every path is relative to the data folder.

Schema version 1 stays open to change until the first live capture writes to it. After that, every
change is a migration.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import astuple, dataclass

from lasto.storage.database import Schema, connect, connect_read_only, migrate, opened_or_closed, require_current
from lasto.storage.ids import new_id
from lasto.storage.root import DataRoot, stored_path_problem

APPLICATION_ID = 0x4C415343  # "LASC"
UNREADABLE = "unreadable"  # the event of an audit line that isn't an audit record (lasto.storage.audit_index)

V1 = """
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    started_utc TEXT NOT NULL,
    ended_utc TEXT,
    end_reason TEXT,
    pid INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('passive', 'polled')),
    interface TEXT NOT NULL CHECK (interface IN ('simulator', 'pcan')),
    channel TEXT NOT NULL,
    lasto_version TEXT NOT NULL,
    safety_config TEXT NOT NULL,
    audit_path TEXT NOT NULL,
    hardware TEXT,
    api_version TEXT,
    channel_version TEXT
);

CREATE TABLE sessions (
    key INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs (id),
    vehicle_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('passive', 'polled')),
    profile TEXT,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed', 'recovered')),
    started_utc TEXT NOT NULL,
    ended_utc TEXT,
    end_reason TEXT,
    folder TEXT NOT NULL,
    base_hw_us INTEGER NOT NULL,
    base_utc_us INTEGER NOT NULL,
    frames INTEGER NOT NULL DEFAULT 0,
    error_frames INTEGER NOT NULL DEFAULT 0,
    stored_bytes INTEGER NOT NULL DEFAULT 0,
    last_hw_us INTEGER
);

CREATE INDEX sessions_by_start ON sessions (started_utc);

CREATE INDEX sessions_left_open ON sessions (state) WHERE state = 'open';

CREATE TABLE segments (
    session INTEGER NOT NULL REFERENCES sessions (key),
    seq INTEGER NOT NULL,
    path TEXT NOT NULL,
    frames INTEGER NOT NULL DEFAULT 0,
    stored_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session, seq)
) WITHOUT ROWID;

CREATE TABLE seconds (
    session INTEGER NOT NULL REFERENCES sessions (key),
    second INTEGER NOT NULL,
    segment_seq INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    byte_length INTEGER NOT NULL,
    frames INTEGER NOT NULL,
    error_frames INTEGER NOT NULL,
    first_hw_us INTEGER NOT NULL,
    last_hw_us INTEGER NOT NULL,
    PRIMARY KEY (session, second)
) WITHOUT ROWID;

CREATE TABLE anchors (
    session INTEGER NOT NULL REFERENCES sessions (key),
    hw_us INTEGER NOT NULL,
    host_utc TEXT NOT NULL,
    host_monotonic REAL NOT NULL,
    PRIMARY KEY (session, hw_us)
) WITHOUT ROWID;

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs (id),
    session INTEGER REFERENCES sessions (key),
    host_utc TEXT NOT NULL,
    hw_us INTEGER,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX events_by_session ON events (session, id);

CREATE TABLE audit (
    run_id TEXT NOT NULL REFERENCES runs (id),
    line INTEGER NOT NULL,
    utc TEXT NOT NULL,
    event TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (run_id, line)
) WITHOUT ROWID;

CREATE TABLE id_seconds (
    session INTEGER NOT NULL REFERENCES sessions (key),
    can_id INTEGER NOT NULL,
    extended INTEGER NOT NULL,
    second INTEGER NOT NULL,
    frames INTEGER NOT NULL,
    first_hw_us INTEGER NOT NULL,
    last_hw_us INTEGER NOT NULL,
    gap_min_us INTEGER,
    gap_max_us INTEGER,
    dlc_min INTEGER NOT NULL,
    dlc_max INTEGER NOT NULL,
    changed_bits BLOB NOT NULL,
    last_data BLOB NOT NULL,
    PRIMARY KEY (session, can_id, extended, second)
) WITHOUT ROWID;
"""
# Table notes:
# - sessions: base_hw_us and base_utc_us are the session's time base (its first frame's hardware
#   timestamp and the UTC it arrived at), which its segments' candump timestamps come from.
# - seconds: where each second of traffic is (its two zstd frames in a segment file). second counts
#   whole seconds from the session's first frame, and only moves forward: the recorder puts a frame
#   stamped back into a second already written into the next one.
# - audit: an index of the run's JSON Lines audit log, which stays the record of truth (rule 11).
#   record is the whole line.
# - id_seconds: each ID's rollup for one second, which per-ID stats, charts, and the broadcast
#   explorer start from (§14.8). changed_bits is 8 bytes, a bit set for each data bit that changed
#   since the ID's previous frame. The gaps are also measured from the previous frame, even one in an
#   earlier second.

SCHEMA = Schema("capture", APPLICATION_ID, (V1,))


@dataclass(frozen=True, slots=True)
class OpenedSession:
    key: int
    id: str


@dataclass(frozen=True, slots=True)
class SecondRow:
    """Where one second of traffic went, and how much there was."""

    session: int
    second: int
    segment_seq: int
    byte_offset: int
    byte_length: int
    frames: int
    error_frames: int
    first_hw_us: int
    last_hw_us: int


@dataclass(frozen=True, slots=True)
class IdSecond:
    """One ID's rollup for one second (see id_seconds)."""

    can_id: int
    extended: bool
    frames: int
    first_hw_us: int
    last_hw_us: int
    gap_min_us: int | None
    gap_max_us: int | None
    dlc_min: int
    dlc_max: int
    changed_bits: bytes
    last_data: bytes


@dataclass(frozen=True, slots=True)
class Anchor:
    """A hardware timestamp and the host's UTC and monotonic time at the same moment."""

    hw_us: int
    host_utc: str
    host_monotonic: float


@dataclass(frozen=True, slots=True)
class LeftOpen:
    """A session a crash or power loss left open, with what recovery needs to close it."""

    key: int
    id: str
    run_id: str
    started_utc: str
    base_hw_us: int
    base_utc_us: int
    last_hw_us: int | None  # its last indexed frame, if any
    last_second: int | None  # its last indexed second, if any


@dataclass(frozen=True, slots=True)
class SegmentRow:
    seq: int
    path: str
    stored_bytes: int  # how much of the file the seconds table indexes, from its start


@dataclass(frozen=True, slots=True)
class LeftUnended:
    """A run whose process stopped without ending it."""

    id: str
    started_utc: str
    audit_path: str


@dataclass(frozen=True, slots=True)
class AuditLine:
    """One line of a run's audit log, as the audit table indexes it."""

    run_id: str
    line: int  # counted from 1
    utc: str
    event: str
    record: str


def open_capture(root: DataRoot) -> sqlite3.Connection:
    """The capture process's writer connection, migrated to the current schema."""
    return opened_or_closed(connect(root.capture_db, synchronous="FULL"), lambda conn: migrate(conn, SCHEMA))


def read_capture(root: DataRoot) -> sqlite3.Connection:
    """A read-only connection for everyone but the capture process."""
    return opened_or_closed(connect_read_only(root.capture_db), lambda conn: require_current(conn, SCHEMA))


def _stored(path: str) -> str:
    problem = stored_path_problem(path)
    if problem is not None:
        raise ValueError(f"path {path!r} {problem}")
    return path


def _transaction(conn: sqlite3.Connection, statements: Sequence[tuple[str, Sequence[object]]]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for sql, parameters in statements:
            conn.execute(sql, parameters)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def create_run(
    conn: sqlite3.Connection,
    *,
    started_utc: str,
    pid: int,
    mode: str,
    interface: str,
    channel: str,
    lasto_version: str,
    safety_config: str,
    audit_path: str,
) -> str:
    run = new_id()
    conn.execute(
        "INSERT INTO runs (id, started_utc, pid, mode, interface, channel, lasto_version, safety_config, audit_path)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run, started_utc, pid, mode, interface, channel, lasto_version, safety_config, _stored(audit_path)),
    )
    return run


def describe_run(conn: sqlite3.Connection, run_id: str, *, hardware: str, api_version: str, channel_version: str) -> None:
    """What the channel reported when it opened: the adapter, the PCAN-Basic version, and the channel's firmware."""
    conn.execute(
        "UPDATE runs SET hardware = ?, api_version = ?, channel_version = ? WHERE id = ?",
        (hardware, api_version, channel_version, run_id),
    )


def end_run(conn: sqlite3.Connection, run_id: str, *, ended_utc: str, end_reason: str) -> None:
    cursor = conn.execute(
        "UPDATE runs SET ended_utc = ?, end_reason = ? WHERE id = ? AND ended_utc IS NULL", (ended_utc, end_reason, run_id)
    )
    if cursor.rowcount != 1:
        raise ValueError(f"run {run_id} has already ended, or doesn't exist")


def open_session(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    vehicle_id: str,
    mode: str,
    started_utc: str,
    folder: str,
    base_hw_us: int,
    base_utc_us: int,
    host_monotonic: float,
    profile: str | None = None,
) -> OpenedSession:
    """A new session, with its time base recorded as its first anchor."""
    session = new_id()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "INSERT INTO sessions (id, run_id, vehicle_id, mode, profile, started_utc, folder, base_hw_us, base_utc_us)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session, run_id, vehicle_id, mode, profile, started_utc, _stored(folder), base_hw_us, base_utc_us),
        )
        key = int(cursor.lastrowid)  # type: ignore[arg-type]
        conn.execute(
            "INSERT INTO anchors (session, hw_us, host_utc, host_monotonic) VALUES (?, ?, ?, ?)",
            (key, base_hw_us, started_utc, host_monotonic),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return OpenedSession(key, session)


def set_session_folder(conn: sqlite3.Connection, session_key: int, folder: str) -> None:
    """The session's own folder, named once its UUID is known."""
    conn.execute("UPDATE sessions SET folder = ? WHERE key = ?", (_stored(folder), session_key))


def open_segment(conn: sqlite3.Connection, *, session_key: int, seq: int, path: str) -> None:
    conn.execute("INSERT INTO segments (session, seq, path) VALUES (?, ?, ?)", (session_key, seq, _stored(path)))


def _id_row(row: SecondRow, one: IdSecond) -> tuple[object, ...]:
    """An id_seconds row, in its columns' order: session, ID, extended, second, then the rollup."""
    can_id, extended, *rollup = astuple(one)
    return (row.session, can_id, extended, row.second, *rollup)


def record_second(
    conn: sqlite3.Connection, row: SecondRow, ids: Sequence[IdSecond], anchor: Anchor | None
) -> None:
    """One second of traffic, in one transaction: its place, its rollups, the running totals, and maybe an anchor.

    Called only after the second is written and fsynced to its segment, so the database never points at
    data that isn't on disk.
    """
    statements: list[tuple[str, Sequence[object]]] = [
        ("INSERT INTO seconds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", astuple(row)),
        *(("INSERT INTO id_seconds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _id_row(row, one)) for one in ids),
        (
            "UPDATE segments SET frames = frames + ?, stored_bytes = stored_bytes + ? WHERE session = ? AND seq = ?",
            (row.frames, row.byte_length, row.session, row.segment_seq),
        ),
        (
            "UPDATE sessions SET frames = frames + ?, error_frames = error_frames + ?, stored_bytes = stored_bytes + ?,"
            " last_hw_us = ? WHERE key = ?",
            (row.frames, row.error_frames, row.byte_length, row.last_hw_us, row.session),
        ),
    ]
    if anchor is not None:
        statements.append(
            ("INSERT OR IGNORE INTO anchors VALUES (?, ?, ?, ?)", (row.session, anchor.hw_us, anchor.host_utc, anchor.host_monotonic))
        )
    _transaction(conn, statements)


def add_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    session_key: int | None,
    host_utc: str,
    hw_us: int | None,
    kind: str,
    detail: dict[str, object],
) -> None:
    conn.execute(
        "INSERT INTO events (run_id, session, host_utc, hw_us, kind, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, session_key, host_utc, hw_us, kind, json.dumps(detail, sort_keys=True)),
    )


def close_session(conn: sqlite3.Connection, session_id: str, *, ended_utc: str, end_reason: str) -> None:
    """Close a session that ended cleanly."""
    cursor = conn.execute(
        "UPDATE sessions SET state = 'closed', ended_utc = ?, end_reason = ? WHERE id = ? AND state = 'open'",
        (ended_utc, end_reason, session_id),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"session {session_id} isn't open")


def sessions_left_open(conn: sqlite3.Connection) -> list[LeftOpen]:
    """Sessions a crash or power loss left open, oldest first: the next capture recovers them."""
    rows = conn.execute(
        "SELECT key, id, run_id, started_utc, base_hw_us, base_utc_us, last_hw_us,"
        " (SELECT max(second) FROM seconds WHERE seconds.session = sessions.key)"
        " FROM sessions WHERE state = 'open' ORDER BY started_utc, key"
    )
    return [LeftOpen(*row) for row in rows]


def segments_of(conn: sqlite3.Connection, session_key: int) -> list[SegmentRow]:
    rows = conn.execute("SELECT seq, path, stored_bytes FROM segments WHERE session = ? ORDER BY seq", (session_key,))
    return [SegmentRow(*row) for row in rows]


def last_frames(conn: sqlite3.Connection, session_key: int) -> dict[tuple[int, bool], tuple[int, bytes]]:
    """Each ID's last indexed frame in a session, (hardware timestamp, data), keyed by (ID, extended)."""
    # With max(), SQLite takes the other bare columns from the row holding the maximum: the ID's latest rollup.
    rows = conn.execute(
        "SELECT can_id, extended, max(second), last_hw_us, last_data FROM id_seconds WHERE session = ?"
        " GROUP BY can_id, extended",
        (session_key,),
    )
    return {(can_id, bool(extended)): (hw_us, data) for can_id, extended, _, hw_us, data in rows}


def close_recovered(
    conn: sqlite3.Connection, session: LeftOpen, *, ended_utc: str, host_utc: str, detail: dict[str, object]
) -> None:
    """Close a session recovery has trimmed, with an event saying what it found, in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "UPDATE sessions SET state = 'recovered', ended_utc = ?, end_reason = 'interrupted'"
            " WHERE key = ? AND state = 'open'",
            (ended_utc, session.key),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"session {session.id} isn't open")
        conn.execute(
            "INSERT INTO events (run_id, session, host_utc, kind, detail) VALUES (?, ?, ?, 'recovered', ?)",
            (session.run_id, session.key, host_utc, json.dumps(detail, sort_keys=True)),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def runs_left_unended(conn: sqlite3.Connection) -> list[LeftUnended]:
    """Runs whose process stopped without ending them, oldest first."""
    rows = conn.execute("SELECT id, started_utc, audit_path FROM runs WHERE ended_utc IS NULL ORDER BY started_utc, id")
    return [LeftUnended(*row) for row in rows]


def run_times(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """Every UTC time a run is known to have reached: its start, its sessions' ends, and its last audit record."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT started_utc FROM runs WHERE id = ?1"
            " UNION ALL SELECT ended_utc FROM sessions WHERE run_id = ?1 AND ended_utc IS NOT NULL"
            " UNION ALL SELECT utc FROM (SELECT utc FROM audit WHERE run_id = ?1 AND event != ?2 ORDER BY line DESC LIMIT 1)",
            (run_id, UNREADABLE),
        )
    ]


def audit_lines_indexed(conn: sqlite3.Connection, run_id: str) -> int:
    """How many lines of a run's audit log the audit table holds, from the start of the file."""
    return int(conn.execute("SELECT coalesce(max(line), 0) FROM audit WHERE run_id = ?", (run_id,)).fetchone()[0])


def add_audit_lines(conn: sqlite3.Connection, lines: Sequence[AuditLine]) -> None:
    if lines:
        _transaction(conn, [("INSERT INTO audit VALUES (?, ?, ?, ?, ?)", astuple(line)) for line in lines])
