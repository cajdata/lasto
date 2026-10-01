"""The workbench database: what the GUI and the CLI's analysis write (lasto.storage.workbench_db)."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from lasto.storage import workbench_db
from lasto.storage.root import DataRoot

UTC = "2026-09-28T18:00:00.000000+00:00"


@pytest.fixture
def conn(tmp_path, opened) -> sqlite3.Connection:
    return opened(workbench_db.open_workbench(DataRoot(tmp_path)))


def test_the_schema(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names >= {"vehicles", "session_notes", "session_tags"}
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_the_default_vehicle_is_the_gx470_made_once(conn):
    vehicle = workbench_db.default_vehicle(conn, created_utc=UTC)
    assert uuid.UUID(vehicle).version == 4
    assert workbench_db.default_vehicle(conn, created_utc=UTC) == vehicle
    rows = conn.execute("SELECT name, description, platform, model_year, is_default FROM vehicles").fetchall()
    assert rows == [("GX470", "2006 Lexus GX470", "toyota-j120", 2006, 1)]


def test_a_failed_default_leaves_no_transaction_open(conn):
    conn.execute(
        "INSERT INTO vehicles (id, name, description, platform, is_default, created_utc) VALUES (?, 'GX470', 'x', 'y', 0, ?)",
        (str(uuid.uuid4()), UTC),
    )
    with pytest.raises(sqlite3.IntegrityError):
        workbench_db.default_vehicle(conn, created_utc=UTC)
    assert not conn.in_transaction


def test_notes_and_tags_refer_to_sessions_by_uuid(conn):
    session = str(uuid.uuid4())
    conn.execute("INSERT INTO session_notes (session_id, note, updated_utc) VALUES (?, ?, ?)", (session, "grade", UTC))
    conn.execute("INSERT INTO session_tags (session_id, tag) VALUES (?, ?)", (session, "mountains"))
    assert conn.execute("SELECT tag FROM session_tags WHERE session_id = ?", (session,)).fetchall() == [("mountains",)]
