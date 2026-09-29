"""The capture database: runs, sessions, and what the recorder writes each second.

Only capture processes write it, with synchronous FULL, so a commit survives power loss. Everyone
else reads it read-only (docs/architecture.md §14.3).
- A run is one `lasto drive` process, holding one channel open.
- A session is one drive within a run. In armed mode a session ends after 60 s of bus silence, and a
  new one starts when traffic resumes.
- Runs, sessions, and vehicles have random UUIDs, and every path is relative to the data folder.

Schema version 1 stays open to change until the first live capture writes to it. After that, every
change is a migration.
"""

from __future__ import annotations

import sqlite3

from lasto.storage.database import Schema, connect, connect_read_only, migrate, opened_or_closed, require_current
from lasto.storage.ids import new_id
from lasto.storage.root import DataRoot, stored_path_problem

APPLICATION_ID = 0x4C415343  # "LASC"

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
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs (id),
    vehicle_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('passive', 'polled')),
    profile TEXT,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed', 'recovered')),
    started_utc TEXT NOT NULL,
    ended_utc TEXT,
    end_reason TEXT,
    folder TEXT NOT NULL,
    frames INTEGER NOT NULL DEFAULT 0,
    error_frames INTEGER NOT NULL DEFAULT 0,
    stored_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX sessions_by_start ON sessions (started_utc);

CREATE INDEX sessions_left_open ON sessions (state) WHERE state = 'open';

CREATE TABLE segments (
    session_id TEXT NOT NULL REFERENCES sessions (id),
    seq INTEGER NOT NULL,
    path TEXT NOT NULL,
    first_hw_us INTEGER,
    last_hw_us INTEGER,
    frames INTEGER NOT NULL DEFAULT 0,
    stored_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, seq)
) WITHOUT ROWID;

CREATE TABLE seconds (
    session_id TEXT NOT NULL REFERENCES sessions (id),
    second INTEGER NOT NULL,
    segment_seq INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    byte_length INTEGER NOT NULL,
    frames INTEGER NOT NULL,
    first_hw_us INTEGER NOT NULL,
    last_hw_us INTEGER NOT NULL,
    PRIMARY KEY (session_id, second)
) WITHOUT ROWID;

CREATE TABLE anchors (
    session_id TEXT NOT NULL REFERENCES sessions (id),
    hw_us INTEGER NOT NULL,
    host_utc TEXT NOT NULL,
    host_monotonic REAL NOT NULL,
    PRIMARY KEY (session_id, hw_us)
) WITHOUT ROWID;

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs (id),
    session_id TEXT REFERENCES sessions (id),
    host_utc TEXT NOT NULL,
    hw_us INTEGER,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX events_by_session ON events (session_id, id);

CREATE TABLE audit (
    run_id TEXT NOT NULL REFERENCES runs (id),
    line INTEGER NOT NULL,
    utc TEXT NOT NULL,
    event TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (run_id, line)
) WITHOUT ROWID;

CREATE TABLE id_stats (
    session_id TEXT NOT NULL REFERENCES sessions (id),
    can_id INTEGER NOT NULL,
    extended INTEGER NOT NULL,
    frames INTEGER NOT NULL,
    first_hw_us INTEGER NOT NULL,
    last_hw_us INTEGER NOT NULL,
    dlc_min INTEGER NOT NULL,
    dlc_max INTEGER NOT NULL,
    period_min_us INTEGER,
    period_max_us INTEGER,
    PRIMARY KEY (session_id, can_id, extended)
) WITHOUT ROWID;

CREATE TABLE id_seconds (
    session_id TEXT NOT NULL REFERENCES sessions (id),
    can_id INTEGER NOT NULL,
    extended INTEGER NOT NULL,
    second INTEGER NOT NULL,
    frames INTEGER NOT NULL,
    changed_bits BLOB NOT NULL,
    PRIMARY KEY (session_id, can_id, extended, second)
) WITHOUT ROWID;
"""
# Table notes:
# - seconds: where each second of traffic is (its zstd frame in a segment file). second counts whole
#   seconds from the session's first frame.
# - audit: an index of the run's JSON Lines audit log, which stays the record of truth (rule 11).
#   record is the whole line.
# - id_seconds: the rollup charts and the broadcast explorer start from (§14.8). changed_bits is 8
#   bytes, one bit set for each data bit that changed within the second.

SCHEMA = Schema("capture", APPLICATION_ID, (V1,))


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
    profile: str | None = None,
) -> str:
    session = new_id()
    conn.execute(
        "INSERT INTO sessions (id, run_id, vehicle_id, mode, profile, started_utc, folder) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session, run_id, vehicle_id, mode, profile, started_utc, _stored(folder)),
    )
    return session


def close_session(
    conn: sqlite3.Connection, session_id: str, *, ended_utc: str, end_reason: str, recovered: bool = False
) -> None:
    """Close an open session: closed after a clean end, recovered after a crash was trimmed."""
    cursor = conn.execute(
        "UPDATE sessions SET state = ?, ended_utc = ?, end_reason = ? WHERE id = ? AND state = 'open'",
        ("recovered" if recovered else "closed", ended_utc, end_reason, session_id),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"session {session_id} isn't open")


def sessions_left_open(conn: sqlite3.Connection) -> list[str]:
    """Sessions a crash or power loss left open, oldest first: the next capture recovers them."""
    return [row[0] for row in conn.execute("SELECT id FROM sessions WHERE state = 'open' ORDER BY started_utc, rowid")]
