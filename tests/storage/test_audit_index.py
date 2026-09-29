"""Indexing a run's audit log into the capture database as the log grows (lasto.storage.audit_index)."""

from __future__ import annotations

import json

import pytest

from lasto.storage import capture_db
from lasto.storage.audit_index import AuditIndex
from lasto.storage.root import DataRoot


def record(event: str, second: int = 0) -> bytes:
    fields = {"event": event, "utc": f"2026-09-26T18:00:{second:02d}+00:00", "mono": 1000.0 + second}
    return (json.dumps(fields, sort_keys=True) + "\n").encode("utf-8")


@pytest.fixture
def root(tmp_path) -> DataRoot:
    root = DataRoot(tmp_path)
    root.ensure()
    return root


@pytest.fixture
def conn(root, opened):
    return opened(capture_db.open_capture(root))


@pytest.fixture
def run(conn) -> str:
    return capture_db.create_run(
        conn,
        started_utc="2026-09-26T18:00:00+00:00",
        pid=1,
        mode="passive",
        interface="simulator",
        channel="PCAN_USBBUS1",
        lasto_version="0.0.1",
        safety_config="{}",
        audit_path="audit/run.jsonl",
    )


def indexed(conn) -> list[tuple[int, str, str]]:
    return conn.execute("SELECT line, utc, event FROM audit ORDER BY line").fetchall()


def test_complete_lines_are_indexed_as_the_log_grows(conn, root, run):
    path = root.audit_dir / "run.jsonl"
    path.write_bytes(record("session_opened") + record("status", 1)[:10])
    index = AuditIndex(conn, run, path)
    assert index.catch_up() == 1  # the second line is still being written
    with open(path, "ab") as file:
        file.write(record("status", 1)[10:] + record("session_closed", 2))
    assert index.catch_up() == 2
    assert index.catch_up() == 0
    assert indexed(conn) == [
        (1, "2026-09-26T18:00:00+00:00", "session_opened"),
        (2, "2026-09-26T18:00:01+00:00", "status"),
        (3, "2026-09-26T18:00:02+00:00", "session_closed"),
    ]
    [(text,)] = conn.execute("SELECT record FROM audit WHERE line = 2").fetchall()
    assert text.encode("utf-8") + b"\n" == record("status", 1)  # the whole line, as the file holds it


def test_a_new_index_picks_up_where_the_last_one_stopped(conn, root, run):
    path = root.audit_dir / "run.jsonl"
    path.write_bytes(record("a") + record("b", 1))
    AuditIndex(conn, run, path).catch_up()
    with open(path, "ab") as file:
        file.write(record("c", 2))
    assert AuditIndex(conn, run, path).catch_up() == 1
    assert [event for _, _, event in indexed(conn)] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "line",
    [
        b"not json",
        b"[1, 2]",
        b'{"event": "x"}',  # no time
        b'{"event": "x", "utc": "yesterday"}',
        b'{"event": "x", "utc": "2026-09-26T18:00:00"}',  # no time zone: not a time this log writes
        b'{"event": 5, "utc": "2026-09-26T18:00:00+00:00"}',
        b"\xff\xfe\x00\x00",  # what a power loss can leave in a file's last block
    ],
)
def test_a_line_that_is_not_an_audit_record_is_indexed_as_unreadable(conn, root, run, line):
    """The index still matches the file line for line, and the damage is there to see."""
    path = root.audit_dir / "run.jsonl"
    path.write_bytes(record("a") + line + b"\n" + record("b", 1))
    assert AuditIndex(conn, run, path).catch_up() == 3
    assert indexed(conn)[1] == (2, "", capture_db.UNREADABLE)
    assert [event for _, _, event in indexed(conn)][2] == "b"


def test_a_missing_log_has_nothing_to_index(conn, root, run):
    assert AuditIndex(conn, run, root.audit_dir / "run.jsonl").catch_up() == 0
    assert indexed(conn) == []
