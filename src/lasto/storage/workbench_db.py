"""The workbench database: what the GUI and the CLI's analysis write, never a capture.

Keeping it apart from the capture database means a capture and an edit never wait on one writer
(docs/architecture.md §14.3). It refers to sessions by UUID; SQLite can't enforce a key across two
files, so the services that join them check.
"""

from __future__ import annotations

import sqlite3

from lasto.storage.database import Schema, connect, migrate, opened_or_closed
from lasto.storage.ids import new_id
from lasto.storage.root import DataRoot

APPLICATION_ID = 0x4C415357  # "LASW"

V1 = """
CREATE TABLE vehicles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    platform TEXT NOT NULL,
    model_year INTEGER,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX one_default_vehicle ON vehicles (is_default) WHERE is_default = 1;

CREATE TABLE session_notes (
    session_id TEXT PRIMARY KEY,
    note TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);

CREATE TABLE session_tags (
    session_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (session_id, tag)
) WITHOUT ROWID;
"""

SCHEMA = Schema("workbench", APPLICATION_ID, (V1,))

# The vehicle lasto was built for, made the default the first time one is needed.
DEFAULT_VEHICLE = {"name": "GX470", "description": "2006 Lexus GX470", "platform": "toyota-j120", "model_year": 2006}


def open_workbench(root: DataRoot) -> sqlite3.Connection:
    return opened_or_closed(connect(root.workbench_db, synchronous="FULL"), lambda conn: migrate(conn, SCHEMA))


def default_vehicle(conn: sqlite3.Connection, *, created_utc: str) -> str:
    """The default vehicle's id, creating the 2006 GX470 the first time."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT id FROM vehicles WHERE is_default = 1").fetchone()
        if row is None:
            vehicle = new_id()
            conn.execute(
                "INSERT INTO vehicles (id, name, description, platform, model_year, is_default, created_utc)"
                " VALUES (?, ?, ?, ?, ?, 1, ?)",
                (vehicle, *DEFAULT_VEHICLE.values(), created_utc),
            )
        else:
            vehicle = row[0]
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return vehicle
