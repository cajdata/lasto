"""SQLite for lasto: connections and schema migrations (docs/architecture.md §4 and §14.3).

- Every database is in WAL mode, so readers never block the one writer, and transactions are
  explicit (autocommit otherwise).
- Writers choose synchronous FULL, so a commit survives power loss, or NORMAL for data that can be
  rebuilt.
- Readers (lasto log, and later the GUI) open read-only, with query_only on.
- Each kind of database carries its own application_id and a schema version. lasto refuses one of
  another kind, or one newer than the code, rather than guess. Each migration runs in one
  transaction, so a failure changes nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
SYNCHRONOUS = frozenset({"FULL", "NORMAL"})


class DatabaseError(Exception):
    """A database lasto can't use as it is."""


class WrongDatabase(DatabaseError):
    """Not the kind of lasto database expected, or not a lasto database at all."""


class DatabaseTooNew(DatabaseError):
    """Written by a newer lasto; this one won't guess at its schema."""


class DatabaseTooOld(DatabaseError):
    """Not set up or upgraded yet; only a writer migrates."""


@dataclass(frozen=True)
class Schema:
    name: str
    application_id: int
    migrations: tuple[str, ...]  # version n is migrations[n - 1]

    @property
    def version(self) -> int:
        return len(self.migrations)


def _no_other_files(action: int, *_details: object) -> int:
    """The authorizer on every connection lasto opens. ATTACH and VACUUM INTO (both SQLITE_ATTACH to SQLite) open a
    second file inside SQLite, where the safety core's guard can't see it, so both are refused."""
    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK


def connect(path: Path, *, synchronous: str) -> sqlite3.Connection:
    """The writer's connection: WAL, the chosen synchronous level, foreign keys on, explicit transactions, and no
    ATTACH or VACUUM INTO."""
    if synchronous not in SYNCHRONOUS:
        raise ValueError(f"synchronous must be FULL or NORMAL, not {synchronous!r}")
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.set_authorizer(_no_other_files)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA synchronous = {synchronous}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def connect_read_only(path: Path) -> sqlite3.Connection:
    """A reader's connection: the file must exist, and nothing can be written through it."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.set_authorizer(_no_other_files)
    conn.execute("PRAGMA query_only = ON")
    return conn


def schema_version(conn: sqlite3.Connection, schema: Schema) -> int:
    """The database's version of `schema`: 0 for an empty file. Refuses another kind, or a newer one."""
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    if application_id == 0:
        if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
            raise WrongDatabase(f"this isn't a lasto {schema.name} database")
        return 0
    if application_id != schema.application_id:
        raise WrongDatabase(f"this is another kind of database, not lasto's {schema.name} database")
    row = conn.execute("SELECT version FROM schema_version WHERE name = ?", (schema.name,)).fetchone()
    version = 0 if row is None else int(row[0])
    if version > schema.version:
        raise DatabaseTooNew(
            f"the {schema.name} database is at schema {version}; this lasto knows {schema.version}. Update lasto."
        )
    return version


def _statements(script: str) -> list[str]:
    """A migration script, one complete statement at a time (executescript would commit on its own).

    Split at each semicolon that completes a statement, so a semicolon inside a string doesn't split it.
    """
    statements, pending = [], ""
    for piece in script.split(";"):
        pending += piece + ";"
        if sqlite3.complete_statement(pending):
            if pending.strip(" \t\r\n;"):
                statements.append(pending.strip())
            pending = ""
    return statements


def migrate(conn: sqlite3.Connection, schema: Schema) -> int:
    """Bring the database to the schema's version, one transaction per version. Returns the version."""
    for version in range(schema_version(conn, schema) + 1, schema.version + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            if version == 1:
                conn.execute(f"PRAGMA application_id = {int(schema.application_id)}")
                conn.execute("CREATE TABLE schema_version (name TEXT PRIMARY KEY, version INTEGER NOT NULL)")
                conn.execute("INSERT INTO schema_version (name, version) VALUES (?, 0)", (schema.name,))
            for statement in _statements(schema.migrations[version - 1]):
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version = ? WHERE name = ?", (version, schema.name))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return schema.version


def opened_or_closed(conn: sqlite3.Connection, check: Callable[[sqlite3.Connection], object]) -> sqlite3.Connection:
    """Run a check on a new connection, and close it again if the check refuses the database."""
    try:
        check(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def require_current(conn: sqlite3.Connection, schema: Schema) -> None:
    """For a reader, which can't migrate: the database must be at exactly the schema's version."""
    version = schema_version(conn, schema)
    if version < schema.version:
        raise DatabaseTooOld(
            f"the {schema.name} database is at schema {version}, not {schema.version}; a capture upgrades it"
        )
