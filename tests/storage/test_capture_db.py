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


def new_session(conn: sqlite3.Connection, run: str, folder: str = "sessions/x") -> capture_db.OpenedSession:
    return capture_db.open_session(
        conn,
        run_id=run,
        vehicle_id="v",
        mode="passive",
        started_utc=UTC,
        folder=folder,
        base_hw_us=1_000_000_000,
        base_utc_us=1_759_168_800_000_000,
        host_monotonic=1000.0,
    )


def test_the_schema(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names >= {"runs", "sessions", "segments", "seconds", "anchors", "events", "audit", "id_seconds"}
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL: survive power loss
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_run_and_a_session_get_uuids(conn):
    run = new_run(conn)
    session = new_session(conn, run)
    assert uuid.UUID(run).version == 4 and uuid.UUID(session.id).version == 4 and run != session.id
    row = conn.execute(
        "SELECT key, run_id, mode, state, folder, profile, base_hw_us FROM sessions WHERE id = ?", (session.id,)
    ).fetchone()
    assert row == (session.key, run, "passive", "open", "sessions/x", None, 1_000_000_000)
    assert json.loads(conn.execute("SELECT safety_config FROM runs").fetchone()[0]) == {"version": 1}
    # The session's time base is its first anchor.
    assert conn.execute("SELECT session, hw_us, host_monotonic FROM anchors").fetchall() == [
        (session.key, 1_000_000_000, 1000.0)
    ]


def test_closing_a_session(conn):
    run = new_run(conn)
    first, second = new_session(conn, run, "sessions/a"), new_session(conn, run, "sessions/b")
    assert [left.id for left in capture_db.sessions_left_open(conn)] == [first.id, second.id]
    capture_db.close_session(conn, first.id, ended_utc=LATER, end_reason="bus_silent")
    [left] = capture_db.sessions_left_open(conn)
    assert left == capture_db.LeftOpen(second.key, second.id, run, UTC, 1_000_000_000, 1_759_168_800_000_000, None, None)
    capture_db.close_recovered(conn, left, ended_utc=LATER, host_utc=LATER, detail={"seconds_indexed": 0})
    assert capture_db.sessions_left_open(conn) == []
    rows = dict(conn.execute("SELECT id, state || ' ' || end_reason FROM sessions").fetchall())
    assert rows == {first.id: "closed bus_silent", second.id: "recovered interrupted"}
    assert conn.execute("SELECT session, kind, detail FROM events").fetchall() == [
        (second.key, "recovered", '{"seconds_indexed": 0}')
    ]
    with pytest.raises(ValueError, match="isn't open"):
        capture_db.close_session(conn, first.id, ended_utc=LATER, end_reason="again")
    with pytest.raises(ValueError, match="isn't open"):
        capture_db.close_recovered(conn, left, ended_utc=LATER, host_utc=LATER, detail={})
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1  # the refused close left no event
    assert not conn.in_transaction


def test_a_run_records_what_the_channel_reported(conn):
    run = new_run(conn)
    capture_db.describe_run(conn, run, hardware="PCAN-USB", api_version="5.1.0.1194", channel_version="8.4.0")
    assert conn.execute("SELECT hardware, api_version, channel_version FROM runs").fetchone() == (
        "PCAN-USB",
        "5.1.0.1194",
        "8.4.0",
    )


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
    run = new_run(conn)
    with pytest.raises(ValueError):
        capture_db.open_segment(conn, session_key=new_session(conn, run).key, seq=1, path=stored)


def test_events_belong_to_a_run_and_maybe_a_session(conn):
    run = new_run(conn)
    session = new_session(conn, run)
    capture_db.add_event(conn, run_id=run, session_key=None, host_utc=UTC, hw_us=None, kind="bus_state", detail={"x": 1})
    capture_db.add_event(conn, run_id=run, session_key=session.key, host_utc=UTC, hw_us=5, kind="read_error", detail={})
    rows = conn.execute("SELECT session, hw_us, kind, detail FROM events ORDER BY id").fetchall()
    assert rows == [(None, None, "bus_state", '{"x": 1}'), (session.key, 5, "read_error", "{}")]


def test_a_second_that_fails_to_record_leaves_nothing_behind(conn):
    run = new_run(conn)
    session = new_session(conn, run)
    capture_db.open_segment(conn, session_key=session.key, seq=1, path="sessions/x/seg-0001.candump.zst")
    row = capture_db.SecondRow(session.key, 0, 1, 0, 100, 3, 0, 10, 20)
    capture_db.record_second(conn, row, [], None)
    with pytest.raises(sqlite3.IntegrityError):
        capture_db.record_second(conn, row, [], None)  # the same second twice
    assert not conn.in_transaction
    assert conn.execute("SELECT frames FROM sessions").fetchone()[0] == 3


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
