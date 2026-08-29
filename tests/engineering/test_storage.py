"""Regression coverage for the versioned Engineering SQLite schema."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.storage import (
    DATABASE_FILENAME,
    ENGINEERING_STORAGE_SCHEMA_VERSION,
    EngineeringStorageError,
    WORKSPACE_DIRECTORY,
    activate_storage_schema,
    database_path,
    ai_capacity_history,
    load_projection,
    load_execution_context_snapshot,
    load_forge_governance_handoff_snapshot,
    open_storage,
    record_ai_capacity_bi_hourly,
    record_artifact,
    record_submission,
    record_admission_decision,
    load_admission_decision,
    record_readiness_evaluation,
    load_readiness_evaluation,
    regenerate_status_projections,
    store_projection,
    verify_artifact_integrity,
)
from tools.engineering.agent_state import StateStore, TransactionState
from tools.engineering.platform_version import EngineeringPlatformManifest


class EngineeringStorageTest(unittest.TestCase):
    def test_platform_manifest_tracks_the_current_storage_schema(self) -> None:
        root = Path(__file__).parents[2]
        manifest = EngineeringPlatformManifest.load(
            root / "tools" / "engineering" / "ENGINEERING_PLATFORM_VERSION.json"
        )
        self.assertEqual(manifest.storage_schema, ENGINEERING_STORAGE_SCHEMA_VERSION)

    def test_provider_free_admission_decision_is_immutable_and_renderable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_submission(
                root, submission_id="submission-admission", producer_id="test", producer_type="HUMAN",
                prompt_content="bounded", prompt_metadata={}, target_identity={}, original_envelope={},
                received_at="2026-08-27T00:00:00+00:00", link_run_id="inbox-admission",
            )
            record_admission_decision(
                root, run_id="inbox-admission", submission_id="submission-admission", execution_mode="MANAGED",
                decision="FAIL", failed_gate_ids=("worktree_untracked",),
                evidence=({"gate_id": "worktree_untracked", "expected": "PASS", "observed": "FAIL", "verified_at": "2026-08-27T00:00:00+00:00"},),
                observed_at="2026-08-27T00:00:00+00:00",
            )
            decision = load_admission_decision(root, "inbox-admission")
            self.assertEqual(decision["decision"], "FAIL")
            self.assertEqual(decision["failed_gate_ids"], ["worktree_untracked"])
            connection = open_storage(root)
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE execution_admission_decisions SET decision='PASS'")
            connection.close()

    def test_ai_capacity_history_keeps_one_lowest_measurement_per_two_hour_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_bucket = datetime(2026, 8, 25, 10, 5, tzinfo=timezone.utc)
            record_ai_capacity_bi_hourly(
                root, provider="Codex CLI", remaining_percent=86, observed_at=first_bucket
            )
            record_ai_capacity_bi_hourly(
                root, provider="Codex CLI", remaining_percent=72, observed_at=first_bucket.replace(hour=11, minute=55)
            )
            record_ai_capacity_bi_hourly(
                root, provider="Codex CLI", remaining_percent=91, observed_at=first_bucket.replace(hour=12)
            )
            with patch("tools.engineering.storage.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 8, 25, 12, 15, tzinfo=timezone.utc)
                mocked_datetime.fromtimestamp.side_effect = datetime.fromtimestamp
                mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
                history = ai_capacity_history(root, provider="Codex CLI")
            self.assertEqual(history, [
                {"at": "2026-08-25T10:00:00+00:00", "remaining_percent": 72.0},
                {"at": "2026-08-25T12:00:00+00:00", "remaining_percent": 91.0},
            ])

    def test_submission_persists_an_immutable_forge_governance_handoff_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff = {"version": "1.0", "recommendation_set": {"id": "set-1"}}
            record_submission(
                root, submission_id="submission-handoff", producer_id="forge", producer_type="FORGE",
                prompt_content="bounded prompt", prompt_metadata={}, target_identity={}, original_envelope={},
                received_at="2026-08-15T08:00:00+00:00", link_run_id="inbox-handoff", forge_governance_handoff=handoff,
            )
            handoff["recommendation_set"]["id"] = "changed"
            self.assertEqual(load_forge_governance_handoff_snapshot(root, "inbox-handoff"), {
                "version": "1.0", "recommendation_set": {"id": "set-1"},
            })

    def test_submission_persists_an_immutable_execution_context_snapshot_and_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = {"context_version": "1.0", "mission_title": "Aurora", "future": {"value": True}}
            record_submission(
                root, submission_id="submission-42", producer_id="forge", producer_type="FORGE",
                prompt_content="bounded prompt", prompt_metadata={}, target_identity={},
                original_envelope='{"original":"unchanged"}', received_at="2026-08-07T08:00:00+00:00",
                link_run_id="inbox-42", execution_context=snapshot,
            )
            snapshot["mission_title"] = "changed after persistence"
            self.assertEqual(load_execution_context_snapshot(root, "inbox-42"), {
                "context_version": "1.0", "mission_title": "Aurora", "future": {"value": True},
            })
            record_submission(
                root, submission_id="submission-42", producer_id="forge", producer_type="FORGE",
                prompt_content="different", prompt_metadata={}, target_identity={},
                original_envelope='{"different":true}', received_at="2026-08-07T08:01:00+00:00",
                link_run_id="inbox-other", execution_context={"context_version": "2.0"},
            )
            self.assertEqual(load_execution_context_snapshot(root, "inbox-42")["context_version"], "1.0")
            with activate_storage_schema(root) as connection:
                self.assertEqual(
                    connection.execute("SELECT original_envelope FROM execution_submissions WHERE submission_id='submission-42'").fetchone()[0],
                    '{"original":"unchanged"}',
                )
    def test_persists_run_correlated_readiness_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState("readiness-run", "repo", "prompt.md", "INITIALIZE")
            )
            record_readiness_evaluation(
                root, run_id="readiness-run", profile_id="managed_repository", profile_version=1,
                execution_mode="MANAGED", passed=False, failed_requirements=("clean_worktree",),
                facts={"repository_clean": False}, evaluated_at="2026-08-07T09:00:00+00:00",
                diagnostic="working tree is not clean",
            )
            self.assertEqual(load_readiness_evaluation(root, "readiness-run"), {
                "profile_id": "managed_repository", "profile_version": 1, "execution_mode": "MANAGED",
                "result": "BLOCKED", "failed_requirements": ["clean_worktree"],
                "evaluated_at": "2026-08-07T09:00:00+00:00", "diagnostic": "working tree is not clean",
            })

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
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='prompt_execution_history'"
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

    def test_schema_twenty_five_repairs_early_usage_snapshot_table(self) -> None:
        """A database that recorded v24 before uncached counters can report."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                connection.execute("DROP TABLE provider_usage_snapshots")
                connection.execute(
                    """CREATE TABLE provider_usage_snapshots (
                        invocation_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                        input_tokens INTEGER, cached_input_tokens INTEGER,
                        output_tokens INTEGER, reasoning_tokens INTEGER,
                        total_tokens INTEGER, input_delta INTEGER,
                        cached_input_delta INTEGER, output_delta INTEGER,
                        PRIMARY KEY(invocation_id, ordinal)
                    )"""
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=25"
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=26"
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=27"
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=28"
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=29"
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=30"
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=31"
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=32"
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=33"
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=34"
                )
                connection.execute(
                    "DELETE FROM engineering_schema_migrations WHERE version=35"
                )
            with activate_storage_schema(root) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(provider_usage_snapshots)")
                }
                self.assertTrue({"uncached_input_tokens", "uncached_input_delta"} <= columns)
                self.assertEqual(
                    connection.execute(
                        "SELECT MAX(version) FROM engineering_schema_migrations"
                    ).fetchone()[0],
                    ENGINEERING_STORAGE_SCHEMA_VERSION,
                )

    def test_schema_four_imports_legacy_redacted_component_logs_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / WORKSPACE_DIRECTORY / "logs"
            logs.mkdir(parents=True)
            (logs / "inbox.log").write_text(
                '{"timestamp":"2026-08-02T12:00:00+00:00","event":"watcher_started"}\n',
                encoding="utf-8",
            )
            with activate_storage_schema(root) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT payload FROM engineering_component_logs WHERE component='inbox'"
                    ).fetchone()[0],
                    '{"event":"watcher_started","timestamp":"2026-08-02T12:00:00+00:00"}',
                )
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM engineering_component_logs WHERE component='inbox'"
                    ).fetchone()[0],
                    1,
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
            with activate_storage_schema(root) as connection:
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

    def test_execution_admission_defers_a_newer_root_schema_migration(self) -> None:
        """A prompt cannot upgrade the live store beyond its admitting watcher."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0],
                    ENGINEERING_STORAGE_SCHEMA_VERSION,
                )
            from tools.engineering import storage

            migrations = dict(storage.MIGRATIONS)
            migrations[ENGINEERING_STORAGE_SCHEMA_VERSION + 1] = lambda _: None
            environment = {
                "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT": str(root),
                "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA": str(ENGINEERING_STORAGE_SCHEMA_VERSION),
            }
            with patch.dict(os.environ, environment, clear=False), patch.object(
                storage, "ENGINEERING_STORAGE_SCHEMA_VERSION", ENGINEERING_STORAGE_SCHEMA_VERSION + 1
            ), patch.object(storage, "MIGRATIONS", migrations):
                with self.assertRaisesRegex(EngineeringStorageError, "migration is deferred"):
                    storage.open_storage(root)
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0],
                    ENGINEERING_STORAGE_SCHEMA_VERSION,
                )

    def test_existing_shared_store_requires_controlled_activation_for_a_new_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root):
                pass
            from tools.engineering import storage

            migrations = dict(storage.MIGRATIONS)
            migrations[ENGINEERING_STORAGE_SCHEMA_VERSION + 1] = lambda _: None
            with patch.object(storage, "ENGINEERING_STORAGE_SCHEMA_VERSION", ENGINEERING_STORAGE_SCHEMA_VERSION + 1), patch.object(
                storage, "MIGRATIONS", migrations
            ):
                with self.assertRaisesRegex(EngineeringStorageError, "controlled post-merge activation"):
                    open_storage(root)
                with activate_storage_schema(root) as connection:
                    self.assertEqual(
                        connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0],
                        ENGINEERING_STORAGE_SCHEMA_VERSION + 1,
                    )

    def test_storage_activation_requirement_probe_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root):
                pass
            from tools.engineering import storage

            self.assertFalse(storage.storage_activation_required(root))
            with patch.object(
                storage, "ENGINEERING_STORAGE_SCHEMA_VERSION", ENGINEERING_STORAGE_SCHEMA_VERSION + 1
            ):
                self.assertTrue(storage.storage_activation_required(root))

    def test_controlled_activation_refuses_active_execution_or_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root):
                pass
            from tools.engineering import storage
            from tools.engineering.execution_lease import acquire

            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState("inbox-schema-activation", "repo", "prompt.md", "INITIALIZE")
            )
            acquire(root, "inbox-schema-activation", identity="test", instance_id="test-instance")
            migrations = dict(storage.MIGRATIONS)
            migrations[ENGINEERING_STORAGE_SCHEMA_VERSION + 1] = lambda _: None
            with patch.object(storage, "ENGINEERING_STORAGE_SCHEMA_VERSION", ENGINEERING_STORAGE_SCHEMA_VERSION + 1), patch.object(
                storage, "MIGRATIONS", migrations
            ):
                with self.assertRaisesRegex(EngineeringStorageError, "no active execution lease"):
                    activate_storage_schema(root)
                with sqlite3.connect(database_path(root)) as connection:
                    connection.execute("UPDATE execution_run_leases SET lease_state='RELEASED'")
                lock = root / WORKSPACE_DIRECTORY / "locks" / "dashboard.lock"
                lock.parent.mkdir(parents=True)
                with lock.open("a+", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    with self.assertRaisesRegex(EngineeringStorageError, "dashboard to stop first"):
                        activate_storage_schema(root)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def test_storage_activation_command_reports_the_activated_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root):
                pass
            from tools.engineering import storage

            self.assertEqual(storage.main(["activate", "--repo", str(root)]), 0)

    def test_canonical_records_survive_projection_loss_and_verify_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / ".engineering" / "reports" / "report.md"
            payload.parent.mkdir(parents=True)
            payload.write_text("immutable evidence", encoding="utf-8")
            record_submission(
                root,
                submission_id="submission-1",
                producer_id="producer-1",
                producer_type="HUMAN",
                prompt_content="# prompt",
                prompt_metadata={"filename": "prompt.md"},
                target_identity={"repository": "djconnect"},
                original_envelope={"content": "# prompt"},
                received_at="2026-08-07T08:00:00+00:00",
            )
            record_artifact(
                root,
                payload,
                artifact_id="report-1",
                artifact_type="TERMINAL_REPORT",
                content_type="text/markdown",
                created_at="2026-08-07T08:00:01+00:00",
                submission_id="submission-1",
            )
            with open_storage(root) as connection:
                store_projection(connection, "watcher_status", {"watcher_state": "WATCHER_IDLE"})
            (root / ".engineering" / "status").mkdir(exist_ok=True)
            regenerate_status_projections(root)
            self.assertEqual(load_projection(root, "watcher_status"), {"watcher_state": "WATCHER_IDLE"})
            self.assertTrue(verify_artifact_integrity(root, "report-1"))
            payload.write_text("changed", encoding="utf-8")
            self.assertFalse(verify_artifact_integrity(root, "report-1"))
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute("SELECT integrity_status FROM execution_artifact_records WHERE artifact_id='report-1'").fetchone()[0],
                    "MISMATCH",
                )
