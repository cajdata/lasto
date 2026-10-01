"""SQLite connections and schema migrations (lasto.storage.database)."""

from __future__ import annotations

import sqlite3

import pytest

from lasto.storage.database import (
    DatabaseTooNew,
    DatabaseTooOld,
    Schema,
    WrongDatabase,
    connect,
    connect_read_only,
    migrate,
    require_current,
    schema_version,
)

APP = 0x4C415354  # "LAST", for these tests only
SCHEMA = Schema("test", APP, ("CREATE TABLE a (x INTEGER);", "CREATE TABLE b (y TEXT);"))


@pytest.fixture
def writer(tmp_path, opened):
    return lambda name="t.sqlite", synchronous="FULL": opened(connect(tmp_path / name, synchronous=synchronous))


@pytest.fixture
def reader(tmp_path, opened):
    return lambda name="t.sqlite": opened(connect_read_only(tmp_path / name))


def tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_a_writer_connection(writer):
    conn = writer()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.isolation_level is None  # transactions are explicit
    assert writer("n.sqlite", "NORMAL").execute("PRAGMA synchronous").fetchone()[0] == 1


def test_synchronous_is_full_or_normal(tmp_path):
    with pytest.raises(ValueError):
        connect(tmp_path / "t.sqlite", synchronous="OFF")


def test_a_read_only_connection_reads_and_never_writes(writer, reader):
    conn = writer()
    migrate(conn, SCHEMA)
    conn.execute("INSERT INTO a VALUES (1)")
    ro = reader()
    assert ro.execute("SELECT x FROM a").fetchall() == [(1,)]
    assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO a VALUES (2)")


def test_a_read_only_connection_needs_the_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        connect_read_only(tmp_path / "missing.sqlite")


def test_migrate_applies_each_version_once(writer):
    conn = writer()
    assert migrate(conn, SCHEMA) == 2
    assert migrate(conn, SCHEMA) == 2
    assert schema_version(conn, SCHEMA) == 2
    assert conn.execute("PRAGMA application_id").fetchone()[0] == APP
    assert {"a", "b", "schema_version"} <= tables(conn)


def test_migrate_picks_up_where_it_left_off(writer):
    conn = writer()
    assert migrate(conn, Schema("test", APP, SCHEMA.migrations[:1])) == 1
    assert migrate(conn, SCHEMA) == 2


def test_a_failed_migration_changes_nothing(writer):
    conn = writer()
    migrate(conn, Schema("test", APP, SCHEMA.migrations[:1]))
    broken = Schema("test", APP, (SCHEMA.migrations[0], "CREATE TABLE c (z INTEGER); CREATE TABLE a (x INTEGER);"))
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, broken)
    assert schema_version(conn, SCHEMA) == 1
    assert "c" not in tables(conn)
    assert not conn.in_transaction


def test_a_database_newer_than_the_code_is_refused(writer):
    conn = writer()
    migrate(conn, SCHEMA)
    with pytest.raises(DatabaseTooNew):
        migrate(conn, Schema("test", APP, SCHEMA.migrations[:1]))
    with pytest.raises(DatabaseTooNew):
        schema_version(conn, Schema("test", APP, SCHEMA.migrations[:1]))


def test_another_kind_of_database_is_refused(writer):
    conn = writer()
    migrate(conn, SCHEMA)
    other = Schema("other", 0x4C41534F, SCHEMA.migrations)
    with pytest.raises(WrongDatabase):
        migrate(conn, other)
    with pytest.raises(WrongDatabase):
        schema_version(conn, other)


def test_a_file_that_is_not_empty_and_not_ours_is_refused(writer):
    conn = writer()
    conn.execute("CREATE TABLE someone_elses (x)")
    with pytest.raises(WrongDatabase):
        migrate(conn, SCHEMA)


def test_the_version_of_a_new_database_is_zero(writer):
    assert schema_version(writer(), SCHEMA) == 0


def test_a_reader_needs_the_current_schema(writer, reader):
    """A reader can't migrate: an empty file, or an older schema, is refused until a writer upgrades it."""
    conn = writer()
    with pytest.raises(DatabaseTooOld):
        require_current(reader(), SCHEMA)
    migrate(conn, Schema("test", APP, SCHEMA.migrations[:1]))
    with pytest.raises(DatabaseTooOld, match="schema 1, not 2"):
        require_current(reader(), SCHEMA)
    migrate(conn, SCHEMA)
    require_current(reader(), SCHEMA)


def test_semicolons_in_strings_stay_in_their_statement(writer):
    conn = writer()
    migrate(conn, Schema("test", APP, ("CREATE TABLE a (x TEXT DEFAULT 'a;b'); INSERT INTO a DEFAULT VALUES;",)))
    assert conn.execute("SELECT x FROM a").fetchall() == [("a;b",)]
