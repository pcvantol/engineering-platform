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
from engineering_platform import development_profile, server, submission_service
from engineering_platform.operational_installation_lock import OperationalInstallationLock
from engineering_platform.storage import sqlite_connection


class SimulatedPostCommitFinishCrash(BaseException):
    """Model process death after durable COMPLETED and before active-root thaw."""


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

    def _bind_update_candidate_fixture(self, operation: Path) -> None:
        """Write the exact token-free identities that own one candidate venv."""
        operation_id = operation.name
        installation_id = str(json.loads(
            (self.root / "runtime-identity.json").read_text(encoding="utf-8")
        )["instance_id"])
        staged = operation / "download" / "engineering_platform-2.3.82-py3-none-any.whl"
        candidate = operation / "candidate-venv"
        plan = {
            "operation_id": operation_id,
            "installation_id": installation_id,
            "data_root": str(self.root),
            "current_version": "2.3.81",
            "current_digest": "sha256:" + "a" * 64,
            "target_version": "2.3.82",
            "target_digest": "sha256:" + "b" * 64,
            "target_source_revision": "c" * 40,
            "artifact": str(staged),
            "cleanup_targets": [
                str(operation / name) for name in ("build", "download", "pip-cache")
            ],
            "steps": [
                "INSTALLATION_LOCK", "INVENTORY_AND_COMPATIBILITY", "EXACT_ARTIFACT",
                "QUIESCE", "BACKUP_AND_MIGRATION", "ACTIVATE", "VERIFY", "CLEANUP",
            ],
        }
        prepared = {
            "operation_id": operation_id,
            "installation_id": installation_id,
            "operation_root": str(operation),
            "staged_artifact": str(staged),
            "artifact_digest": plan["target_digest"],
            "candidate_venv": str(candidate),
            "interpreter": str(candidate / "bin" / "python"),
            "pip_cache": str(operation / "pip-cache"),
            "package": {
                "interpreter": str(candidate / "bin" / "python"),
                "version": "2.3.82",
                "metadata": str(candidate / "lib" / "python3.14" / "site-packages" / "engineering_platform-2.3.82.dist-info"),
                "package": str(candidate / "lib" / "python3.14" / "site-packages" / "engineering_platform"),
            },
        }
        canonical_plan = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        canonical_prepared = json.dumps(
            prepared, sort_keys=True, separators=(",", ":"),
        ).encode()
        (operation / "operation.json").write_text(json.dumps({
            "schema_version": 2,
            "operation_id": operation_id,
            "plan": plan,
            "plan_digest": "sha256:" + hashlib.sha256(canonical_plan).hexdigest(),
            "state": "PREPARED",
            "events": [{"state": "PREPARED", "evidence": {}}],
            "prepared_candidate": prepared,
            "prepared_candidate_digest": "sha256:" + hashlib.sha256(canonical_prepared).hexdigest(),
        }), encoding="utf-8")
        (operation / "candidate-runtime.json").write_text(json.dumps({
            "schema_version": 1,
            "operation_id": operation_id,
            "installation_id": installation_id,
            "target_version": plan["target_version"],
            "target_digest": plan["target_digest"],
            "target_source_revision": plan["target_source_revision"],
            "staged_artifact": str(staged),
            "candidate_venv": str(candidate),
        }), encoding="utf-8")

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
            connection.execute("DELETE FROM engineering_schema_migrations WHERE version>=68")
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

    def test_development_directories_require_valid_profile_and_unknown_paths_still_block(self) -> None:
        for name in (development_profile.CACHE_DIRECTORY, development_profile.LOG_DIRECTORY):
            (self.root / name).mkdir()
        self.assertEqual(
            set(reset.preview(self.root)["unknown_external_paths"]),
            {development_profile.CACHE_DIRECTORY, development_profile.LOG_DIRECTORY},
        )

        development_root = Path(self.temporary.name) / "isolated-development"
        venv = Path(self.temporary.name) / "fixture-venv"
        interpreter = venv / "bin" / "python3.14"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("fixture interpreter identity\n", encoding="utf-8")
        (venv / "pyvenv.cfg").write_text("fixture venv identity\n", encoding="utf-8")
        development_profile.establish(
            data_root=development_root, bind_port=18877, development_venv=venv,
            credential_reference="development:reset-fixture", interpreter=interpreter,
            operational_data_roots=(), environment={},
        )
        server.initialize(development_root)
        plan = reset.preview(development_root)
        self.assertNotIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", plan["blocking_codes"])
        self.assertEqual(plan["unknown_external_paths"], [])
        preserved = {item["path"]: item for item in plan["runtime_control_external"]}
        for name in (development_profile.CACHE_DIRECTORY, development_profile.LOG_DIRECTORY):
            self.assertEqual(preserved[name]["effect"], "PRESERVE")
            self.assertEqual(preserved[name]["classification"], "SYSTEM_RUNTIME_CONTROL")
        (development_root / "unclassified.txt").write_text("must block", encoding="utf-8")
        self.assertIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", reset.preview(development_root)["blocking_codes"])
        (development_root / "unclassified.txt").unlink()
        cache = development_root / development_profile.CACHE_DIRECTORY
        cache.rmdir()
        cache.write_text("not a directory", encoding="utf-8")
        self.assertIn("DEVELOPMENT_RUNTIME_PATH_INVALID", reset.preview(development_root)["blocking_codes"])
        cache.unlink()
        cache.mkdir()
        logs = development_root / development_profile.LOG_DIRECTORY
        logs.rmdir()
        logs.symlink_to(venv, target_is_directory=True)
        self.assertIn("DEVELOPMENT_PROFILE_INVALID", reset.preview(development_root)["blocking_codes"])
        logs.unlink()
        logs.mkdir()
        marker = development_root / development_profile.FILENAME
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["cache_directory"] = str(development_root / "wrong-cache")
        marker.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIn("DEVELOPMENT_PROFILE_INVALID", reset.preview(development_root)["blocking_codes"])

    def test_complete_reset_preserves_bindings_credentials_and_configuration(self) -> None:
        request = self._populate()
        with sqlite_connection(self.root / "epdata.sqlite") as connection:
            retired_submission_id = str(connection.execute(
                "SELECT submission_id FROM ep_submissions"
            ).fetchone()[0])
            connection.execute(
                "INSERT INTO ep_merge_delegations "
                "(delegation_id,actor_reference,project_id,repository_id,github_repository,mission_id,"
                "mission_revision,base_branch,roles,expires_at,created_at,activated_at,revoked_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("a" * 32, "local-uid:501", "djconnect", "djconnect", "pcvantol/djconnect",
                 "mission-1", "1", "main", '["IMPLEMENTATION"]', "2099-01-01T00:00:00+00:00",
                 "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", None),
            )
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
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_merge_delegations").fetchone(), (1,))
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

    def test_prepared_operation_has_explicit_read_only_revalidation(self) -> None:
        self._populate()
        initial = reset.preview(self.root)
        operation_id, digest = self._prepared(operation_id="reset-revalidate-0001")
        before = reset.status(self.root, operation_id=operation_id)

        generic = reset.preview(self.root)
        self.assertEqual(generic["plan_digest"], initial["plan_digest"])
        self.assertEqual(generic["blocking_codes"], ["MAINTENANCE_ALREADY_ACTIVE"])
        self.assertEqual(
            generic["active_maintenance"],
            [{"operation_id": operation_id, "state": "AUTHORIZED"}],
        )
        with self.assertRaisesRegex(reset.OperationalResetError, "PLAN_BLOCKED"):
            reset.prepare(
                self.root, operation_id="reset-revalidate-foreign-0001",
                plan_digest=digest, backup_root=self.backups,
            )

        revalidated = reset.revalidate(
            self.root, operation_id=operation_id, plan_digest=digest,
        )

        self.assertEqual(revalidated["plan_digest"], digest)
        self.assertEqual(revalidated["revalidation"]["writer_fence_owner"], operation_id)
        self.assertRegex(
            str(revalidated["revalidation"]["revalidation_digest"]),
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(before, reset.status(self.root, operation_id=operation_id))

    def test_revalidation_rejects_same_count_security_drift_and_backup_tampering(self) -> None:
        self._populate()
        operation_id, digest = self._prepared(operation_id="reset-revalidate-drift-0001")
        database = self.root / "epdata.sqlite"
        with reset._central_connection(database) as connection:
            triggers = [
                (str(name), str(sql)) for name, sql in connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='ep_consumer_registrations'"
                ) if sql is not None
            ]
            for name, _sql in triggers:
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute(
                "UPDATE ep_consumer_registrations SET status='REVOKED' WHERE consumer_id='consumer-a'"
            )
            for _name, sql in triggers:
                connection.execute(sql)
        with self.assertRaisesRegex(reset.OperationalResetError, "REVALIDATION_CHANGED"):
            reset.revalidate(self.root, operation_id=operation_id, plan_digest=digest)

        backup_root = Path(self.temporary.name) / "backup-tamper-data"
        backup_destination = Path(self.temporary.name) / "backup-tamper-recovery"
        server.initialize(backup_root)
        plan = reset.preview(backup_root)
        reset.prepare(
            backup_root, operation_id="reset-revalidate-backup-0001",
            plan_digest=str(plan["plan_digest"]), backup_root=backup_destination,
        )
        status = reset.status(
            backup_root, operation_id="reset-revalidate-backup-0001",
        )["operation"]
        manifest = Path(str(status["backup_path"])) / "manifest.json"
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_payload["profile"] = "tampered"
        manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
        with self.assertRaisesRegex(reset.OperationalResetError, "BACKUP_MANIFEST_DIGEST_MISMATCH"):
            reset.revalidate(
                backup_root, operation_id="reset-revalidate-backup-0001",
                plan_digest=str(plan["plan_digest"]),
            )

    def test_apply_rechecks_source_at_the_database_mutation_boundary(self) -> None:
        self._populate()
        operation_id, digest = self._prepared(operation_id="reset-apply-recheck-0001")
        reset.revalidate(self.root, operation_id=operation_id, plan_digest=digest)
        database = self.root / "epdata.sqlite"
        with reset._central_connection(database) as connection:
            triggers = [
                (str(name), str(sql)) for name, sql in connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='ep_project_registrations'"
                ) if sql is not None
            ]
            for name, _sql in triggers:
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute(
                "UPDATE ep_project_registrations SET attachment_contract='{\"changed\":true}' "
                "WHERE project_id='project-a'"
            )
            for _name, sql in triggers:
                connection.execute(sql)
        with self.assertRaisesRegex(reset.OperationalResetError, "REVALIDATION_CHANGED"):
            reset.apply(self.root, operation_id=operation_id, plan_digest=digest)

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
            connection.execute("DELETE FROM engineering_schema_migrations WHERE version>=68")
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

    def test_finish_atomically_isolates_file_arriving_after_last_empty_scan(self) -> None:
        self._populate()
        operation_id, digest = self._prepared(operation_id="reset-finish-race-0001")
        reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)
        original_verify = reset._verify_under_lock
        injected = False

        def inject_after_empty_scan(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal injected
            result = original_verify(*args, **kwargs)
            if not injected:
                injected = True
                delayed = self.root / "file-inbox" / "delayed-old-event.json"
                delayed.write_text('{"event":"old"}', encoding="utf-8")
            return result

        with patch.object(reset, "_verify_under_lock", side_effect=inject_after_empty_scan):
            completed = reset.finish(
                self.root, operation_id=operation_id, plan_digest=digest,
            )

        self.assertEqual("COMPLETED", completed["state"])
        self.assertFalse(reset.maintenance_active(self.root))
        self.assertFalse((self.root / "file-inbox" / "delayed-old-event.json").exists())
        boundary = Path(str(completed["finish_boundary_path"]))
        isolated = boundary / "file-inbox" / "delayed-old-event.json"
        self.assertEqual('{"event":"old"}', isolated.read_text(encoding="utf-8"))
        self.assertRegex(str(completed["finish_boundary_digest"]), r"^sha256:[0-9a-f]{64}$")
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)

    def test_finish_boundary_rotation_resumes_same_operation_after_partial_crash(self) -> None:
        operation_id, digest = self._prepared(operation_id="reset-finish-crash-0001")
        reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)
        real_rename = os.rename
        rotated = 0

        def crash_during_second_route(source: object, target: object, *args: object,
                                      **kwargs: object) -> object:
            nonlocal rotated
            source_path, target_path = Path(source), Path(target)
            if "finish-boundary" in target_path.parts and source_path.is_dir():
                rotated += 1
                if rotated == 2:
                    raise OSError("synthetic process loss during route rotation")
            return real_rename(source, target, *args, **kwargs)

        with patch.object(reset.os, "rename", side_effect=crash_during_second_route):
            with self.assertRaisesRegex(
                reset.OperationalResetError, "FINISH_BOUNDARY_ROTATION_FAILED",
            ):
                reset.finish(self.root, operation_id=operation_id, plan_digest=digest)

        interrupted = reset.status(self.root, operation_id=operation_id)["operation"]
        self.assertEqual("VERIFIED", interrupted["state"])
        self.assertIsNotNone(interrupted["finish_boundary_path"])
        self.assertTrue(reset.maintenance_active(self.root))
        completed = reset.finish(
            self.root, operation_id=operation_id, plan_digest=digest,
        )
        self.assertEqual("COMPLETED", completed["state"])
        self.assertFalse(reset.maintenance_active(self.root))
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)

    def test_finish_freezes_new_roots_and_rejects_post_rotation_active_file(self) -> None:
        operation_id, digest = self._prepared(operation_id="reset-finish-frozen-0001")
        reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)
        real_rotate = reset._rotate_finish_boundary

        def inject_after_real_rotation(*args: object, **kwargs: object) -> dict[str, object]:
            proof = real_rotate(*args, **kwargs)
            inbox = self.root / "file-inbox"
            self.assertEqual(0o500, inbox.stat().st_mode & 0o777)
            # Simulate an installation-owner writer bypassing the ordinary
            # permission failure. The post-rotation proof must still prevent
            # COMPLETED rather than accepting this active old event.
            inbox.chmod(0o700)
            (inbox / "late-after-rotation.json").write_text("{}", encoding="utf-8")
            return proof

        with patch.object(
            reset, "_rotate_finish_boundary", side_effect=inject_after_real_rotation,
        ):
            with self.assertRaisesRegex(
                reset.OperationalResetError, "ACTIVE_ROOT_NOT_FROZEN",
            ):
                reset.finish(self.root, operation_id=operation_id, plan_digest=digest)

        operation = reset.status(self.root, operation_id=operation_id)["operation"]
        self.assertEqual("VERIFIED", operation["state"])
        self.assertTrue(reset.maintenance_active(self.root))
        self.assertTrue((self.root / "file-inbox" / "late-after-rotation.json").is_file())

    def test_finish_roots_are_frozen_until_commit_then_thawed_for_new_generation(self) -> None:
        operation_id, digest = self._prepared(operation_id="reset-finish-modes-0001")
        reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)
        observed_during_commit: dict[str, int] = {}
        real_transition = reset._transition

        def inspect_completed_transition(
            connection: sqlite3.Connection, selected: str, before: str, after: str,
            assignments: str = "", parameters: tuple[object, ...] = (),
        ) -> None:
            if before == "VERIFIED" and after == "COMPLETED":
                observed_during_commit.update(reset._active_roots_state(self.root))
            real_transition(
                connection, selected, before, after, assignments, parameters,
            )

        with patch.object(reset, "_transition", side_effect=inspect_completed_transition):
            completed = reset.finish(
                self.root, operation_id=operation_id, plan_digest=digest,
            )

        self.assertEqual("COMPLETED", completed["state"])
        self.assertEqual({name: 0o500 for name in reset._EFFECT_DIRECTORIES},
                         observed_during_commit)
        self.assertEqual({name: 0o700 for name in reset._EFFECT_DIRECTORIES},
                         reset._active_roots_state(self.root))
        late = self.root / "file-inbox" / "new-generation.json"
        late.write_text("{}", encoding="utf-8")
        self.assertEqual(
            "COMPLETED",
            reset.finish(self.root, operation_id=operation_id, plan_digest=digest)["state"],
        )
        self.assertTrue(late.is_file())
        self.assertEqual(
            "COMPLETED",
            reset.verify(self.root, operation_id=operation_id, plan_digest=digest)["state"],
        )

    def test_post_commit_pre_thaw_crash_resumes_same_completed_operation(self) -> None:
        operation_id, digest = self._prepared(operation_id="reset-finish-thaw-crash-0001")
        reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)

        with patch.object(
            reset, "_thaw_active_roots", side_effect=SimulatedPostCommitFinishCrash(),
        ):
            with self.assertRaises(SimulatedPostCommitFinishCrash):
                reset.finish(self.root, operation_id=operation_id, plan_digest=digest)

        operation = reset.status(self.root, operation_id=operation_id)["operation"]
        self.assertEqual("COMPLETED", operation["state"])
        self.assertFalse(reset.maintenance_active(self.root))
        self.assertEqual({name: 0o500 for name in reset._EFFECT_DIRECTORIES},
                         reset._active_roots_state(self.root))
        with self.assertRaises(PermissionError):
            (self.root / "file-inbox" / "blocked.json").write_text("{}", encoding="utf-8")

        resumed = reset.resume(
            self.root, operation_id=operation_id, plan_digest=digest,
        )
        self.assertEqual("COMPLETED", resumed["operation"]["state"])
        self.assertEqual({name: 0o700 for name in reset._EFFECT_DIRECTORIES},
                         reset._active_roots_state(self.root))

    def test_completed_finish_refuses_active_root_symlink_without_touching_target(self) -> None:
        operation_id, digest = self._prepared(operation_id="reset-finish-symlink-0001")
        reset.apply(self.root, operation_id=operation_id, plan_digest=digest)
        reset.verify(self.root, operation_id=operation_id, plan_digest=digest)
        with patch.object(
            reset, "_thaw_active_roots", side_effect=SimulatedPostCommitFinishCrash(),
        ):
            with self.assertRaises(SimulatedPostCommitFinishCrash):
                reset.finish(self.root, operation_id=operation_id, plan_digest=digest)

        inbox = self.root / "file-inbox"
        inbox.rmdir()
        outside = Path(self.temporary.name) / "outside-inbox"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        inbox.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(reset.OperationalResetError, "ACTIVE_ROOT_UNSAFE"):
            reset.finish(self.root, operation_id=operation_id, plan_digest=digest)
        self.assertEqual("preserve", sentinel.read_text(encoding="utf-8"))

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

    def test_updater_candidate_venv_is_an_opaque_preserved_runtime_boundary(self) -> None:
        operation = self.root / "operations" / "update-live-preview-0001"
        operation.mkdir(parents=True)
        venv = operation / "candidate-venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "lib" / "python3.14" / "site-packages").mkdir(parents=True)
        (venv / "bin" / "python").symlink_to("python3.14")
        (venv / "bin" / "python3.14").symlink_to("/usr/bin/python3")
        (venv / "lib" / "python3.14" / "site-packages" / "installed.py").write_text(
            "# updater-owned fixture\n", encoding="utf-8",
        )
        self._bind_update_candidate_fixture(operation)

        plan = reset.preview(self.root)

        self.assertNotIn("EXTERNAL_SYMLINK_UNSAFE", plan["blocking_codes"])
        self.assertNotIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", plan["blocking_codes"])
        boundary = next(
            item for item in plan["preserved_external"]
            if item["path"] == "operations/update-live-preview-0001/candidate-venv"
        )
        self.assertEqual("INSTALLATION_RUNTIME", boundary["classification"])
        self.assertEqual("directory", boundary["kind"])
        self.assertTrue(boundary["opaque_boundary"])
        self.assertFalse(boundary["symlinks_followed"])
        self.assertFalse(any(
            str(item["path"]).startswith(
                "operations/update-live-preview-0001/candidate-venv/"
            ) for item in plan["preserved_external"]
        ))

        # A sibling staging symlink is not covered by the opaque venv contract.
        outside = Path(self.temporary.name) / "outside-runtime"
        outside.mkdir()
        (operation / "build").mkdir()
        unsafe = operation / "build" / "unsafe-link"
        unsafe.symlink_to(outside, target_is_directory=True)
        self.assertIn("EXTERNAL_SYMLINK_UNSAFE", reset.preview(self.root)["blocking_codes"])
        unsafe.unlink()

        # A candidate-venv is opaque only below an exact journalled operation;
        # the same symlink shape at an unowned path remains fail-closed.
        unowned = self.root / "operations" / "update-no-journal-0002" / "candidate-venv"
        unowned.mkdir(parents=True)
        (unowned / "python").symlink_to("/usr/bin/python3")
        self.assertIn("EXTERNAL_SYMLINK_UNSAFE", reset.preview(self.root)["blocking_codes"])
        (unowned / "python").unlink()
        unowned.rmdir()
        unowned.parent.rmdir()

        linked_operation = self.root / "operations" / "update-venv-link-0003"
        linked_operation.mkdir()
        self._bind_update_candidate_fixture(linked_operation)
        linked_venv = linked_operation / "candidate-venv"
        linked_venv.symlink_to(outside, target_is_directory=True)
        self.assertIn("EXTERNAL_SYMLINK_UNSAFE", reset.preview(self.root)["blocking_codes"])
        linked_venv.unlink()
        (linked_operation / "candidate-runtime.json").unlink()
        (linked_operation / "operation.json").unlink()
        linked_operation.rmdir()

        # A malformed journal or a candidate marker bound to another path
        # cannot hide regular unknown content behind a venv-shaped directory.
        malformed = self.root / "operations" / "update-malformed-0004"
        malformed_venv = malformed / "candidate-venv"
        malformed_venv.mkdir(parents=True)
        (malformed_venv / "unknown.bin").write_bytes(b"keep")
        (malformed / "operation.json").write_text("{}", encoding="utf-8")
        (malformed / "candidate-runtime.json").write_text("{}", encoding="utf-8")
        malformed_plan = reset.preview(self.root)
        self.assertIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", malformed_plan["blocking_codes"])
        self.assertIn(
            "operations/update-malformed-0004/candidate-venv/unknown.bin",
            malformed_plan["unknown_external_paths"],
        )
        (malformed_venv / "unknown.bin").unlink()
        malformed_venv.rmdir()
        (malformed / "candidate-runtime.json").unlink()
        (malformed / "operation.json").unlink()
        malformed.rmdir()

        unbound = self.root / "operations" / "update-unbound-0005"
        unbound_venv = unbound / "candidate-venv"
        unbound_venv.mkdir(parents=True)
        (unbound_venv / "unknown.bin").write_bytes(b"keep")
        self._bind_update_candidate_fixture(unbound)
        marker_path = unbound / "candidate-runtime.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["candidate_venv"] = str(unbound / "some-other-venv")
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        unbound_plan = reset.preview(self.root)
        self.assertIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", unbound_plan["blocking_codes"])
        self.assertIn(
            "operations/update-unbound-0005/candidate-venv/unknown.bin",
            unbound_plan["unknown_external_paths"],
        )

        unknown = operation / "operator-notes.txt"
        unknown.write_text("preserve", encoding="utf-8")
        blocked = reset.preview(self.root)
        self.assertIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", blocked["blocking_codes"])
        self.assertIn(
            "operations/update-live-preview-0001/operator-notes.txt",
            blocked["unknown_external_paths"],
        )
        self.assertEqual("preserve", unknown.read_text(encoding="utf-8"))

    def test_marker_bound_pre_journal_updater_staging_is_opaque_but_incomplete(self) -> None:
        operation = self.root / "operations" / "ep-update-2358-release-20260915-001"
        operation.mkdir(parents=True)
        candidate = operation / "candidate-venv"
        (candidate / "bin").mkdir(parents=True)
        (candidate / "bin" / "python").symlink_to("python3.14")
        (candidate / "bin" / "python3.14").symlink_to("/usr/bin/python3")
        self._bind_update_candidate_fixture(operation)
        marker = json.loads((operation / "candidate-runtime.json").read_text(encoding="utf-8"))
        staged = Path(str(marker["staged_artifact"]))
        staged.parent.mkdir()
        staged.write_bytes(b"synthetic wheel")
        marker["target_digest"] = "sha256:" + hashlib.sha256(staged.read_bytes()).hexdigest()
        (operation / "candidate-runtime.json").write_text(
            json.dumps(marker), encoding="utf-8",
        )
        pip_cache = operation / "pip-cache"
        pip_cache.mkdir()
        # Exact updater-owned subtrees are opaque in this explicit crash
        # window, so their internal links are neither followed nor reset.
        (pip_cache / "cached-link").symlink_to(Path(self.temporary.name) / "outside-cache")
        (operation / "operation.json").unlink()

        plan = reset.preview(self.root)

        self.assertNotIn("EXTERNAL_SYMLINK_UNSAFE", plan["blocking_codes"])
        self.assertNotIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", plan["blocking_codes"])
        preserved = {str(item["path"]): item for item in plan["preserved_external"]}
        marker_path = f"operations/{operation.name}/candidate-runtime.json"
        self.assertEqual(
            preserved[marker_path]["classification"],
            "INSTALLATION_RUNTIME_STAGING_UNBOUND",
        )
        for name in ("candidate-venv", "download", "pip-cache"):
            boundary = preserved[f"operations/{operation.name}/{name}"]
            self.assertEqual(
                boundary["classification"], "INSTALLATION_RUNTIME_STAGING_UNBOUND",
            )
            self.assertTrue(boundary["opaque_boundary"])
            self.assertFalse(boundary["symlinks_followed"])
            self.assertEqual(
                boundary["installation_update_state"],
                "INCOMPLETE_NO_OPERATION_JOURNAL",
            )
        self.assertFalse(any(
            path.startswith(f"operations/{operation.name}/candidate-venv/")
            or path.startswith(f"operations/{operation.name}/download/")
            or path.startswith(f"operations/{operation.name}/pip-cache/")
            for path in preserved
        ))

        sibling = operation / "operator-notes.txt"
        sibling.write_text("preserve", encoding="utf-8")
        blocked = reset.preview(self.root)
        self.assertIn("EXTERNAL_CLASSIFICATION_INCOMPLETE", blocked["blocking_codes"])
        self.assertIn(
            f"operations/{operation.name}/operator-notes.txt",
            blocked["unknown_external_paths"],
        )
        self.assertEqual(sibling.read_text(encoding="utf-8"), "preserve")

    def test_unbound_staging_marker_mismatch_missing_and_linked_root_fail_closed(self) -> None:
        outside = Path(self.temporary.name) / "outside-unbound"
        outside.mkdir()
        for suffix, mutation in (
            ("mismatch", "MISMATCH"),
            ("malformed", "MALFORMED"),
            ("missing", "MISSING"),
            ("identity", "WRONG_INSTALLATION"),
            ("digest", "DIGEST_MISMATCH"),
            ("wheel", "WHEEL_IDENTITY"),
            ("linked", "LINKED_ROOT"),
        ):
            operation = self.root / "operations" / f"ep-update-unbound-{suffix}-0001"
            operation.mkdir(parents=True)
            self._bind_update_candidate_fixture(operation)
            marker_path = operation / "candidate-runtime.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            staged = Path(str(marker["staged_artifact"]))
            staged.parent.mkdir()
            staged.write_bytes(b"synthetic wheel")
            marker["target_digest"] = "sha256:" + hashlib.sha256(staged.read_bytes()).hexdigest()
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            (operation / "operation.json").unlink()
            candidate = operation / "candidate-venv"
            if mutation == "LINKED_ROOT":
                candidate.symlink_to(outside, target_is_directory=True)
            else:
                candidate.mkdir()
                (candidate / "python").symlink_to("/usr/bin/python3")
                if mutation == "MISMATCH":
                    marker["candidate_venv"] = str(operation / "different-venv")
                    marker_path.write_text(json.dumps(marker), encoding="utf-8")
                elif mutation == "MALFORMED":
                    marker_path.write_text("{}", encoding="utf-8")
                elif mutation == "WRONG_INSTALLATION":
                    marker["installation_id"] = "forged-installation-id"
                    marker_path.write_text(json.dumps(marker), encoding="utf-8")
                elif mutation == "DIGEST_MISMATCH":
                    marker["target_digest"] = "sha256:" + "d" * 64
                    marker_path.write_text(json.dumps(marker), encoding="utf-8")
                elif mutation == "WHEEL_IDENTITY":
                    foreign = staged.with_name("foreign-candidate.whl")
                    staged.rename(foreign)
                    staged = foreign
                    marker["staged_artifact"] = str(staged)
                    marker_path.write_text(json.dumps(marker), encoding="utf-8")
                else:
                    marker_path.unlink()

            blocked = reset.preview(self.root)
            self.assertIn("EXTERNAL_SYMLINK_UNSAFE", blocked["blocking_codes"])

            if candidate.is_symlink():
                candidate.unlink()
            else:
                (candidate / "python").unlink()
                candidate.rmdir()
            staged.unlink()
            staged.parent.rmdir()
            marker_path.unlink(missing_ok=True)
            operation.rmdir()

    def test_journalled_candidate_rejects_forged_identity_cleanup_and_steps(self) -> None:
        operation = self.root / "operations" / "update-adversarial-0001"
        operation.mkdir(parents=True)
        candidate = operation / "candidate-venv"
        (candidate / "bin").mkdir(parents=True)
        (candidate / "bin" / "python").symlink_to("/usr/bin/python3")
        self._bind_update_candidate_fixture(operation)
        journal_path = operation / "operation.json"
        marker_path = operation / "candidate-runtime.json"
        original_journal = journal_path.read_text(encoding="utf-8")
        original_marker = marker_path.read_text(encoding="utf-8")

        def persist(journal: dict[str, object], marker: dict[str, object]) -> None:
            plan = journal["plan"]
            prepared = journal["prepared_candidate"]
            assert isinstance(plan, dict) and isinstance(prepared, dict)
            journal["plan_digest"] = "sha256:" + hashlib.sha256(json.dumps(
                plan, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            journal["prepared_candidate_digest"] = "sha256:" + hashlib.sha256(json.dumps(
                prepared, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            journal_path.write_text(json.dumps(journal), encoding="utf-8")
            marker_path.write_text(json.dumps(marker), encoding="utf-8")

        for case in ("WRONG_INSTALLATION", "EXTERNAL_CLEANUP", "UNKNOWN_STEP"):
            journal = json.loads(original_journal)
            marker = json.loads(original_marker)
            plan = journal["plan"]
            prepared = journal["prepared_candidate"]
            assert isinstance(plan, dict) and isinstance(prepared, dict)
            if case == "WRONG_INSTALLATION":
                plan["installation_id"] = "forged-installation-id"
                prepared["installation_id"] = "forged-installation-id"
                marker["installation_id"] = "forged-installation-id"
            elif case == "EXTERNAL_CLEANUP":
                plan["cleanup_targets"] = [
                    str(Path(self.temporary.name) / "outside-cleanup"),
                    str(operation / "download"),
                    str(operation / "pip-cache"),
                ]
            else:
                plan["steps"] = [*plan["steps"], "UNKNOWN_SIDE_EFFECT"]
            persist(journal, marker)
            with self.subTest(case=case):
                blocked = reset.preview(self.root)
                self.assertIn("EXTERNAL_SYMLINK_UNSAFE", blocked["blocking_codes"])

        journal_path.write_text(original_journal, encoding="utf-8")
        marker_path.write_text(original_marker, encoding="utf-8")
        allowed = reset.preview(self.root)
        self.assertNotIn("EXTERNAL_SYMLINK_UNSAFE", allowed["blocking_codes"])

    def test_empty_database_identity_and_empty_updater_bindings_never_become_opaque(self) -> None:
        runtime_identity = json.loads(
            (self.root / "runtime-identity.json").read_text(encoding="utf-8")
        )["instance_id"]
        self.assertRegex(
            runtime_identity,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        operation = self.root / "operations" / "update-empty-identity-0001"
        operation.mkdir(parents=True)
        candidate = operation / "candidate-venv"
        candidate.mkdir()
        unknown = candidate / "unknown-application-data.bin"
        unknown.write_bytes(b"preserve")
        self._bind_update_candidate_fixture(operation)

        journal_path = operation / "operation.json"
        marker_path = operation / "candidate-runtime.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        plan = journal["plan"]
        prepared = journal["prepared_candidate"]
        assert isinstance(plan, dict) and isinstance(prepared, dict)
        plan["installation_id"] = ""
        prepared["installation_id"] = ""
        marker["installation_id"] = ""
        journal["plan_digest"] = "sha256:" + hashlib.sha256(json.dumps(
            plan, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        journal["prepared_candidate_digest"] = "sha256:" + hashlib.sha256(json.dumps(
            prepared, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        with reset._central_connection(self.root / "epdata.sqlite") as connection:
            connection.execute("UPDATE ep_installations SET instance_id=''")
            connection.execute(
                "UPDATE engineering_metadata SET value='' "
                "WHERE key='installation.instance_id'"
            )

        with self.assertRaisesRegex(reset.OperationalResetError, "TARGET_IDENTITY_CONFLICT"):
            reset.preview(self.root)
        with self.assertRaisesRegex(reset.OperationalResetError, "TARGET_IDENTITY_CONFLICT"):
            reset._external_inventory(self.root, expected_installation_id="")
        self.assertIsNone(reset._opaque_update_directory_classification(
            "operations", candidate, f"{operation.name}/candidate-venv",
            expected_installation_id="",
        ))
        self.assertEqual(unknown.read_bytes(), b"preserve")

    def test_opaque_directory_swap_before_receipt_fails_closed(self) -> None:
        operation = self.root / "operations" / "update-swap-race-0001"
        operation.mkdir(parents=True)
        candidate = operation / "candidate-venv"
        (candidate / "bin").mkdir(parents=True)
        (candidate / "bin" / "python").symlink_to("/usr/bin/python3")
        self._bind_update_candidate_fixture(operation)
        displaced = operation / "candidate-venv-displaced"
        outside = Path(self.temporary.name) / "outside-swap-target"
        outside.mkdir()
        original_recheck = reset._recheck_opaque_directory
        swapped = False

        def swap_then_recheck(
            path: Path, *, device: int, inode: int, descriptor: int,
        ) -> None:
            nonlocal swapped
            if path == candidate and not swapped:
                candidate.rename(displaced)
                candidate.symlink_to(outside, target_is_directory=True)
                swapped = True
            original_recheck(
                path, device=device, inode=inode, descriptor=descriptor,
            )

        with patch.object(
            reset, "_recheck_opaque_directory", side_effect=swap_then_recheck,
        ):
            blocked = reset.preview(self.root)
        self.assertTrue(swapped)
        self.assertIn("EXTERNAL_SYMLINK_UNSAFE", blocked["blocking_codes"])
        self.assertFalse(any(
            item.get("path") == "operations/update-swap-race-0001/candidate-venv"
            for item in blocked["preserved_external"]
        ))

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
            self.root, command="preview", operation_id=None,
        )
        self.assertEqual(contract["contract_version"], "operational-reset-v1")
        self.assertEqual(contract["product"], "engineering-platform")
        self.assertEqual(contract["target"]["instance_id"], plan["target"]["instance_id"])
        rendered = json.dumps(contract)
        self.assertNotIn((b"v" * 32).hex(), rendered)
        self.assertNotIn((b"f" * 32).hex(), rendered)
        self.assertEqual(
            contract["details"],
            {
                "credentials_included_in_receipt": False,
                "projection": "PERSISTED_PUBLIC_MAINTENANCE_STATE",
            },
        )

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
        code, revalidated = invoke("revalidate", *common, *mutation)
        self.assertEqual((code, revalidated["state"], revalidated["allowed"]), (0, "AUTHORIZED", True))
        self.assertEqual(revalidated["details"]["revalidation"]["writer_fence_owner"], operation_id)
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
