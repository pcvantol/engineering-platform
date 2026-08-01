"""Regression coverage for the versioned Engineering SQLite schema."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.engineering.storage import (
    DATABASE_FILENAME,
    ENGINEERING_STORAGE_SCHEMA_VERSION,
    EngineeringStorageError,
    WORKSPACE_DIRECTORY,
    database_path,
    open_storage,
)


class EngineeringStorageTest(unittest.TestCase):
    def test_creates_private_versioned_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                version = connection.execute(
                    "SELECT MAX(version) FROM engineering_schema_migrations"
                ).fetchone()[0]
                self.assertEqual(version, ENGINEERING_STORAGE_SCHEMA_VERSION)
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='engineering_artifacts'"
                    ).fetchone()
                )
            path = root / WORKSPACE_DIRECTORY / DATABASE_FILENAME
            self.assertEqual(database_path(root), path.resolve())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.with_name(path.name + "-wal").exists())
            self.assertFalse(path.with_name(path.name + "-shm").exists())

    def test_reopening_is_idempotent_and_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO engineering_status(name,payload,updated_at) VALUES('canonical','{}','now')"
                )
                connection.execute(
                    "INSERT INTO engineering_artifacts(category,run_id,name,content,created_at) "
                    "VALUES('report','inbox-123','report.md',X'74657374','now')"
                )
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM engineering_schema_migrations").fetchone()[0],
                    ENGINEERING_STORAGE_SCHEMA_VERSION,
                )
                self.assertEqual(
                    connection.execute("SELECT content FROM engineering_artifacts").fetchone()[0], b"test"
                )

    def test_refuses_unknown_non_versioned_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = database_path(root)
            path.parent.mkdir()
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE unrelated(value TEXT)")
            with self.assertRaisesRegex(EngineeringStorageError, "no recognized schema history"):
                open_storage(root)

    def test_upgrades_the_pre_release_schema_without_losing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = database_path(root)
            path.parent.mkdir()
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE ep_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("CREATE TABLE ep_status(name TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
                connection.execute(
                    "CREATE TABLE ep_transactions(run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, phase TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE ep_artifacts(id INTEGER PRIMARY KEY, category TEXT NOT NULL, run_id TEXT, name TEXT NOT NULL, content BLOB NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE ep_component_logs(id INTEGER PRIMARY KEY, component TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO ep_status VALUES('canonical','{\"watcher_state\":\"WATCHER_IDLE\"}','now')"
                )
                connection.execute("INSERT INTO ep_transactions VALUES('inbox-123','{}','COMPLETE','now')")
                connection.execute(
                    "INSERT INTO ep_artifacts VALUES(1,'report','inbox-123','report.md',X'65766964656E6365','now')"
                )
                connection.execute(
                    "INSERT INTO ep_component_logs VALUES(1,'inbox','{\"event\":\"watcher_started\"}','now')"
                )
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute("SELECT payload FROM engineering_status WHERE name='canonical'").fetchone()[0],
                    '{"watcher_state":"WATCHER_IDLE"}',
                )
                self.assertEqual(
                    connection.execute("SELECT phase FROM engineering_transactions WHERE run_id='inbox-123'").fetchone()[0],
                    "COMPLETE",
                )
                self.assertEqual(connection.execute("SELECT content FROM engineering_artifacts").fetchone()[0], b"evidence")
                self.assertEqual(connection.execute("SELECT component FROM engineering_component_logs").fetchone()[0], "inbox")

    def test_refuses_newer_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO engineering_schema_migrations(version) VALUES(?)",
                    (ENGINEERING_STORAGE_SCHEMA_VERSION + 1,),
                )
            with self.assertRaisesRegex(EngineeringStorageError, "newer"):
                open_storage(root)
