from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from engineering_platform import server
from engineering_platform.ep_consumer_credentials import verifier
from engineering_platform import storage


class ConsumerCredentialNamespaceMigrationTests(unittest.TestCase):
    def _schema_52_with_legacy_authority(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys=OFF")
        identity = server.RuntimeIdentity("installation-id", "2026-09-06T00:00:00+00:00")
        server._install_schema_41(connection, identity)
        connection.execute("DROP INDEX ep_consumer_credentials_scope_lookup")
        connection.execute("DROP INDEX ep_consumer_registrations_status_lookup")
        connection.execute("ALTER TABLE ep_consumer_credentials RENAME TO local_api_credentials")
        connection.execute("ALTER TABLE ep_consumer_registrations RENAME TO local_api_consumer_registrations")
        for migration in (
            server._migrate_schema_42, server._migrate_schema_43, server._migrate_schema_44,
            server._migrate_schema_45, server._migrate_schema_46, server._migrate_schema_47,
            server._migrate_schema_48, server._migrate_schema_49, server._migrate_schema_50,
            server._migrate_schema_51, server._migrate_schema_52,
        ):
            migration(connection)
        tables = server._table_names(connection)
        if "ep_consumer_credentials" in tables:
            connection.execute("DROP INDEX ep_consumer_credentials_scope_lookup")
            connection.execute("DROP INDEX ep_consumer_registrations_status_lookup")
            connection.execute("DROP TABLE ep_consumer_credentials")
            connection.execute("DROP TABLE ep_consumer_registrations")
        connection.commit()
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        connection.commit()
        connection.execute("PRAGMA legacy_alter_table=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            server._migrate_schema_53(connection)
        except Exception:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        connection.execute("PRAGMA legacy_alter_table=OFF")

    def test_transfer_preserves_values_and_uses_neutral_tables_only(self) -> None:
        connection = self._schema_52_with_legacy_authority()
        self.addCleanup(connection.close)
        token = "migration-token"
        connection.execute(
            "INSERT INTO local_api_consumer_registrations(consumer_id,project_id,status,created_at,updated_at,audit_metadata) VALUES(?,?,?,?,?,?)",
            ("consumer", "project", "ACTIVE", "now", "now", "{}"),
        )
        connection.execute(
            "INSERT INTO local_api_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)",
            ("credential", "consumer", "project", verifier(token), b"f" * 32, "now"),
        )
        self._migrate(connection)
        self.assertEqual(server._schema_version(connection), 53)
        self.assertEqual(
            connection.execute("SELECT value FROM engineering_metadata WHERE key='installation.schema_version'").fetchone()[0],
            "53",
        )
        self.assertEqual(
            connection.execute("SELECT verifier,fingerprint FROM ep_consumer_credentials").fetchone(),
            (verifier(token), b"f" * 32),
        )
        self.assertEqual(
            connection.execute("SELECT verifier,fingerprint FROM local_api_credentials").fetchone(),
            (verifier(token), b"f" * 32),
        )
        self.assertEqual(server._authenticated_consumer(connection, token, "project"), "consumer")
        connection.execute("UPDATE local_api_consumer_registrations SET status='REVOKED'")
        self.assertEqual(server._authenticated_consumer(connection, token, "project"), "consumer")

    def test_missing_or_malformed_legacy_source_fails_without_metadata_advance(self) -> None:
        connection = self._schema_52_with_legacy_authority()
        self.addCleanup(connection.close)
        connection.execute("DROP TABLE local_api_consumer_registrations")
        with self.assertRaisesRegex(server.ServerConfigurationError, "source is incomplete"):
            self._migrate(connection)
        self.assertEqual(server._schema_version(connection), 52)
        self.assertNotIn("ep_consumer_credentials", server._table_names(connection))

    def test_preexisting_destination_and_metadata_mismatch_fail_closed(self) -> None:
        connection = self._schema_52_with_legacy_authority()
        self.addCleanup(connection.close)
        server._install_ep_consumer_schema(connection)
        with self.assertRaisesRegex(server.ServerConfigurationError, "ambiguous parallel authority"):
            self._migrate(connection)
        self.assertEqual(server._schema_version(connection), 52)
        connection.execute("DROP TABLE ep_consumer_credentials")
        connection.execute("DROP TABLE ep_consumer_registrations")
        connection.execute("UPDATE engineering_metadata SET value='51' WHERE key='installation.schema_version'")
        with self.assertRaisesRegex(server.ServerConfigurationError, "metadata is invalid"):
            self._migrate(connection)
        self.assertEqual(server._schema_version(connection), 52)

    def test_interrupted_transfer_rolls_back_destination_and_preserves_legacy_authority(self) -> None:
        connection = self._schema_52_with_legacy_authority()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO local_api_consumer_registrations(consumer_id,project_id,status,created_at,updated_at,audit_metadata) VALUES(?,?,?,?,?,?)",
            ("consumer", "project", "ACTIVE", "now", "now", "{}"),
        )
        with patch(
            "engineering_platform.server._assert_exact_consumer_transfer",
            side_effect=server.ServerConfigurationError("injected transfer failure"),
        ):
            with self.assertRaisesRegex(server.ServerConfigurationError, "injected transfer failure"):
                self._migrate(connection)
        self.assertEqual(server._schema_version(connection), 52)
        self.assertNotIn("ep_consumer_credentials", server._table_names(connection))
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM local_api_consumer_registrations").fetchone()[0], 1,
        )

    def test_malformed_source_identity_shape_fails_closed(self) -> None:
        connection = self._schema_52_with_legacy_authority()
        self.addCleanup(connection.close)
        connection.execute("DROP TABLE local_api_consumer_registrations")
        connection.execute(
            "CREATE TABLE local_api_consumer_registrations (consumer_id TEXT,project_id TEXT,status TEXT,created_at TEXT,updated_at TEXT,disabled_at TEXT,revoked_at TEXT,audit_metadata TEXT)"
        )
        with self.assertRaisesRegex(server.ServerConfigurationError, "identity is invalid"):
            self._migrate(connection)
        self.assertEqual(server._schema_version(connection), 52)

    def test_fresh_and_reopened_server_schema_uses_only_neutral_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = server.initialize(root)
            with sqlite3.connect(root / server.SERVER_DATABASE_FILENAME) as connection:
                tables = server._table_names(connection)
                self.assertTrue({"ep_consumer_credentials", "ep_consumer_registrations"} <= tables)
                self.assertFalse({"local_api_credentials", "local_api_consumer_registrations"} & tables)
            self.assertEqual(server.initialize(root), identity)

    def test_historical_storage_schema_transfers_to_neutral_tables_transactionally(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = storage.database_path(root)
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as connection:
                for version in range(1, 39):
                    storage.MIGRATIONS[version](connection)
                    connection.execute("INSERT INTO engineering_schema_migrations(version) VALUES(?)", (version,))
                connection.execute(
                    "CREATE TABLE local_api_credentials (credential_id TEXT PRIMARY KEY,consumer_id TEXT NOT NULL,project_id TEXT NOT NULL,verifier BLOB NOT NULL UNIQUE,fingerprint BLOB NOT NULL UNIQUE,issued_at TEXT NOT NULL,expires_at TEXT,revoked_at TEXT,replaced_by_credential_id TEXT REFERENCES local_api_credentials(credential_id))"
                )
                connection.execute(
                    "CREATE TABLE local_api_consumer_registrations (consumer_id TEXT NOT NULL,project_id TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,disabled_at TEXT,revoked_at TEXT,audit_metadata TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(consumer_id,project_id))"
                )
                connection.execute("INSERT INTO engineering_schema_migrations(version) VALUES(39)")
                connection.execute("INSERT INTO engineering_schema_migrations(version) VALUES(40)")
                connection.execute(
                    "INSERT INTO local_api_consumer_registrations(consumer_id,project_id,status,created_at,updated_at,audit_metadata) VALUES(?,?,?,?,?,?)",
                    ("consumer", "project", "ACTIVE", "now", "now", "{}"),
                )
                connection.execute(
                    "INSERT INTO local_api_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)",
                    ("credential", "consumer", "project", verifier("storage-token"), b"f" * 32, "now"),
                )
            with storage.activate_storage_schema(root) as connection:
                self.assertEqual(storage._schema_version(connection), 42)
                self.assertEqual(
                    connection.execute("SELECT verifier,fingerprint FROM ep_consumer_credentials").fetchone(),
                    (verifier("storage-token"), b"f" * 32),
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM local_api_credentials").fetchone()[0], 1,
                )


if __name__ == "__main__":
    unittest.main()
