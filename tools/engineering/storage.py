"""Versioned SQLite schema contract for private Engineering Platform evidence.

This module owns only the database lifecycle. Consumers are migrated to it in a
separate, compatibility-tested change; an unrecognized database is never
silently replaced or downgraded.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sqlite3


WORKSPACE_DIRECTORY = ".engineering"
DATABASE_FILENAME = "engineering.db"
ENGINEERING_STORAGE_SCHEMA_VERSION = 1


class EngineeringStorageError(RuntimeError):
    """Raised when the local Engineering evidence database is unsafe to use."""


Migration = Callable[[sqlite3.Connection], None]


def _schema_v1(connection: sqlite3.Connection) -> None:
    """Create the initial normalized local-evidence schema."""
    for statement in """
        CREATE TABLE IF NOT EXISTS engineering_schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS engineering_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS engineering_status (
            name TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS engineering_transactions (
            run_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            phase TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS engineering_artifacts (
            id INTEGER PRIMARY KEY,
            category TEXT NOT NULL,
            run_id TEXT,
            name TEXT NOT NULL,
            content BLOB NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(category, run_id, name)
        );
        CREATE INDEX IF NOT EXISTS engineering_artifacts_lookup
            ON engineering_artifacts(category, run_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS engineering_component_logs (
            id INTEGER PRIMARY KEY,
            component TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS engineering_component_logs_lookup
            ON engineering_component_logs(component, id DESC);
        """.split(";"):
        if statement.strip():
            connection.execute(statement)
    legacy_tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    copies = (
        ("ep_status", "engineering_status", "name,payload,updated_at"),
        ("ep_transactions", "engineering_transactions", "run_id,payload,phase,updated_at"),
        ("ep_artifacts", "engineering_artifacts", "id,category,run_id,name,content,created_at"),
        ("ep_component_logs", "engineering_component_logs", "id,component,payload,created_at"),
    )
    for source, destination, columns in copies:
        if source in legacy_tables:
            connection.execute(f"INSERT OR IGNORE INTO {destination}({columns}) SELECT {columns} FROM {source}")


MIGRATIONS: dict[int, Migration] = {1: _schema_v1}


def database_path(root: Path) -> Path:
    """Return the only persistent EP evidence path for a repository."""
    return root.resolve() / WORKSPACE_DIRECTORY / DATABASE_FILENAME


def _schema_version(connection: sqlite3.Connection) -> int:
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "engineering_schema_migrations" not in tables:
        if "ep_metadata" in tables:
            return 0
        if tables:
            raise EngineeringStorageError("Engineering storage has no recognized schema history.")
        return 0
    versions = [int(row[0]) for row in connection.execute("SELECT version FROM engineering_schema_migrations")]
    return max(versions, default=0)


def open_storage(root: Path) -> sqlite3.Connection:
    """Open, upgrade and validate the private SQLite evidence database.

    Schema upgrades run in one immediate transaction. SQLite rollback-journal
    mode intentionally avoids persistent WAL sidecars in `.engineering`.
    """
    path = database_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=DELETE")
        current = _schema_version(connection)
        if current > ENGINEERING_STORAGE_SCHEMA_VERSION:
            raise EngineeringStorageError(
                "Engineering storage schema is newer than this Engineering Platform supports."
            )
        connection.execute("BEGIN IMMEDIATE")
        for version in range(current + 1, ENGINEERING_STORAGE_SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise EngineeringStorageError(f"Engineering storage migration {version} is unavailable.")
            migration(connection)
            connection.execute(
                "INSERT INTO engineering_schema_migrations(version) VALUES(?)", (version,)
            )
        connection.execute("COMMIT")
        path.chmod(0o600)
        return connection
    except (OSError, sqlite3.DatabaseError, EngineeringStorageError) as error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        connection.close()
        if isinstance(error, EngineeringStorageError):
            raise
        raise EngineeringStorageError("Engineering storage could not be opened safely.") from error
