"""Regression coverage for the suite-wide installed-authority isolation hook."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering import central_store_migration as migration
from tools.engineering import storage
from tests.engineering.harness_isolation import activate


activate()


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema_version(path: Path) -> int:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return int(connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0])


class EngineeringHarnessAuthorityIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.installation_root = Path(os.environ["DJCONNECT_EP_TEST_INSTALLATION_ROOT"]).resolve()
        self.assertTrue(self.installation_root.is_dir())
        self.external = tempfile.TemporaryDirectory()
        self.external_root = Path(self.external.name)
        self.external_store = self.external_root / "production-central.db"
        self.external_pointer = (
            self.external_root / "Library" / "Application Support" / "Engineering Platform" / "runtime" / "store-authority.json"
        )
        self._create_external_central(schema=storage.ENGINEERING_STORAGE_SCHEMA_VERSION)

    def tearDown(self) -> None:
        self.external.cleanup()

    def _create_external_central(self, *, schema: int) -> None:
        fixture_root = self.external_root / "fixture-repository"
        with storage.open_storage(fixture_root) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS external_canary (value TEXT)")
            connection.execute("INSERT INTO external_canary VALUES ('production')")
        source = fixture_root / storage.WORKSPACE_DIRECTORY / storage.DATABASE_FILENAME
        self.external_store.write_bytes(source.read_bytes())
        if schema != storage.ENGINEERING_STORAGE_SCHEMA_VERSION:
            with sqlite3.connect(self.external_store) as connection:
                connection.execute("DELETE FROM engineering_schema_migrations")
                connection.execute(
                    "INSERT INTO engineering_schema_migrations(version) VALUES(?)", (schema,)
                )
        self.external_pointer.parent.mkdir(parents=True, exist_ok=True)
        self.external_pointer.write_text(
            json.dumps(
                {
                    "version": 1,
                    "authoritative_path": str(self.external_store),
                    "schema": schema,
                    "fingerprint_sha256": _fingerprint(self.external_store),
                }
            ),
            encoding="utf-8",
        )

    def test_external_installation_matrix_is_ignored_by_ordinary_storage(self) -> None:
        for external_state in ("ABSENT", "LEGACY", "CENTRAL_SCHEMA_40", "CENTRAL_SCHEMA_41"):
            with self.subTest(external_state=external_state), tempfile.TemporaryDirectory() as temporary:
                if external_state == "ABSENT":
                    self.external_pointer.unlink(missing_ok=True)
                    self.external_store.unlink(missing_ok=True)
                elif external_state == "LEGACY":
                    self._create_external_central(schema=storage.ENGINEERING_STORAGE_SCHEMA_VERSION)
                    self.external_pointer.unlink(missing_ok=True)
                elif external_state == "CENTRAL_SCHEMA_41":
                    self._create_external_central(schema=41)
                else:
                    self._create_external_central(schema=storage.ENGINEERING_STORAGE_SCHEMA_VERSION)
                before = _fingerprint(self.external_store) if self.external_store.exists() else None
                repository = Path(temporary)
                with storage.open_storage(repository) as connection:
                    connection.execute("CREATE TABLE harness_canary (value TEXT)")
                    connection.execute("INSERT INTO harness_canary VALUES (?)", (external_state,))
                isolated = storage.database_path(repository)
                self.assertNotEqual(isolated, self.external_store)
                self.assertTrue(isolated.is_relative_to(repository.resolve()))
                after = _fingerprint(self.external_store) if self.external_store.exists() else None
                self.assertEqual(after, before)

    def test_schema_activation_never_observes_external_central_authority(self) -> None:
        before = _fingerprint(self.external_store)
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            with storage.open_storage(repository):
                pass
            migrations = dict(storage.MIGRATIONS)
            migrations[storage.ENGINEERING_STORAGE_SCHEMA_VERSION + 1] = lambda _: None
            with (
                patch.object(storage, "ENGINEERING_STORAGE_SCHEMA_VERSION", storage.ENGINEERING_STORAGE_SCHEMA_VERSION + 1),
                patch.object(storage, "MIGRATIONS", migrations),
            ):
                with storage.activate_storage_schema(repository):
                    pass
            self.assertEqual(_schema_version(storage.database_path(repository)), 41)
        self.assertEqual(_fingerprint(self.external_store), before)

    def test_dashboard_fixture_rows_stay_out_of_external_central_store(self) -> None:
        before = _fingerprint(self.external_store)
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            with storage.open_storage(repository) as connection:
                connection.execute("CREATE TABLE backup_probe (value TEXT)")
                connection.execute("INSERT INTO backup_probe VALUES ('isolated')")
            with sqlite3.connect(storage.database_path(repository)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM backup_probe").fetchone(), ("isolated",))
        self.assertEqual(_fingerprint(self.external_store), before)
        with sqlite3.connect(f"file:{self.external_store}?mode=ro", uri=True) as connection:
            self.assertNotIn("backup_probe", {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")})

    def test_migration_control_paths_resolve_under_the_isolated_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            with storage.open_storage(repository):
                pass
            freeze = migration.set_admission_freeze(repository, migration_id="test-migration", reason="test")
            self.assertEqual(freeze["state"], "ACTIVE")
            self.assertTrue(migration.authority_pointer_path().resolve().is_relative_to(self.installation_root))
            self.assertTrue(migration.receipt_path("test-migration").resolve().is_relative_to(self.installation_root))

    def test_external_authority_pointer_is_rejected_before_writable_storage_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            storage, "_authority_pointer_path", return_value=self.external_pointer
        ):
            with self.assertRaisesRegex(storage.EngineeringStorageError, "test harness rejected"):
                storage.open_storage(Path(temporary))
