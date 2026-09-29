"""The capture database: runs and sessions, written only by capture processes (lasto.storage.capture_db)."""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from lasto.storage import capture_db, workbench_db
from lasto.storage.database import DatabaseTooNew, WrongDatabase, connect, migrate
from lasto.storage.root import DataRoot

UTC = "2026-09-28T18:00:00.000000+00:00"
LATER = "2026-09-28T18:20:00.000000+00:00"
CONFIG = json.dumps({"version": 1})


@pytest.fixture
def root(tmp_path) -> DataRoot:
    root = DataRoot(tmp_path)
    root.ensure()
    return root


@pytest.fixture
def conn(root, opened) -> sqlite3.Connection:
    return opened(capture_db.open_capture(root))


def new_run(conn: sqlite3.Connection, **changes: object) -> str:
    fields = {
        "started_utc": UTC,
        "pid": 4242,
        "mode": "passive",
        "interface": "simulator",
        "channel": "PCAN_USBBUS1",
        "lasto_version": "0.0.1",
        "safety_config": CONFIG,
        "audit_path": "audit/run.jsonl",
    }
    return capture_db.create_run(conn, **{**fields, **changes})


def new_session(conn: sqlite3.Connection, run: str, folder: str = "sessions/x") -> str:
    return capture_db.open_session(conn, run_id=run, vehicle_id="v", mode="passive", started_utc=UTC, folder=folder)


def test_the_schema(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names >= {
        "runs", "sessions", "segments", "seconds", "anchors", "events", "audit", "id_stats", "id_seconds",
    }  # fmt: skip
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL: survive power loss
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_run_and_a_session_get_uuids(conn):
    run = new_run(conn)
    session = capture_db.open_session(
        conn, run_id=run, vehicle_id=str(uuid.uuid4()), mode="passive", started_utc=UTC, folder="sessions/x"
    )
    assert uuid.UUID(run).version == 4 and uuid.UUID(session).version == 4 and run != session
    row = conn.execute("SELECT run_id, mode, state, folder, profile FROM sessions WHERE id = ?", (session,)).fetchone()
    assert row == (run, "passive", "open", "sessions/x", None)
    assert json.loads(conn.execute("SELECT safety_config FROM runs").fetchone()[0]) == {"version": 1}


def test_closing_a_session(conn):
    run = new_run(conn)
    first, second = new_session(conn, run, "sessions/a"), new_session(conn, run, "sessions/b")
    assert capture_db.sessions_left_open(conn) == [first, second]
    capture_db.close_session(conn, first, ended_utc=LATER, end_reason="bus_silent")
    capture_db.close_session(conn, second, ended_utc=LATER, end_reason="crashed", recovered=True)
    assert capture_db.sessions_left_open(conn) == []
    rows = dict(conn.execute("SELECT id, state || ' ' || end_reason FROM sessions").fetchall())
    assert rows == {first: "closed bus_silent", second: "recovered crashed"}
    with pytest.raises(ValueError, match="isn't open"):
        capture_db.close_session(conn, first, ended_utc=LATER, end_reason="again")


def test_ending_a_run(conn):
    run = new_run(conn)
    capture_db.end_run(conn, run, ended_utc=LATER, end_reason="stopped")
    assert conn.execute("SELECT ended_utc, end_reason FROM runs").fetchone() == (LATER, "stopped")
    with pytest.raises(ValueError, match="already ended"):
        capture_db.end_run(conn, run, ended_utc=LATER, end_reason="again")


@pytest.mark.parametrize(("field", "value"), [("mode", "loud"), ("interface", "wifi")])
def test_runs_hold_only_known_modes_and_interfaces(conn, field, value):
    with pytest.raises(sqlite3.IntegrityError):
        new_run(conn, **{field: value})


def test_a_session_belongs_to_a_run(conn):
    with pytest.raises(sqlite3.IntegrityError):
        new_session(conn, "no-such-run")


@pytest.mark.parametrize("stored", ["../audit.jsonl", r"C:\audit.jsonl", "/audit.jsonl"])
def test_paths_in_the_database_stay_under_the_data_root(conn, stored):
    with pytest.raises(ValueError):
        new_run(conn, audit_path=stored)
    with pytest.raises(ValueError):
        new_session(conn, new_run(conn), folder=stored)


def test_a_reader_opens_it_read_only(root, conn, opened):
    new_run(conn)
    reader = opened(capture_db.read_capture(root))
    assert reader.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("DELETE FROM runs")


def test_a_reader_needs_a_capture_database(root):
    with pytest.raises(FileNotFoundError):
        capture_db.read_capture(root)


def test_a_reader_refuses_a_database_it_does_not_know(root, opened):
    conn = opened(connect(root.capture_db, synchronous="FULL"))
    migrate(conn, capture_db.SCHEMA)
    conn.execute("UPDATE schema_version SET version = version + 1")
    with pytest.raises(DatabaseTooNew):
        capture_db.read_capture(root)


def test_the_capture_database_is_not_a_workbench(root):
    workbench_db.open_workbench(root).close()
    root.workbench_db.rename(root.capture_db)
    with pytest.raises(WrongDatabase):
        capture_db.open_capture(root)
