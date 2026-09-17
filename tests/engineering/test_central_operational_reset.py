from __future__ import annotations

from collections import namedtuple
from contextlib import closing, contextmanager, redirect_stdout
import gc
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import warnings
from unittest.mock import patch

from engineering_platform import central_operational_reset as reset
from engineering_platform import server, submission_service
from engineering_platform.operational_installation_lock import OperationalInstallationLock
from engineering_platform.storage import sqlite_connection


class CentralOperationalResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.root = Path(self.temporary.name) / "data"
        self.backups = Path(self.temporary.name) / "protected-backups"
        server.initialize(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _populate(self) -> submission_service.SubmissionRequest:
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            connection.execute(
                "INSERT INTO ep_project_registrations VALUES(?,?,?,?,?)",
                ("project-a", "{}", "ACTIVE", "now", "now"),
            )
            connection.execute(
                "INSERT INTO ep_repository_registrations VALUES(?,?,?,?,?,?,?)",
                ("repo-a", "project-a", "repo-a", "authority", "{}", "now", "now"),
            )
            connection.execute(
                "INSERT INTO ep_consumer_registrations VALUES(?,?,?,?,?,?,?,?)",
                ("consumer-a", "project-a", "ACTIVE", "now", "now", None, None, "{}"),
            )
            connection.execute(
                "INSERT INTO ep_consumer_credentials VALUES(?,?,?,?,?,?,?,?,?)",
                ("credential-a", "consumer-a", "project-a", b"v" * 32, b"f" * 32,
                 "now", None, None, None),
            )
            connection.execute(
                "INSERT INTO execution_projections VALUES(?,?,?,?,?)",
                ("configuration-a", "CONFIGURATION", "{}", "digest", "now"),
            )
            connection.execute(
                "INSERT INTO execution_projections VALUES(?,?,?,?,?)",
                ("cache-a", "PROJECTION", "{}", "digest", "now"),
            )
            request = submission_service.SubmissionRequest(
                "project-a", "repo-a", "human", "HUMAN", "1", "synthetic prompt", "HTTP",
                idempotency_key="already-used",
            )
            submission_service.submit(connection, request)
        (self.root / "artifacts").mkdir()
        (self.root / "artifacts" / "terminal.json").write_text("{}", encoding="utf-8")
        (self.root / "file-inbox").mkdir()
        (self.root / "file-inbox" / "historical.json").write_text("{}", encoding="utf-8")
        return request

    def _prepared(self, *, operation_id: str = "reset-fixture-0001") -> tuple[str, str]:
        plan = reset.preview(self.root)
        reset.prepare(
            self.root, operation_id=operation_id, plan_digest=str(plan["plan_digest"]),
            backup_root=self.backups,
            allowed_fk_findings=tuple(plan["review_required"]),
        )
        return operation_id, str(plan["plan_digest"])

    def test_schema_owned_inventory_is_exhaustive_and_preview_is_read_only(self) -> None:
        before = hashlib.sha256((self.root / "epdata.sqlite").read_bytes()).hexdigest()
        plan = reset.preview(self.root)
        after = hashlib.sha256((self.root / "epdata.sqlite").read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(plan["execution_state"], "ALLOWED")
        self.assertFalse(plan["unknown_tables"])
        self.assertFalse(plan["missing_tables"])
        self.assertTrue(plan["schema_objects"])
        self.assertFalse(any(
            str(item["name"]).startswith("sqlite_") for item in plan["schema_objects"]
        ))
        with sqlite_connection(f"file:{self.root / 'epdata.sqlite'}?mode=ro", uri=True) as connection:
            self.assertEqual(reset._tables(connection), set(reset.MAPPED_TABLES))

    def test_preview_of_older_schema_blocks_without_migrating(self) -> None:
        with closing(sqlite3.connect(self.root / "epdata.sqlite")) as connection:
            connection.execute("DELETE FROM engineering_schema_migrations WHERE version=68")
            connection.execute("INSERT INTO engineering_schema_migrations(version) VALUES(67)")
            connection.commit()
        before = hashlib.sha256((self.root / "epdata.sqlite").read_bytes()).hexdigest()
        plan = reset.preview(self.root)
        self.assertEqual(hashlib.sha256((self.root / "epdata.sqlite").read_bytes()).hexdigest(), before)
        self.assertIn("SCHEMA_UNSUPPORTED", plan["blocking_codes"])

    def test_unknown_table_and_unknown_root_file_block_but_known_recovery_files_do_not(self) -> None:
        (self.root / "central.sqlite").write_bytes(b"")
        (self.root / "epdata.sqlite.pre-2.3.32.backup").write_bytes(b"backup")
        (self.root / ".forge-ep-consumer-recovery.lock").write_text("", encoding="utf-8")
        self.assertNotIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", reset.preview(self.root)["blocking_codes"])
        (self.root / "user-notes.txt").write_text("preserve", encoding="utf-8")
        plan = reset.preview(self.root)
        self.assertIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", plan["blocking_codes"])
        self.assertTrue((self.root / "user-notes.txt").is_file())
        (self.root / "user-notes.txt").unlink()
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            connection.execute("CREATE TABLE plugin_unknown(id INTEGER PRIMARY KEY)")
        self.assertIn("TABLE_CLASSIFICATION_INCOMPLETE", reset.preview(self.root)["blocking_codes"])

    def test_complete_reset_preserves_bindings_credentials_and_configuration(self) -> None:
        request = self._populate()
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            retired_submission_id = str(connection.execute(
                "SELECT submission_id FROM ep_submissions"
            ).fetchone()[0])
        operation_id, digest = self._prepared()
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            with self.assertRaisesRegex(submission_service.SubmissionError, "PLATFORM_MAINTENANCE_ACTIVE"):
                submission_service.submit(connection, request)
        reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        result = reset.verify(self.root, operation_id=operation_id, plan_digest=digest)
        self.assertEqual(result["quick_check"], ["ok"])
        self.assertEqual(result["foreign_key_errors"], 0)
        self.assertFalse(result["operational_rows_remaining"])
        reset.finish(self.root, operation_id=operation_id, plan_digest=digest)
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_project_registrations").fetchone(), (1,))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_repository_registrations").fetchone(), (1,))
            self.assertEqual(connection.execute("SELECT verifier FROM ep_consumer_credentials").fetchone(), (b"v" * 32,))
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM execution_projections WHERE classification='CONFIGURATION'"
            ).fetchone(), (1,))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_submissions").fetchone(), (0,))
            with self.assertRaisesRegex(submission_service.SubmissionError, "IDEMPOTENCY_RETIRED"):
                submission_service.submit(connection, request)
            changed = submission_service.SubmissionRequest(
                **{**request.__dict__, "prompt": "different bytes"}
            )
            with self.assertRaisesRegex(submission_service.SubmissionError, "IDEMPOTENCY_CONFLICT"):
                submission_service.submit(connection, changed)
            fresh = submission_service.SubmissionRequest(
                **{**request.__dict__, "idempotency_key": None, "prompt": "new work"}
            )
            with patch.object(
                submission_service.secrets, "token_hex",
                side_effect=[retired_submission_id.removeprefix("sub-"), "n" * 32],
            ):
                allocated = submission_service.submit(connection, fresh)
            self.assertEqual(allocated.submission_id, "sub-" + "n" * 32)
        archive = self.root / "operational-reset-archive" / operation_id
        self.assertTrue((archive / "artifacts" / "terminal.json").is_file())
        self.assertTrue((archive / "file-inbox" / "historical.json").is_file())

    def test_empty_reset_is_meaningful_and_idempotent_for_same_operation(self) -> None:
        operation_id, digest = self._prepared(operation_id="reset-empty-0001")
        first = reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        second = reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        self.assertEqual(first["generation_after"], 1)
        self.assertEqual(second["generation_after"], 1)
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)

    def test_changed_source_plan_target_and_request_are_rejected(self) -> None:
        plan = reset.preview(self.root)
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            connection.execute(
                "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES('ep_server','{}','now')"
            )
        with self.assertRaisesRegex(reset.OperationalResetError, "PLAN_DIGEST_MISMATCH"):
            reset.prepare(self.root, operation_id="reset-stale-0001",
                          plan_digest=str(plan["plan_digest"]), backup_root=self.backups)
        other = Path(self.temporary.name) / "other"
        server.initialize(other)
        with self.assertRaisesRegex(reset.OperationalResetError, "PLAN_DIGEST_MISMATCH"):
            reset.prepare(other, operation_id="reset-other-0001",
                          plan_digest=str(plan["plan_digest"]), backup_root=self.backups)

    def test_existing_installation_lock_blocks_prepare(self) -> None:
        plan = reset.preview(self.root)
        lock = OperationalInstallationLock(self.root)
        lock.acquire("update-lock-0001")
        try:
            with self.assertRaisesRegex(reset.OperationalResetError, "MAINTENANCE_LOCK_BUSY"):
                reset.prepare(self.root, operation_id="reset-locked-0001",
                              plan_digest=str(plan["plan_digest"]), backup_root=self.backups)
        finally:
            lock.release("update-lock-0001")

    def test_crash_before_database_commit_has_no_half_purge_and_resumes_same_operation(self) -> None:
        self._populate()
        operation_id, digest = self._prepared(operation_id="reset-crash-0001")
        with patch.object(reset, "_delete_operational", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_submissions").fetchone(), (1,))
        resumed = reset.resume(self.root, operation_id=operation_id, plan_digest=digest)
        self.assertEqual(resumed["operation"]["state"], "VERIFIED")

    def test_resume_after_database_commit_verifies_without_a_second_reset(self) -> None:
        self._populate()
        operation_id, digest = self._prepared(operation_id="reset-after-commit-0001")
        applied = reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        self.assertEqual(applied["state"], "DB_APPLIED")
        with self.assertRaisesRegex(reset.OperationalResetError, "ABORT_AFTER_EFFECT_FORBIDDEN"):
            reset.abort(self.root, operation_id=operation_id, plan_digest=digest)
        resumed = reset.resume(self.root, operation_id=operation_id, plan_digest=digest)
        self.assertEqual(resumed["operation"]["state"], "VERIFIED")
        self.assertEqual(resumed["dataset_generation"], 1)

    def test_durable_fence_survives_new_connection_and_blocks_server_restart(self) -> None:
        operation_id, _digest = self._prepared(operation_id="reset-fence-0001")
        self.assertTrue(reset.maintenance_active(self.root))
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "EP_OPERATIONAL_MAINTENANCE_ACTIVE"):
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) "
                    "VALUES('ep_server','{}','now')"
                )
        with self.assertRaisesRegex(server.ServerConfigurationError,
                                    "EP_OPERATIONAL_MAINTENANCE_ACTIVE"):
            server.start(self.root)
        self.assertEqual(reset.status(self.root, operation_id=operation_id)["operation"]["state"],
                         "AUTHORIZED")

    def test_partial_artifact_move_reconciles_forward(self) -> None:
        self._populate()
        operation_id, digest = self._prepared(operation_id="reset-artifact-0001")
        with reset._central_connection(self.root / "epdata.sqlite") as connection:
            connection.execute("BEGIN IMMEDIATE")
            reset._transition(
                connection, operation_id, "AUTHORIZED", "ARTIFACTS_ARCHIVING",
            )
            connection.execute("COMMIT")
        archive = self.root / "operational-reset-archive" / operation_id
        archive.mkdir(parents=True)
        (self.root / "artifacts").replace(archive / "artifacts")
        reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        self.assertTrue((archive / "artifacts" / "terminal.json").is_file())
        self.assertTrue((self.root / "artifacts").is_dir())

    def test_operational_fk_requires_exact_approval_and_preservation_fk_blocks(self) -> None:
        connection = sqlite3.connect(self.root / "epdata.sqlite")
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO ep_execution_leases VALUES(?,?,?,?,?,?)",
            ("lease-orphan", "run-missing", "host", "now", "later", None),
        )
        connection.commit(); connection.close()
        plan = reset.preview(self.root)
        self.assertEqual(plan["execution_state"], "REVIEW_REQUIRED")
        with self.assertRaisesRegex(reset.OperationalResetError, "OPERATIONAL_FK_APPROVAL_MISMATCH"):
            reset.prepare(self.root, operation_id="reset-fk-0001", plan_digest=str(plan["plan_digest"]),
                          backup_root=self.backups)
        operation_id = "reset-fk-0002"
        reset.prepare(self.root, operation_id=operation_id, plan_digest=str(plan["plan_digest"]),
                      backup_root=self.backups, allowed_fk_findings=tuple(plan["review_required"]))
        reset.apply(self.root, operation_id=operation_id, plan_digest=str(plan["plan_digest"]))
        self.assertEqual(reset.verify(self.root, operation_id=operation_id,
                                      plan_digest=str(plan["plan_digest"]))["foreign_key_errors"], 0)

        other = Path(self.temporary.name) / "preserve-fk"
        server.initialize(other)
        connection = sqlite3.connect(other / "epdata.sqlite")
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO ep_external_producer_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("binding", "GITHUB", "REPOSITORY", "owner/repo", "missing-project",
             "missing-repo", "ACTIVE", 1, "now", "actor", "now", "{}"),
        )
        connection.commit(); connection.close()
        blocked = reset.preview(other)
        self.assertIn("PRESERVATION_INTEGRITY_FAILED", blocked["blocking_codes"])

    def test_backup_paths_data_root_symlink_and_active_runtime_fail_closed(self) -> None:
        plan = reset.preview(self.root)
        inside = self.root / "backups"
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_PATH_INSIDE_ACTIVE_DATA_ROOT"):
            reset.prepare(self.root, operation_id="reset-inside-0001",
                          plan_digest=str(plan["plan_digest"]), backup_root=inside)

        other = Path(self.temporary.name) / "symlink-root"
        server.initialize(other)
        (Path(self.temporary.name) / "outside").mkdir()
        (other / "artifacts").symlink_to(Path(self.temporary.name) / "outside", target_is_directory=True)
        self.assertIn("EXTERNAL_SYMLINK_UNSAFE", reset.preview(other)["blocking_codes"])

        active = Path(self.temporary.name) / "active"
        server.initialize(active)
        (active / "runtime.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        self.assertIn("TARGET_WRITER_ACTIVE", reset.preview(active)["blocking_codes"])

        backup_target = Path(self.temporary.name) / "backup-target"
        backup_target.mkdir()
        backup_link = Path(self.temporary.name) / "backup-link"
        backup_link.symlink_to(backup_target, target_is_directory=True)
        other = Path(self.temporary.name) / "backup-fixture"
        server.initialize(other)
        other_plan = reset.preview(other)
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_PATH_UNSAFE"):
            reset.prepare(other, operation_id="reset-backup-link-0001",
                          plan_digest=str(other_plan["plan_digest"]), backup_root=backup_link)
        self.assertTrue(reset.maintenance_active(other))
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_ROOT_BINDING_MISMATCH"):
            reset.resume(other, operation_id="reset-backup-link-0001",
                         plan_digest=str(other_plan["plan_digest"]),
                         backup_root=Path(self.temporary.name) / "different-backup")
        aborted = reset.abort(other, operation_id="reset-backup-link-0001",
                              plan_digest=str(other_plan["plan_digest"]))
        self.assertEqual(aborted["state"], "ABORTED")
        self.assertFalse(reset.maintenance_active(other))

        backup_parent_link = Path(self.temporary.name) / "backup-parent-link"
        backup_parent_link.symlink_to(backup_target, target_is_directory=True)
        parent_fixture = Path(self.temporary.name) / "backup-parent-fixture"
        server.initialize(parent_fixture)
        parent_plan = reset.preview(parent_fixture)
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_PATH_UNSAFE"):
            reset.prepare(parent_fixture, operation_id="reset-backup-parent-link-0001",
                          plan_digest=str(parent_plan["plan_digest"]),
                          backup_root=backup_parent_link / "not-created")

        data_link = Path(self.temporary.name) / "data-link"
        data_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(reset.OperationalResetError, "DATA_ROOT_UNSAFE"):
            reset.preview(data_link)

    def test_backup_restore_validation_permissions_space_and_invalid_archive(self) -> None:
        self._populate()
        operation_id, digest = self._prepared(operation_id="reset-backup-proof-0001")
        backup = self.backups / operation_id
        backup.chmod(0o755)
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_PERMISSIONS_UNSAFE"):
            reset.verify_backup(backup, operation_id=operation_id, plan_digest=digest)
        backup.chmod(0o700)
        database = backup / "central.sqlite"
        database.write_bytes(database.read_bytes() + b"tamper")
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_BINDING_INVALID"):
            reset.verify_backup(backup, operation_id=operation_id, plan_digest=digest)

        low_space = Path(self.temporary.name) / "low-space"
        server.initialize(low_space)
        low_plan = reset.preview(low_space)
        usage = namedtuple("usage", "total used free")(1024, 1024, 0)
        with patch.object(reset.shutil, "disk_usage", return_value=usage):
            with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_SPACE_INSUFFICIENT"):
                reset.prepare(low_space, operation_id="reset-low-space-0001",
                              plan_digest=str(low_plan["plan_digest"]),
                              backup_root=Path(self.temporary.name) / "low-space-backup")

        unwritable_shape = Path(self.temporary.name) / "not-a-directory"
        unwritable_shape.write_text("file", encoding="utf-8")
        unsafe = Path(self.temporary.name) / "unsafe-shape"
        server.initialize(unsafe)
        unsafe_plan = reset.preview(unsafe)
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_PATH_UNSAFE"):
            reset.prepare(unsafe, operation_id="reset-unwritable-0001",
                          plan_digest=str(unsafe_plan["plan_digest"]),
                          backup_root=unwritable_shape)

        archive_root = Path(self.temporary.name) / "archive-fixture"
        archive_backup = Path(self.temporary.name) / "archive-backup"
        server.initialize(archive_root)
        (archive_root / "artifacts").mkdir()
        (archive_root / "artifacts" / "evidence.json").write_text("{}", encoding="utf-8")
        archive_plan = reset.preview(archive_root)
        reset.prepare(archive_root, operation_id="reset-invalid-archive-0001",
                      plan_digest=str(archive_plan["plan_digest"]), backup_root=archive_backup)
        reset.apply(archive_root, operation_id="reset-invalid-archive-0001",
                    plan_digest=str(archive_plan["plan_digest"]))
        archived = archive_root / "operational-reset-archive" / "reset-invalid-archive-0001"
        (archived / "artifacts" / "evidence.json").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(reset.OperationalResetError,
                                    "ARCHIVE_RECONCILIATION_FAILED"):
            reset.verify(archive_root, operation_id="reset-invalid-archive-0001",
                         plan_digest=str(archive_plan["plan_digest"]))

    def test_chat_parent_migration_preserves_rows_and_enforces_canonical_run(self) -> None:
        with closing(sqlite3.connect(self.root / "epdata.sqlite")) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TRIGGER execution_chat_messages_immutable_update")
            connection.execute("DROP INDEX execution_chat_messages_run_created")
            connection.execute("ALTER TABLE execution_chat_messages RENAME TO chat_schema68")
            connection.execute(
                "CREATE TABLE execution_chat_messages(id INTEGER PRIMARY KEY,"
                "run_id TEXT NOT NULL REFERENCES prompt_execution_history(run_id),"
                "role TEXT NOT NULL,content TEXT NOT NULL,model TEXT,created_at TEXT NOT NULL)"
            )
            connection.execute("DROP TABLE chat_schema68")
            connection.execute(
                "INSERT INTO ep_project_registrations VALUES('p','{}','ACTIVE','now','now')"
            )
            connection.execute(
                "INSERT INTO ep_execution_runs VALUES('run-chat','p','BLOCKED','now','now',NULL)"
            )
            connection.execute(
                "INSERT INTO execution_chat_messages VALUES(1,'run-chat','user','kept',NULL,'now')"
            )
            connection.execute("DELETE FROM engineering_schema_migrations WHERE version=68")
            connection.execute("INSERT INTO engineering_schema_migrations(version) VALUES(67)")
            connection.execute("UPDATE engineering_metadata SET value='67' WHERE key='installation.schema_version'")
            connection.execute("ALTER TABLE ep_installations RENAME TO installation_schema68")
            connection.execute(
                "CREATE TABLE ep_installations(instance_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,"
                "schema_version INTEGER NOT NULL CHECK(schema_version=67))"
            )
            connection.execute(
                "INSERT INTO ep_installations SELECT instance_id,created_at,67 FROM installation_schema68"
            )
            connection.execute("DROP TABLE installation_schema68")
            connection.commit()
        server.initialize(self.root)
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            parent = connection.execute("PRAGMA foreign_key_list(execution_chat_messages)").fetchone()[2]
            self.assertEqual(parent, "ep_execution_runs")
            self.assertEqual(connection.execute("SELECT content FROM execution_chat_messages").fetchone(), ("kept",))
            self.assertFalse(list(connection.execute("PRAGMA foreign_key_check")))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO execution_chat_messages(run_id,role,content,created_at) "
                    "VALUES('missing','user','blocked','now')"
                )

    def test_apply_refuses_terminal_states_and_aborted_is_not_an_active_submission_fence(self) -> None:
        request = self._populate()
        operation_id, digest = self._prepared(operation_id="reset-aborted-0001")
        reset.abort(self.root, operation_id=operation_id, plan_digest=digest)
        self.assertFalse(reset.maintenance_active(self.root))
        with self.assertRaisesRegex(reset.OperationalResetError,
                                    "OPERATION_STATE_INVALID_FOR_APPLY"):
            reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            fresh = submission_service.SubmissionRequest(
                **{**request.__dict__, "idempotency_key": None, "prompt": "after abort"}
            )
            self.assertTrue(submission_service.submit(connection, fresh).submission_id)

        completed = Path(self.temporary.name) / "completed"
        completed_backup = Path(self.temporary.name) / "completed-backup"
        server.initialize(completed)
        plan = reset.preview(completed)
        reset.prepare(
            completed, operation_id="reset-completed-0001",
            plan_digest=str(plan["plan_digest"]), backup_root=completed_backup,
        )
        reset.apply(completed, operation_id="reset-completed-0001",
                    plan_digest=str(plan["plan_digest"]))
        reset.verify(completed, operation_id="reset-completed-0001",
                     plan_digest=str(plan["plan_digest"]))
        reset.finish(completed, operation_id="reset-completed-0001",
                     plan_digest=str(plan["plan_digest"]))
        with self.assertRaisesRegex(reset.OperationalResetError,
                                    "OPERATION_STATE_INVALID_FOR_APPLY"):
            reset.apply(completed, operation_id="reset-completed-0001",
                        plan_digest=str(plan["plan_digest"]))

    def test_backup_copy_rejects_existing_content_and_nested_symlinks_and_resumes_after_copy_crash(self) -> None:
        plan = reset.preview(self.root)
        operation_id = "reset-existing-backup-0001"
        occupied = self.backups / operation_id
        occupied.mkdir(parents=True)
        self.backups.chmod(0o700)
        occupied.chmod(0o700)
        marker = occupied / "foreign.txt"
        marker.write_text("do not overwrite", encoding="utf-8")
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_INVALID"):
            reset.prepare(
                self.root, operation_id=operation_id,
                plan_digest=str(plan["plan_digest"]), backup_root=self.backups,
            )
        self.assertEqual(marker.read_text(encoding="utf-8"), "do not overwrite")
        reset.abort(self.root, operation_id=operation_id, plan_digest=str(plan["plan_digest"]))

        crash_root = Path(self.temporary.name) / "copy-crash"
        crash_backup = Path(self.temporary.name) / "copy-crash-backup"
        server.initialize(crash_root)
        crash_plan = reset.preview(crash_root)
        with patch.object(reset, "_copy_external", side_effect=RuntimeError("synthetic crash")):
            with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
                reset.prepare(
                    crash_root, operation_id="reset-copy-crash-0001",
                    plan_digest=str(crash_plan["plan_digest"]), backup_root=crash_backup,
                )
        self.assertEqual(
            reset.status(crash_root, operation_id="reset-copy-crash-0001")["operation"]["state"],
            "PREPARING",
        )
        resumed = reset.resume(
            crash_root, operation_id="reset-copy-crash-0001",
            plan_digest=str(crash_plan["plan_digest"]), backup_root=crash_backup,
        )
        self.assertEqual(resumed["operation"]["state"], "VERIFIED")
        self.assertTrue((crash_backup / "reset-copy-crash-0001" / "manifest.json").is_file())
        self.assertTrue(any(path.name.endswith(".partial") for path in crash_backup.iterdir()))

        source_root = Path(self.temporary.name) / "copy-source"
        outside = Path(self.temporary.name) / "copy-outside"
        destination = Path(self.temporary.name) / "copy-destination"
        (source_root / "artifacts").mkdir(parents=True)
        outside.mkdir()
        (outside / "secret").write_text("secret", encoding="utf-8")
        (source_root / "artifacts" / "nested").symlink_to(outside, target_is_directory=True)
        destination.mkdir()
        entry = [{
            "root": "artifacts", "path": "nested/secret", "sha256": reset._file_digest(outside / "secret"),
        }]
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_SOURCE_CHANGED"):
            reset._copy_external(source_root, destination, entry)
        (source_root / "artifacts" / "nested").unlink()
        (source_root / "artifacts" / "nested").mkdir()
        (source_root / "artifacts" / "nested" / "secret").write_text("secret", encoding="utf-8")
        (destination / "files").mkdir()
        (destination / "files" / "artifacts").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_PATH_UNSAFE"):
            reset._copy_external(source_root, destination, entry)
        self.assertFalse((outside / "nested").exists())

    def test_stored_manifest_digest_is_mandatory_for_apply_and_verify(self) -> None:
        operation_id, digest = self._prepared(operation_id="reset-manifest-binding-0001")
        manifest = self.backups / operation_id / "manifest.json"
        original = manifest.read_bytes()
        unexpected = self.backups / operation_id / "unexpected.bin"
        unexpected.write_bytes(b"foreign")
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_FILE_SET_INVALID"):
            reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        unexpected.unlink()
        manifest.write_bytes(original.rstrip() + b"  \n")
        with self.assertRaisesRegex(reset.OperationalResetError,
                                    "BACKUP_MANIFEST_DIGEST_MISMATCH"):
            reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        manifest.write_bytes(original)
        reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)
        manifest.write_bytes(original.rstrip() + b" \n")
        with self.assertRaisesRegex(reset.OperationalResetError,
                                    "BACKUP_MANIFEST_DIGEST_MISMATCH"):
            reset.verify(self.root, operation_id=operation_id, plan_digest=digest)
        manifest.unlink()
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_INVALID"):
            reset.verify(self.root, operation_id=operation_id, plan_digest=digest)

    def test_maintenance_tables_are_owner_guarded_and_preview_requires_every_fence(self) -> None:
        operation_id, digest = self._prepared(operation_id="reset-guard-0001")
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE ep_operational_reset_operations SET updated_at='forged' WHERE operation_id=?",
                    (operation_id,),
                )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "INSERT INTO ep_operational_identity_tombstones VALUES(?,?,?,?,?)",
                    ("run_id", "forged", None, operation_id, "now"),
                )
        self.assertEqual(
            reset.abort(self.root, operation_id=operation_id, plan_digest=digest)["state"],
            "ABORTED",
        )
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            connection.execute("DROP TRIGGER ep_operational_reset_block_ep_submissions_insert")
        plan = reset.preview(self.root)
        self.assertIn("WRITER_FENCE_INCOMPLETE", plan["blocking_codes"])
        self.assertIn("ep_operational_reset_block_ep_submissions_insert",
                      plan["missing_writer_fences"])

    def test_finish_reproves_backup_database_bindings_and_all_active_ingest_routes(self) -> None:
        self._populate()
        operation_id, digest = self._prepared(operation_id="reset-finish-fence-0001")
        reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)
        delayed = self.root / "file-inbox" / "delayed.json"
        delayed.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(reset.OperationalResetError,
                                    "ARCHIVE_RECONCILIATION_AMBIGUOUS"):
            reset.finish(self.root, operation_id=operation_id, plan_digest=digest)
        self.assertEqual(reset.status(self.root, operation_id=operation_id)["operation"]["state"],
                         "VERIFIED")
        delayed.unlink()
        pending = self.root / "runtime" / "pending-central-data-import.json"
        pending.parent.mkdir(exist_ok=True)
        pending.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(reset.OperationalResetError,
                                    "POST_RESET_VERIFICATION_FAILED"):
            reset.finish(self.root, operation_id=operation_id, plan_digest=digest)
        pending.unlink()
        self.assertEqual(
            reset.finish(self.root, operation_id=operation_id, plan_digest=digest)["state"],
            "COMPLETED",
        )

    def test_nested_preserved_external_inventory_is_explicit_and_unknown_appdata_blocks(self) -> None:
        operations = self.root / "operations" / "update-known-0001"
        operations.mkdir(parents=True)
        (operations / "operation.json").write_text("{}", encoding="utf-8")
        plan = reset.preview(self.root)
        self.assertNotIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", plan["blocking_codes"])
        classified = {str(item["path"]): item["classification"] for item in plan["preserved_external"]}
        self.assertEqual(classified["operations/update-known-0001/operation.json"],
                         "INSTALLATION_OPERATION_AUDIT")
        operation_unknown = operations / "user-payload.bin"
        operation_unknown.write_bytes(b"keep")
        self.assertIn("operations/update-known-0001/user-payload.bin",
                      reset.preview(self.root)["unknown_external_paths"])
        operation_unknown.unlink()
        runtime = self.root / "runtime"
        runtime.mkdir(exist_ok=True)
        unknown = runtime / "user-payload.bin"
        unknown.write_bytes(b"keep")
        blocked = reset.preview(self.root)
        self.assertIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", blocked["blocking_codes"])
        self.assertIn("runtime/user-payload.bin", blocked["unknown_external_paths"])
        self.assertEqual(unknown.read_bytes(), b"keep")

    def test_cli_argument_and_internal_errors_are_stable_secret_free_contracts(self) -> None:
        def invoke(arguments: list[str]) -> tuple[int, dict[str, object]]:
            output = io.StringIO()
            with redirect_stdout(output):
                code = reset.main(arguments)
            return code, json.loads(output.getvalue())

        code, receipt = invoke([])
        self.assertEqual((code, receipt["error_code"]), (2, "CLI_ARGUMENT_INVALID"))
        required = {
            "contract_version", "product", "command", "operation_id", "state", "allowed",
            "target", "profile", "dataset_generation", "plan_digest",
            "relevant_revision_digest", "backup", "counts", "blockers", "integrity",
            "preserved_bindings_digest", "error_code",
        }
        self.assertTrue(required.issubset(receipt))
        with patch.object(reset, "preview", side_effect=ValueError("secret=do-not-print")):
            code, receipt = invoke(["preview", "--data-root", str(self.root)])
        self.assertEqual((code, receipt["error_code"]), (2, "MAINTENANCE_COMMAND_FAILED"))
        self.assertNotIn("do-not-print", json.dumps(receipt))
        self.assertIsNone(receipt["target"]["database_path"])

    def test_central_chat_writer_enables_foreign_keys_on_the_used_connection(self) -> None:
        self._populate()
        statements: list[str] = []
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            submission_id = str(connection.execute(
                "SELECT submission_id FROM ep_submissions ORDER BY created_at LIMIT 1"
            ).fetchone()[0])
            connection.execute(
                "INSERT INTO ep_execution_runs VALUES(?,?,?,?,?,?)",
                ("run-chat-fk", "project-a", "BLOCKED", "now", "now", None),
            )
            connection.execute(
                "INSERT INTO ep_parity_lifecycle_dispatches("
                "submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (submission_id, "project-a", "repo-a", "run-chat-fk", "BLOCKED",
                 "CENTRAL:prompt", "now", "now"),
            )

        real_connection = sqlite_connection

        @contextmanager
        def observed_connection(*args: object, **kwargs: object):
            with real_connection(*args, **kwargs) as connection:
                connection.set_trace_callback(statements.append)
                yield connection

        with patch.object(server.storage, "sqlite_connection", observed_connection):
            server._central_console_append_chat_message(
                self.root, "project-a", "run-chat-fk", "user", "safe",
            )
        self.assertTrue(any(statement.casefold() == "pragma foreign_keys=on"
                            for statement in statements))
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            self.assertEqual(connection.execute(
                "SELECT content FROM execution_chat_messages WHERE run_id='run-chat-fk'"
            ).fetchone(), ("safe",))

    def test_reset_exception_paths_release_sqlite_connections_without_resource_warnings(self) -> None:
        self._populate()
        operation_id, digest = self._prepared(operation_id="reset-resource-0001")
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always", ResourceWarning)
            with patch.object(reset, "_delete_operational", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
            gc.collect()
        self.assertFalse([item for item in observed if item.category is ResourceWarning])

    def test_shared_json_contract_contains_no_verifier(self) -> None:
        self._populate()
        plan = reset.preview(self.root)
        contract = reset.contract_readback(
            self.root, command="preview", operation_id=None, details=plan,
        )
        self.assertEqual(contract["contract_version"], "operational-reset-v1")
        self.assertEqual(contract["product"], "engineering-platform")
        self.assertEqual(contract["target"]["instance_id"], plan["target"]["instance_id"])
        rendered = json.dumps(contract)
        self.assertNotIn((b"v" * 32).hex(), rendered)
        self.assertNotIn((b"f" * 32).hex(), rendered)

    def test_cli_state_machine_emits_stable_shared_contract(self) -> None:
        def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
            output = io.StringIO()
            with redirect_stdout(output):
                code = reset.main(list(arguments))
            return code, json.loads(output.getvalue())

        common = ("--data-root", str(self.root))
        code, preview = invoke("preview", *common)
        self.assertEqual(code, 0)
        operation_id = "reset-cli-0001"
        mutation = (
            "--operation-id", operation_id,
            "--plan-digest", str(preview["plan_digest"]),
        )
        code, prepared = invoke(
            "prepare", *common, *mutation, "--backup-root", str(self.backups),
        )
        self.assertEqual((code, prepared["state"]), (0, "AUTHORIZED"))
        self.assertTrue(prepared["backup"]["verified"])
        code, status = invoke("status", *common, "--operation-id", operation_id)
        self.assertEqual((code, status["state"]), (0, "AUTHORIZED"))
        self.assertEqual(invoke("apply", *common, *mutation)[1]["state"], "DB_APPLIED")
        self.assertEqual(invoke("verify", *common, *mutation)[1]["state"], "VERIFIED")
        self.assertEqual(invoke("finish", *common, *mutation)[1]["state"], "COMPLETED")
        required = {
            "contract_version", "product", "command", "operation_id", "state", "allowed",
            "target", "profile", "dataset_generation", "plan_digest",
            "relevant_revision_digest", "backup", "counts", "blockers", "integrity",
            "preserved_bindings_digest",
        }
        self.assertTrue(required.issubset(prepared))

        code, error = invoke(
            "prepare", *common, "--operation-id", "reset-cli-error-0001",
            "--plan-digest", "sha256:wrong", "--backup-root", str(self.backups),
        )
        self.assertEqual((code, error["error"]), (2, "PLAN_DIGEST_MISMATCH"))

        abort_root = Path(self.temporary.name) / "cli-abort"
        abort_backup = Path(self.temporary.name) / "cli-abort-backup"
        server.initialize(abort_root)
        abort_preview = reset.preview(abort_root)
        abort_common = ("--data-root", str(abort_root))
        abort_mutation = (
            "--operation-id", "reset-cli-abort-0001",
            "--plan-digest", str(abort_preview["plan_digest"]),
        )
        self.assertEqual(invoke(
            "prepare", *abort_common, *abort_mutation, "--backup-root", str(abort_backup),
        )[1]["state"], "AUTHORIZED")
        self.assertEqual(invoke("abort", *abort_common, *abort_mutation)[1]["state"], "ABORTED")


if __name__ == "__main__":
    unittest.main()
