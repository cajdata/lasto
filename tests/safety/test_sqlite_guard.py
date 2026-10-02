"""Guard v2 and SQLite (Phase 3, A2): SQLite opens its files itself, raising no `open` event.

- sqlite3.connect raises its own event with the database it opens, a path or a file: URI. That path must pass
  the same write_problem() as any write, in any mode (a reader registers its data folder too).
- Loading an SQLite extension is refused.
- ATTACH and VACUUM INTO open another file with no event at all. The guard can't put an authorizer on a new
  connection: Python raises the connect/handle event before the connection is initialized. So lasto's own
  connections (lasto.storage.database) refuse both, and the scanner bans that SQL, and set_authorizer, in src/
  (test_database_setup.py, test_structure.py, test_structure_reach.py).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import events

import lasto
from lasto.safety import serial_guard
from lasto.safety.audit import REFUSALS
from lasto.safety.errors import SafetyViolation
from lasto.safety.serial_guard import sqlite_problem

REPO = Path(lasto.__file__).resolve().parents[2]
OUTSIDE = REPO / "lasto-sqlite-guard-probe.sqlite"  # not a folder a test run may write in


@pytest.fixture
def root(tmp_path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    return folder


@pytest.mark.parametrize(
    ("database", "allowed"),
    [
        (":memory:", True),
        ("file::memory:", True),
        ("file:scratch?mode=memory&cache=shared", True),
        ("{root}\\capture.sqlite", True),
        ("file:///{root_url}/capture.sqlite?mode=ro", True),
        ("file:{root_url}/live.sqlite", True),
        ("file://localhost/{root_url}/live.sqlite", True),
        ("file:///{root_url}/my%20data.sqlite", True),  # percent-encoded, as Path.as_uri() writes it
        ("{outside}", False),
        ("file:///{outside_url}?mode=ro", False),  # read-only, but still outside
        ("file://server/share/capture.sqlite", False),
        ("file:///{root_url}/COM5", False),
        ("", False),  # SQLite's own temporary file
    ],
)
def test_where_sqlite_may_open_a_database(root, database, allowed):
    text = database.format(
        root=root, root_url=root.as_posix(), outside=OUTSIDE, outside_url=OUTSIDE.as_posix()
    )
    assert (sqlite_problem(text, (str(root),)) is None) is allowed


def test_something_that_is_not_a_path_is_refused(root):
    assert sqlite_problem(object(), (str(root),)) == "not a path"


def test_a_database_in_a_test_folder_opens(tmp_path):
    conn = sqlite3.connect(tmp_path / "ok.sqlite")
    conn.execute("CREATE TABLE t (x)")
    conn.close()


def test_a_database_outside_is_refused_before_sqlite_creates_it(auditor, sink):
    REFUSALS.attach(auditor)
    with pytest.raises(SafetyViolation) as refused:
        sqlite3.connect(OUTSIDE)
    assert refused.value.reason == "write_outside_the_data_folder"
    assert not OUTSIDE.exists()
    [refusal] = events(sink, "rejected")
    assert refusal["request"].startswith("sqlite3.connect")


def test_an_ordinary_connection_still_reads_and_writes(tmp_path):
    conn = sqlite3.connect(tmp_path / "main.sqlite")
    try:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        conn.close()


def test_loading_an_extension_is_refused(tmp_path):
    conn = sqlite3.connect(tmp_path / "main.sqlite")
    try:
        with pytest.raises(SafetyViolation) as refused:
            conn.enable_load_extension(True)
        assert refused.value.reason == "sqlite_extension_refused"
        conn.enable_load_extension(False)  # turning it off is fine
    finally:
        conn.close()


def test_the_hook_decides_a_connect(auditor, sink, tmp_path):
    """Called directly too: coverage doesn't trace code while Python runs an audit hook."""
    REFUSALS.attach(auditor)
    serial_guard.guard_event("sqlite3.connect", (str(tmp_path / "ok.sqlite"),))  # a test folder
    serial_guard.guard_event("sqlite3.connect", (":memory:",))
    assert events(sink, "rejected") == []
    with pytest.raises(SafetyViolation) as refused:
        serial_guard.guard_event("sqlite3.connect", (str(OUTSIDE),))
    assert refused.value.reason == "write_outside_the_data_folder"
    [refusal] = events(sink, "rejected")
    assert refusal["request"] == f"sqlite3.connect {str(OUTSIDE)!r}"


def test_the_hook_refuses_loading_an_extension_by_path(auditor, sink):
    REFUSALS.attach(auditor)
    with pytest.raises(SafetyViolation):
        serial_guard.guard_event("sqlite3.load_extension", (None, "C:\\x\\ext.dll"))


PRODUCTION = r"""
import sqlite3, sys
import lasto.safety
from lasto.safety.errors import SafetyViolation
from lasto.safety.serial_guard import allow_writes_in

folder = sys.argv[1]
try:
    sqlite3.connect(folder + "\\capture.sqlite")
    print("connected")
except SafetyViolation as exc:
    print(exc.reason)
allow_writes_in(folder)
sqlite3.connect(folder + "\\capture.sqlite").close()
print("connected")
"""


def test_outside_a_test_run_sqlite_opens_only_in_the_registered_folder(tmp_path):
    done = subprocess.run([sys.executable, "-c", PRODUCTION, str(tmp_path)], capture_output=True, text=True, check=True)
    assert done.stdout.split() == ["write_outside_the_data_folder", "connected"]
