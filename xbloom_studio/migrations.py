"""Ordered SQLite schema migrations. Released definitions are immutable.

Schema versions never upgrade recipe JSON or reinterpret recorded plans. A
database is accepted only when its layout matches one this project could have
written for its version; anything else is refused without changes.
"""

# The DDL below is stored verbatim in databases; its layout is not reflowed.
# ruff: noqa: E501

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable

SCHEMA_VERSION = 2
# "OBLM". Databases created before the identifier was introduced carry zero.
APPLICATION_ID = 0x4F424C4D

BASE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS recipes(id TEXT PRIMARY KEY, name TEXT NOT NULL,
          head INTEGER NOT NULL, temporary INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);""",
    """CREATE TABLE IF NOT EXISTS revisions(recipe_id TEXT NOT NULL REFERENCES recipes(id),
          revision INTEGER NOT NULL, created_at TEXT NOT NULL, document TEXT NOT NULL,
          sha256 TEXT NOT NULL, PRIMARY KEY(recipe_id,revision));""",
    """CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY COLLATE NOCASE,
          recipe_id TEXT NOT NULL REFERENCES recipes(id));""",
    """CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, device TEXT NOT NULL, status TEXT NOT NULL,
          request TEXT NOT NULL, plan TEXT NOT NULL, result TEXT,
          idempotency_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL);""",
    """CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, operation_id TEXT NOT NULL
          REFERENCES operations(id), created_at TEXT NOT NULL, note TEXT NOT NULL, rating INTEGER);""",
    """CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
          at TEXT NOT NULL, monotonic_ns INTEGER NOT NULL, session_id TEXT, sequence INTEGER,
          operation_id TEXT REFERENCES operations(id), device TEXT, kind TEXT NOT NULL, data TEXT NOT NULL);""",
    """CREATE INDEX IF NOT EXISTS event_operation ON events(operation_id,id);""",
    """CREATE INDEX IF NOT EXISTS event_session ON events(session_id,sequence);""",
    """CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events
          BEGIN SELECT RAISE(ABORT,'events are append-only'); END;""",
    """CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events
          BEGIN SELECT RAISE(ABORT,'events are append-only'); END;""",
    """CREATE TRIGGER IF NOT EXISTS immutable_revisions_update BEFORE UPDATE ON revisions
          BEGIN SELECT RAISE(ABORT,'revisions are immutable'); END;""",
    """CREATE TRIGGER IF NOT EXISTS immutable_revisions_delete BEFORE DELETE ON revisions
          BEGIN SELECT RAISE(ABORT,'revisions are immutable'); END;""",
)  # fmt: skip

OPERATION_TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS immutable_operation_inputs BEFORE UPDATE OF "
    "request,plan,device,idempotency_key,request_hash,created_at,id ON operations\n"
    "          BEGIN SELECT RAISE(ABORT,'accepted operation inputs are immutable'); END;"
)

IMPORTS_TABLE = (
    "CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,"
    "source TEXT NOT NULL,document TEXT,error TEXT);"
)

Layout = dict[tuple[str, str], tuple[str, ...]]


def _initial(db: sqlite3.Connection) -> None:
    for statement in BASE_SCHEMA:
        db.execute(statement)


def _imports_and_frozen_inputs(db: sqlite3.Connection) -> None:
    db.execute(IMPORTS_TABLE)
    db.execute(OPERATION_TRIGGER)


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _initial,
    2: _imports_and_frozen_inputs,
}

# Version 1 stores could also add the version 2 objects one at a time, before
# the version number existed for them: each prefix is a valid version 1 layout.
VERSION_1_ADDITIONS = (OPERATION_TRIGGER, IMPORTS_TABLE)

_TOKEN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\w+|[^\s]")
_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)  # ASCII only, so quoted text and identifiers keep their exact bytes.


def _normalized(sql: str) -> tuple[str, ...]:
    """Tokenize, lowercase keywords and drop ``IF NOT EXISTS``; keep quoted text."""
    tokens = [
        token if token.startswith(("'", '"')) else token.translate(_LOWER)
        for token in _TOKEN.findall(sql)
    ]
    return tuple(
        token
        for index, token in enumerate(tokens)
        if not any(
            tokens[start : start + 3] == ["if", "not", "exists"]
            for start in range(max(0, index - 2), index + 1)
        )
    )


def _layout(db: sqlite3.Connection) -> Layout:
    return {
        (kind, name): _normalized(sql)
        for kind, name, sql in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
        )
    }


def _validate(db: sqlite3.Connection) -> int:
    version: int = db.execute("PRAGMA user_version").fetchone()[0]
    identity: int = db.execute("PRAGMA application_id").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"Database schema {version} is newer than supported {SCHEMA_VERSION}; "
            "upgrade Open Bloom Studio"
        )
    if version < 0 or identity not in (0, APPLICATION_ID):
        raise ValueError(
            "Database belongs to another application or has an invalid version"
        )
    actual = _layout(db)
    if version == 0:
        if actual:
            raise ValueError("Refusing nonempty unversioned database")
        return version
    reference = sqlite3.connect(":memory:")
    try:
        for target in range(1, version + 1):
            MIGRATIONS[target](reference)
        valid = [_layout(reference)]
        if version == 1:
            for statement in VERSION_1_ADDITIONS:
                reference.execute(statement)
                valid.append(_layout(reference))
        if actual not in valid:
            raise ValueError(
                f"Database schema {version} does not match a supported layout; "
                "restore a backup or inspect before upgrading"
            )
    finally:
        reference.close()
    return version


def migrate(db: sqlite3.Connection) -> None:
    """Validate, serialize and atomically upgrade; never recover operations here."""
    db.execute("BEGIN")
    try:
        _validate(db)  # All metadata must come from one consistent read snapshot.
    finally:
        db.rollback()
    db.execute("BEGIN IMMEDIATE")
    try:
        version = _validate(db)  # Another process may have migrated meanwhile.
        for target in range(version + 1, SCHEMA_VERSION + 1):
            MIGRATIONS[target](db)
            db.execute(f"PRAGMA user_version={target}")
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        _validate(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise
