"""The live feed: a capture's once-a-second status, for other processes to read (docs/architecture.md §14.3).

- The capture process writes `live.sqlite` in its own connection, with synchronous NORMAL: it's
  rewritten every second, so a power loss costs nothing worth keeping.
- A failure there never stops the capture. It's reported when it starts and when it clears, and the
  write is tried again each second.
- Readers (lasto log now; lasto view and the GUI later) open it read-only. The heartbeat's age, and
  the capture lock, tell them whether a capture is running.
- Phase 2 publishes the capture's status. Decoded values and alarms join it in Phase 4.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from dataclasses import astuple, dataclass
from pathlib import Path

from lasto.storage.database import (
    DatabaseError,
    Schema,
    connect,
    connect_read_only,
    migrate,
    opened_or_closed,
    require_current,
)
from lasto.storage.root import DataRoot

APPLICATION_ID = 0x4C41534C  # "LASL"

V1 = """
CREATE TABLE status (
    only INTEGER PRIMARY KEY CHECK (only = 1),
    pid INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT,
    state TEXT NOT NULL,
    interface TEXT NOT NULL,
    channel TEXT NOT NULL,
    started_utc TEXT NOT NULL,
    heartbeat_utc TEXT NOT NULL,
    frames INTEGER NOT NULL,
    error_frames INTEGER NOT NULL,
    frames_per_second INTEGER NOT NULL,
    bus TEXT
);
"""

SCHEMA = Schema("live", APPLICATION_ID, (V1,))


@dataclass(frozen=True, slots=True)
class LiveStatus:
    pid: int
    run_id: str
    session_id: str | None  # none while armed and waiting for traffic
    state: str  # "capturing", "waiting" (armed, the bus quiet), or "stopped"
    interface: str
    channel: str
    started_utc: str  # the run's start
    heartbeat_utc: str
    frames: int  # this session's, so far
    error_frames: int
    frames_per_second: int  # in the last second
    bus: str | None  # the last bus status the channel reported, if any


class LiveFeed:
    """The capture's writer of the live feed. Nothing it does raises."""

    def __init__(self, path: Path, *, on_change: Callable[[str | None], None]) -> None:
        """on_change is called with the error when writing starts failing, and with None when it works again."""
        self._path = path
        self._on_change = on_change
        self._conn: sqlite3.Connection | None = None
        self._failing = False
        self._closed = False

    def publish(self, status: LiveStatus) -> None:
        if self._closed:
            return
        try:
            if self._conn is None:
                self._conn = opened_or_closed(
                    connect(self._path, synchronous="NORMAL"), lambda conn: migrate(conn, SCHEMA)
                )
            self._conn.execute(f"INSERT OR REPLACE INTO status VALUES (1, {', '.join('?' * 12)})", astuple(status))
        except (sqlite3.Error, DatabaseError, OSError) as exc:
            self._drop()
            if not self._failing:
                self._failing = True
                self._on_change(f"{type(exc).__name__}: {exc}")
            return
        if self._failing:
            self._failing = False
            self._on_change(None)

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):  # closing a connection that failed can fail too
                conn.close()

    def close(self) -> None:
        self._closed = True
        self._drop()


def read_live(root: DataRoot) -> LiveStatus | None:
    """The live feed's status, or None if no capture has published one."""
    try:
        conn = opened_or_closed(connect_read_only(root.live_db), lambda conn: require_current(conn, SCHEMA))
    except FileNotFoundError:
        return None
    try:
        row = conn.execute(
            "SELECT pid, run_id, session_id, state, interface, channel, started_utc, heartbeat_utc, frames,"
            " error_frames, frames_per_second, bus FROM status"
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else LiveStatus(*row)
