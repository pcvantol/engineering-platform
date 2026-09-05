from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from engineering_platform import server


class ReceiptRunProvenancePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "central"
        server.initialize(self.root)
        self.database = self.root / server.SERVER_DATABASE_FILENAME
        with sqlite3.connect(self.database) as db:
            self.installation = db.execute("SELECT value FROM engineering_metadata WHERE key='installation.instance_id'").fetchone()[0]
            for suffix in ("a", "b"):
                db.execute("INSERT INTO ep_project_registrations(project_id,attachment_contract,status,created_at,updated_at) VALUES(?,?,?,?,?)", (f"project-{suffix}", "TEST", "ACTIVE", "now", "now"))
                db.execute("INSERT INTO ep_repository_registrations(repository_id,project_id,authority_repository_id,role,attachment_contract,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (f"repo-{suffix}", f"project-{suffix}", f"repo-{suffix}", "authority", "TEST", "now", "now"))
                db.execute("INSERT INTO ep_submissions(submission_id,project_id,repository_id,producer_id,producer_type,transport,prompt,prompt_digest,constraints,state,admission,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (f"sub-{suffix}", f"project-{suffix}", f"repo-{suffix}", "test", "TEST", "HTTP", "p", "d", "{}", "QUEUED", "ADMITTED", "now"))
                db.execute("INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)", (f"run-{suffix}", f"project-{suffix}", "CLAIMED", "now", "now"))
                db.execute("INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (f"sub-{suffix}", f"project-{suffix}", f"repo-{suffix}", f"run-{suffix}", "CLAIMED", "CENTRAL:test", "now", "now"))
            self._insert(db, "sub-a", "run-a", "project-a", "repo-a", self.installation)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _insert(db: sqlite3.Connection, submission: str, run: str, project: str, repository: str, installation: str) -> None:
        db.execute("INSERT INTO ep_receipt_run_provenance(submission_id,run_id,project_id,repository_id,installation_id,created_at) VALUES(?,?,?,?,?,?)", (submission, run, project, repository, installation, "now"))

    def test_direct_sqlite_rejects_substitution_scope_and_rewrite(self) -> None:
        with sqlite3.connect(self.database) as db:
            failures = (
                ("sub-b", "run-a", "project-b", "repo-b", self.installation),
                ("sub-a", "run-b", "project-a", "repo-a", self.installation),
                ("sub-a", "run-a", "project-b", "repo-a", self.installation),
                ("sub-a", "run-a", "project-a", "repo-b", self.installation),
                ("sub-a", "run-a", "project-a", "repo-a", "other-installation"),
            )
            for attempt in failures:
                with self.subTest(attempt=attempt), self.assertRaises(sqlite3.IntegrityError):
                    self._insert(db, *attempt)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE ep_receipt_run_provenance SET project_id='project-b' WHERE submission_id='sub-a'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM ep_receipt_run_provenance WHERE submission_id='sub-a'")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ep_receipt_run_provenance").fetchone()[0], 1)

    def test_binding_survives_reopen_and_is_bidirectional(self) -> None:
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT run_id FROM ep_receipt_run_provenance WHERE submission_id='sub-a'").fetchone()[0], "run-a")
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT submission_id FROM ep_receipt_run_provenance WHERE run_id='run-a'").fetchone()[0], "sub-a")

    def _downgrade_to_schema_51(self) -> None:
        """Construct a real pre-52 database shape for the installed upgrade canary."""
        with sqlite3.connect(self.database) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("DROP TRIGGER ep_receipt_run_provenance_scope_insert")
            db.execute("DROP TRIGGER ep_receipt_run_provenance_immutable_update")
            db.execute("DROP TRIGGER ep_receipt_run_provenance_immutable_delete")
            db.execute("DROP TABLE ep_receipt_run_provenance")
            db.execute("ALTER TABLE ep_installations RENAME TO ep_installations_schema52")
            db.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version IN (41,42,43,44,45,46,47,48,49,50,51)))")
            db.execute("INSERT INTO ep_installations(instance_id,created_at,schema_version) SELECT instance_id,created_at,51 FROM ep_installations_schema52")
            db.execute("DROP TABLE ep_installations_schema52")
            db.execute("DELETE FROM engineering_schema_migrations WHERE version=52")
            db.execute("UPDATE engineering_metadata SET value='51' WHERE key='installation.schema_version'")

    def test_schema_51_upgrade_backfills_every_verified_canonical_binding(self) -> None:
        """A pre-52 installed database upgrades in place without a second authority."""
        self._downgrade_to_schema_51()
        server.initialize(self.root)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT submission_id,run_id,project_id,repository_id,installation_id FROM ep_receipt_run_provenance ORDER BY submission_id").fetchall(), [("sub-a", "run-a", "project-a", "repo-a", self.installation), ("sub-b", "run-b", "project-b", "repo-b", self.installation)])
            self.assertEqual(db.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0], 52)

    def test_schema_51_upgrade_rejects_an_incomplete_or_conflicting_import(self) -> None:
        self._downgrade_to_schema_51()
        with sqlite3.connect(self.database) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("UPDATE ep_parity_lifecycle_dispatches SET project_id='project-a' WHERE submission_id='sub-b'")
        with self.assertRaisesRegex(server.ServerConfigurationError, "provenance migration is incomplete"):
            server.initialize(self.root)
