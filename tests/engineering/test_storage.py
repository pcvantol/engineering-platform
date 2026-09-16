"""Regression coverage for the versioned Engineering SQLite schema."""

from __future__ import annotations

from engineering_platform.storage import sqlite_connection

from datetime import datetime, timezone
import fcntl
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import agent_state, storage
from engineering_platform.storage import (
    DATABASE_FILENAME,
    ENGINEERING_STORAGE_SCHEMA_VERSION,
    MIGRATIONS,
    EngineeringStorageError,
    WORKSPACE_DIRECTORY,
    activate_storage_schema,
    database_path,
    ai_capacity_history,
    load_projection,
    load_execution_context_snapshot,
    load_forge_governance_handoff_snapshot,
    load_run_qualification_snapshot,
    open_storage,
    record_ai_capacity_bi_hourly,
    record_artifact,
    record_submission,
    record_admission_decision,
    load_admission_decision,
    record_readiness_evaluation,
    record_run_qualification_snapshot,
    load_readiness_evaluation,
    regenerate_status_projections,
    store_projection,
    verify_artifact_integrity,
)
from engineering_platform.agent_state import StateError, StateStore, TransactionState
from engineering_platform.platform_version import EngineeringPlatformManifest


class EngineeringStorageTest(unittest.TestCase):
    def test_sqlite_context_helpers_close_connections_and_preserve_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "connection.db"
            with storage.sqlite_connection(database) as connection:
                connection.execute("CREATE TABLE records(value TEXT NOT NULL)")
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

            failed_connection: sqlite3.Connection | None = None
            with self.assertRaisesRegex(RuntimeError, "abort transaction"):
                with storage.sqlite_connection(database) as failed_connection:
                    failed_connection.execute("INSERT INTO records(value) VALUES('discarded')")
                    raise RuntimeError("abort transaction")
            self.assertIsNotNone(failed_connection)
            with self.assertRaises(sqlite3.ProgrammingError):
                failed_connection.execute("SELECT 1")  # type: ignore[union-attr]
            verification = sqlite3.connect(database)
            try:
                self.assertEqual(verification.execute("SELECT value FROM records").fetchall(), [])
            finally:
                verification.close()

            with open_storage(root) as managed_connection:
                with managed_connection:
                    self.assertEqual(managed_connection.execute("PRAGMA foreign_keys").fetchone(), (1,))
            with self.assertRaises(sqlite3.ProgrammingError):
                managed_connection.execute("SELECT 1")

    def test_storage_admission_context_is_complete_valid_and_root_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {storage.ADMITTED_STORAGE_ROOT_ENVIRONMENT: str(root)}, clear=False):
                with self.assertRaisesRegex(EngineeringStorageError, "admission context is incomplete"):
                    storage._admitted_migration_ceiling(root)
            with patch.dict(os.environ, {
                storage.ADMITTED_STORAGE_ROOT_ENVIRONMENT: str(root),
                storage.ADMITTED_STORAGE_SCHEMA_ENVIRONMENT: "not-a-number",
            }, clear=False):
                with self.assertRaisesRegex(EngineeringStorageError, "admission schema is invalid"):
                    storage._admitted_migration_ceiling(root)
            with patch.dict(os.environ, {
                storage.ADMITTED_STORAGE_ROOT_ENVIRONMENT: str(root),
                storage.ADMITTED_STORAGE_SCHEMA_ENVIRONMENT: "17",
            }, clear=False):
                self.assertEqual(storage._admitted_migration_ceiling(root), 17)
                self.assertIsNone(storage._admitted_migration_ceiling(root / "other"))

    def test_checkpoint_decode_rejects_corrupt_identity_admission_and_recovery_ledgers(self) -> None:
        raw = TransactionState("safe-run", "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT").to_dict()
        cases = (
            {**raw, "schema_version": 0},
            {**raw, "run_id": "Unsafe Run"},
            {**raw, "owner_authorized": "yes"},
            {**raw, "admission_decision": "PASS", "admission_completed_at": None},
            {**raw, "provider_recovery_attempts": ({"bad": "ledger"},)},
            {**raw, "commit_evidence": ({"phase": "EXECUTE_AGENT"},)},
        )
        for checkpoint in cases:
            with self.assertRaises(StateError):
                TransactionState.from_dict(checkpoint)

    def test_central_checkpoint_has_no_checkout_shadow_and_missing_database_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "central.db"
            with open_storage(root) as source, sqlite_connection(database) as target:
                source.backup(target)
            store = StateStore(root / ".engineering" / "engineering-runs", central_database=database)
            state = TransactionState("central-safe-run", "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT")
            path = store.save(state)
            self.assertFalse(path.exists())
            self.assertEqual(store.load(state.run_id), state)
            missing = StateStore(root / "other" / "runs", central_database=root / "missing.db")
            with self.assertRaisesRegex(StateError, "canonical engineering storage is unavailable"):
                missing.run_ids()

    def test_central_checkpoint_retries_one_transient_disk_io_error(self) -> None:
        """A one-off SQLite I/O failure cannot discard a post-provider checkpoint."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "central.db"
            with open_storage(root) as source, sqlite_connection(database) as target:
                source.backup(target)
            store = StateStore(root / ".engineering" / "engineering-runs", central_database=database)
            original_open = store._open
            attempts = 0

            class FailFirstBegin:
                def __init__(self, connection: sqlite3.Connection) -> None:
                    self.connection = connection

                def execute(self, statement: str, *arguments: object) -> object:
                    if statement == "BEGIN IMMEDIATE":
                        raise sqlite3.OperationalError("disk I/O error")
                    return self.connection.execute(statement, *arguments)

                def close(self) -> None:
                    self.connection.close()

            def open_once(*, create: bool) -> sqlite3.Connection:
                nonlocal attempts
                attempts += 1
                connection = original_open(create=create)
                return FailFirstBegin(connection) if attempts == 1 else connection  # type: ignore[return-value]

            state = TransactionState("transient-io-run", "pcvantol/forge", "prompt.md", "LOCAL_REPOSITORY_VALIDATION")
            with patch.object(store, "_open", side_effect=open_once), patch.object(agent_state, "sleep") as delay:
                store.save(state)
            delay.assert_called_once_with(0.02)
            self.assertEqual(attempts, 2)
            self.assertEqual(store.load(state.run_id), state)

    def test_central_checkpoint_preserves_second_transient_io_error(self) -> None:
        """The bounded retry never turns a persistent SQLite failure into success."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "central.db"
            with open_storage(root) as source, sqlite_connection(database) as target:
                source.backup(target)
            store = StateStore(root / ".engineering" / "engineering-runs", central_database=database)
            original_open = store._open
            attempts = 0

            class FailBegin:
                def __init__(self, connection: sqlite3.Connection) -> None:
                    self.connection = connection

                def execute(self, statement: str, *arguments: object) -> object:
                    if statement == "BEGIN IMMEDIATE":
                        raise sqlite3.OperationalError("disk I/O error")
                    return self.connection.execute(statement, *arguments)

                def close(self) -> None:
                    self.connection.close()

            def open_always_failing(*, create: bool) -> sqlite3.Connection:
                nonlocal attempts
                attempts += 1
                return FailBegin(original_open(create=create))  # type: ignore[return-value]

            state = TransactionState("persistent-io-run", "pcvantol/forge", "prompt.md", "EXECUTE_AGENT")
            with patch.object(store, "_open", side_effect=open_always_failing), patch.object(agent_state, "sleep") as delay:
                with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
                    store.save(state)
            delay.assert_called_once_with(0.02)
            self.assertEqual(attempts, 2)

    def test_explicit_central_evidence_connection_uses_shared_sqlite_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            central = root / "epdata.sqlite"
            source = open_storage(root)
            target = sqlite3.connect(central)
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
            connection = storage._evidence_connection(root, central)
            try:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone(), (1,))
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone(), (10_000,))
            finally:
                connection.close()

    def test_central_storage_open_skips_legacy_schema_write_transaction(self) -> None:
        """A runner's CENTRAL read must not contend for the schema writer lock."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            central = root / "epdata.sqlite"
            source = open_storage(root)
            target = sqlite3.connect(central)
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
            statements: list[str] = []
            original_connect = sqlite3.connect

            def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
                connection = original_connect(*args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection

            with patch.object(storage.sqlite3, "connect", side_effect=traced_connect), patch.dict(
                os.environ,
                {storage.CENTRAL_OPERATIONAL_DATABASE_ENVIRONMENT: str(central)},
                clear=False,
            ):
                connection = open_storage(root)
            try:
                self.assertFalse(any(statement == "BEGIN IMMEDIATE" for statement in statements))
            finally:
                connection.close()

    def test_checkpoint_store_removes_json_shadow_and_rejects_corrupt_durable_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / ".engineering" / "engineering-runs")
            state = TransactionState("remove-safe-run", "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT")
            path = store.save(state)
            self.assertTrue(path.exists())
            connection = open_storage(root)
            connection.execute("UPDATE engineering_transactions SET payload='{' WHERE run_id=?", (state.run_id,))
            connection.close()
            with self.assertRaisesRegex(StateError, "canonical checkpoint is corrupt"):
                store.load(state.run_id)
            store.remove(state.run_id)
            self.assertFalse(path.exists())
            self.assertEqual(store.run_ids(), ())

    def test_checkpoint_save_and_central_database_errors_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = TransactionState("save-error-run", "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT")
            with patch("engineering_platform.agent_state.open_storage", side_effect=OSError("disk unavailable")):
                with self.assertRaisesRegex(StateError, "could not save checkpoint"):
                    StateStore(root / ".engineering" / "engineering-runs").save(state)
            corrupt = root / "corrupt-central.db"; corrupt.write_text("not a sqlite database", encoding="utf-8")
            with self.assertRaisesRegex(StateError, "canonical engineering storage is unavailable"):
                StateStore(root / "central-runs", central_database=corrupt).run_ids()
    def test_schema_39_adds_verifier_only_local_api_credential_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = database_path(root)
            path.parent.mkdir(parents=True)
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE engineering_schema_migrations(version INTEGER PRIMARY KEY)"
            )
            for version in range(1, 39):
                MIGRATIONS[version](connection)
                connection.execute(
                    "INSERT INTO engineering_schema_migrations(version) VALUES(?)", (version,)
                )
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(
                EngineeringStorageError, "controlled post-merge activation"
            ):
                open_storage(root)
            with activate_storage_schema(root) as activated:
                columns = {
                    str(row[1])
                    for row in activated.execute("PRAGMA table_info(ep_consumer_credentials)")
                }
                self.assertEqual(
                    columns,
                    {
                        "credential_id",
                        "consumer_id",
                        "project_id",
                        "verifier",
                        "fingerprint",
                        "issued_at",
                        "expires_at",
                        "revoked_at",
                        "replaced_by_credential_id",
                    },
                )
                self.assertFalse({"credential", "token", "secret", "plaintext"} & columns)
                activated.execute(
                    "INSERT INTO ep_consumer_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)",
                    (
                        "credential-one",
                        "consumer-one",
                        "project-one",
                        b"a" * 32,
                        b"b" * 32,
                        "2026-08-30T00:00:00+00:00",
                    ),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    activated.execute(
                        "INSERT INTO ep_consumer_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)",
                        (
                            "credential-two",
                            "consumer-one",
                            "project-one",
                            b"a" * 32,
                            b"c" * 32,
                            "2026-08-30T00:00:00+00:00",
                        ),
                    )

    def test_schema_7_backfills_safe_runtime_facts_and_rejects_report_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = database_path(root)
            path.parent.mkdir(parents=True)
            reports = path.parent / "reports"
            reports.mkdir()
            (reports / "valid.md").write_text(
                "- Runtime Provider: `Codex`\n- AI Model: `gpt-5`\n"
                "- Reasoning Profile: `high`\n- Configuration Profile: `safe`\n"
                "- Submitted Prompt Characters: `42`\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(path)
            for version in range(1, 7):
                MIGRATIONS[version](connection)
            run_values = (
                "historical-run", "2026-01-01", "now", "now", "now", 0.0, None,
                "COMPLETE", None, None, None, "MANAGED", "workspace", "repository", "1.0",
            )
            connection.execute(
                "INSERT INTO execution_runs(run_id,execution_date,arrived_at,execution_started_at,execution_finished_at,queue_wait_seconds,execution_seconds,terminal_state,input_tokens,output_tokens,total_tokens,execution_mode,workspace,repository,execution_host_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                run_values,
            )
            connection.execute(
                "INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,git_commit,report_path,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("historical-run", "COMPLETE", "historical", "now", None, "valid.md", "now"),
            )
            connection.execute(
                "INSERT INTO execution_runs(run_id,execution_date,arrived_at,execution_started_at,execution_finished_at,queue_wait_seconds,execution_seconds,terminal_state,input_tokens,output_tokens,total_tokens,execution_mode,workspace,repository,execution_host_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("escaped-run", *run_values[1:]),
            )
            connection.execute(
                "INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,git_commit,report_path,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("escaped-run", "COMPLETE", "historical", "now", None, "../outside.md", "now"),
            )
            MIGRATIONS[7](connection)
            backfill = connection.execute(
                "SELECT prompt_characters,runtime_provider,runtime_model,reasoning_profile,configuration_profile FROM execution_runs WHERE run_id=?",
                ("historical-run",),
            ).fetchone()
            escaped = connection.execute(
                "SELECT prompt_characters,runtime_provider FROM execution_runs WHERE run_id=?", ("escaped-run",)
            ).fetchone()
            connection.close()
        self.assertEqual(backfill, (42, "Codex", "gpt-5", "high", "safe"))
        self.assertEqual(escaped, (None, None))

    def test_provider_recovery_schema_is_prospective_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = TransactionState(
                "recovery-schema", "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT"
            )
            StateStore(root / ".engineering" / "engineering-runs").save(state)
            connection = open_storage(root)
            connection.execute(
                "INSERT INTO provider_recovery_attempts(run_id,recovery_ordinal,maximum_attempts,triggering_invocation_id,replacement_invocation_id,lifecycle_phase,state,requested_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    state.run_id,
                    1,
                    1,
                    "invocation-1",
                    "invocation-2",
                    "EXECUTE_AGENT",
                    "RECOVERY_AVAILABLE",
                    "2026-08-30T00:00:00+00:00",
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO provider_recovery_attempts(run_id,recovery_ordinal,maximum_attempts,triggering_invocation_id,replacement_invocation_id,lifecycle_phase,state,requested_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "second-run",
                        2,
                        1,
                        "invocation-x",
                        "invocation-y",
                        "EXECUTE_AGENT",
                        "RECOVERY_AVAILABLE",
                        "2026-08-30T00:00:00+00:00",
                    ),
                )
            recovery_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(provider_recovery_attempts)")
            }
            receipt_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(provider_invocation_receipts)")
            }
            self.assertIn("provider_session_id", recovery_columns)
            self.assertTrue(
                {"provider_session_id", "process_start_fingerprint", "process_executable_identity"}
                <= receipt_columns
            )
            connection.close()

    def test_platform_manifest_tracks_the_current_storage_schema(self) -> None:
        root = Path(__file__).parents[2]
        manifest = EngineeringPlatformManifest.load(
            root / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json"
        )
        self.assertEqual(manifest.storage_schema, ENGINEERING_STORAGE_SCHEMA_VERSION)

    def test_provider_free_admission_decision_is_immutable_and_renderable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_submission(
                root,
                submission_id="submission-admission",
                producer_id="test",
                producer_type="HUMAN",
                prompt_content="bounded",
                prompt_metadata={},
                target_identity={},
                original_envelope={},
                received_at="2026-08-27T00:00:00+00:00",
                link_run_id="inbox-admission",
            )
            record_admission_decision(
                root,
                run_id="inbox-admission",
                submission_id="submission-admission",
                execution_mode="MANAGED",
                decision="FAIL",
                failed_gate_ids=("worktree_untracked",),
                evidence=(
                    {
                        "gate_id": "worktree_untracked",
                        "expected": "PASS",
                        "observed": "FAIL",
                        "verified_at": "2026-08-27T00:00:00+00:00",
                    },
                ),
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
                root,
                provider="Codex CLI",
                remaining_percent=72,
                observed_at=first_bucket.replace(hour=11, minute=55),
            )
            record_ai_capacity_bi_hourly(
                root,
                provider="Codex CLI",
                remaining_percent=91,
                observed_at=first_bucket.replace(hour=12),
            )
            with patch("engineering_platform.storage.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(
                    2026, 8, 25, 12, 15, tzinfo=timezone.utc
                )
                mocked_datetime.fromtimestamp.side_effect = datetime.fromtimestamp
                mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
                history = ai_capacity_history(root, provider="Codex CLI")
            self.assertEqual(
                history,
                [
                    {"at": "2026-08-25T10:00:00+00:00", "remaining_percent": 72.0},
                    {"at": "2026-08-25T12:00:00+00:00", "remaining_percent": 91.0},
                ],
            )

    def test_submission_persists_an_immutable_forge_governance_handoff_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff = {"version": "1.0", "recommendation_set": {"id": "set-1"}}
            record_submission(
                root,
                submission_id="submission-handoff",
                producer_id="forge",
                producer_type="FORGE",
                prompt_content="bounded prompt",
                prompt_metadata={},
                target_identity={},
                original_envelope={},
                received_at="2026-08-15T08:00:00+00:00",
                link_run_id="inbox-handoff",
                forge_governance_handoff=handoff,
            )
            handoff["recommendation_set"]["id"] = "changed"
            self.assertEqual(
                load_forge_governance_handoff_snapshot(root, "inbox-handoff"),
                {
                    "version": "1.0",
                    "recommendation_set": {"id": "set-1"},
                },
            )

    def test_submission_persists_an_immutable_execution_context_snapshot_and_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = {
                "context_version": "1.0",
                "mission_title": "Aurora",
                "future": {"value": True},
            }
            record_submission(
                root,
                submission_id="submission-42",
                producer_id="forge",
                producer_type="FORGE",
                prompt_content="bounded prompt",
                prompt_metadata={},
                target_identity={},
                original_envelope='{"original":"unchanged"}',
                received_at="2026-08-07T08:00:00+00:00",
                link_run_id="inbox-42",
                execution_context=snapshot,
            )
            snapshot["mission_title"] = "changed after persistence"
            self.assertEqual(
                load_execution_context_snapshot(root, "inbox-42"),
                {
                    "context_version": "1.0",
                    "mission_title": "Aurora",
                    "future": {"value": True},
                },
            )
            with self.assertRaisesRegex(EngineeringStorageError, "different run"):
                record_submission(
                    root,
                    submission_id="submission-42",
                    producer_id="forge",
                    producer_type="FORGE",
                    prompt_content="different",
                    prompt_metadata={},
                    target_identity={},
                    original_envelope='{"different":true}',
                    received_at="2026-08-07T08:01:00+00:00",
                    link_run_id="inbox-other",
                    execution_context={"context_version": "2.0"},
                )
            self.assertEqual(
                load_execution_context_snapshot(root, "inbox-42")["context_version"], "1.0"
            )
            with activate_storage_schema(root) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT original_envelope FROM execution_submissions WHERE submission_id='submission-42'"
                    ).fetchone()[0],
                    '{"original":"unchanged"}',
                )

    def test_persists_run_correlated_readiness_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState("readiness-run", "repo", "prompt.md", "INITIALIZE")
            )
            record_readiness_evaluation(
                root,
                run_id="readiness-run",
                profile_id="managed_repository",
                profile_version=1,
                execution_mode="MANAGED",
                passed=False,
                failed_requirements=("clean_worktree",),
                facts={"repository_clean": False},
                evaluated_at="2026-08-07T09:00:00+00:00",
                diagnostic="working tree is not clean",
            )
            self.assertEqual(
                load_readiness_evaluation(root, "readiness-run"),
                {
                    "profile_id": "managed_repository",
                    "profile_version": 1,
                    "execution_mode": "MANAGED",
                    "result": "BLOCKED",
                    "failed_requirements": ["clean_worktree"],
                    "evaluated_at": "2026-08-07T09:00:00+00:00",
                    "diagnostic": "working tree is not clean",
                },
            )

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
                    connection.execute(
                        "SELECT COUNT(*) FROM engineering_schema_migrations"
                    ).fetchone()[0],
                    ENGINEERING_STORAGE_SCHEMA_VERSION,
                )
                self.assertEqual(
                    connection.execute("SELECT content FROM engineering_artifacts").fetchone()[0],
                    b"test",
                )

    def test_schema_forty_five_preserves_old_rows_and_adds_reconciliation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO managed_governance_gates("
                    "run_id,gate_type,gate_authority,status,requested_at,related_pr,phase) "
                    "VALUES('migration-run','IMPLEMENTATION_MERGE_APPROVAL','OPERATOR',"
                    "'SATISFIED','now',41,'MERGE')"
                )
                connection.execute(
                    "INSERT INTO managed_pr_check_observations("
                    "run_id,pr_number,pr_role,pr_state,merge_state,merge_commit,"
                    "required_checks_state,evidence_ref,observed_at,currentness) "
                    "VALUES('migration-run',41,'IMPLEMENTATION','MERGED','MERGED',?,"
                    "'PASS','github','now',0)",
                    ("a" * 40,),
                )
                MIGRATIONS[45](connection)
                self.assertEqual(
                    connection.execute(
                        "SELECT gate_type,status,related_pr FROM managed_governance_gates "
                        "WHERE run_id='migration-run'"
                    ).fetchall(),
                    [("IMPLEMENTATION_MERGE_APPROVAL", "SATISFIED", 41)],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT pr_role,required_checks_state FROM managed_pr_check_observations "
                        "WHERE run_id='migration-run'"
                    ).fetchall(),
                    [("IMPLEMENTATION", "PASS")],
                )
                connection.execute(
                    "INSERT INTO managed_governance_gates("
                    "run_id,gate_type,gate_authority,status,requested_at,related_pr,phase) "
                    "VALUES('migration-run','RECONCILIATION_MERGE_APPROVAL','OPERATOR',"
                    "'WAITING','now',43,'WAIT_FOR_OPERATOR_MERGE')"
                )
                connection.execute(
                    "INSERT INTO managed_pr_check_observations("
                    "run_id,pr_number,pr_role,pr_state,merge_state,required_checks_state,"
                    "evidence_ref,observed_at,currentness) VALUES("
                    "'migration-run',43,'RECONCILIATION','OPEN','NOT_MERGED','PASS',"
                    "'github','now',0)"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO managed_pr_check_observations("
                        "run_id,pr_number,pr_role,pr_state,merge_state,required_checks_state,"
                        "evidence_ref,observed_at,currentness) VALUES("
                        "'migration-run',44,'UNSCOPED','OPEN','NOT_MERGED','PASS',"
                        "'github','now',0)"
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
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=25")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=26")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=27")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=28")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=29")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=30")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=31")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=32")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=33")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=34")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=35")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=36")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=37")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=38")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=39")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=40")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=41")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=42")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=43")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=44")
                connection.execute("DELETE FROM engineering_schema_migrations WHERE version=45")
            with activate_storage_schema(root) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(provider_usage_snapshots)")
                }
                self.assertTrue({"uncached_input_tokens", "uncached_input_delta"} <= columns)
                artifact_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(execution_artifact_records)")
                }
                self.assertTrue({"ep_run_id", "ep_submission_id"} <= artifact_columns)
                self.assertEqual(
                    connection.execute(
                        "SELECT MAX(version) FROM engineering_schema_migrations"
                    ).fetchone()[0],
                    ENGINEERING_STORAGE_SCHEMA_VERSION,
                )

    def test_run_qualification_snapshot_is_immutable_and_historical_runs_are_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(load_run_qualification_snapshot(root, "legacy-run"))
            snapshot = {
                "run_id": "future-run",
                "qualification_snapshot_id": "qualification:sha256:test",
                "required_control_snapshot_ref": "required-controls:sha256:test",
                "terminal_checkpoint_ref": "terminal-checkpoint:future-run:COMPLETE",
                "persisted_at": "2026-08-30T00:00:00+00:00",
                "terminal_execution_state": "COMPLETE",
                "required_validation_state": "PASS",
                "cleanup_outcome": "COMPLETED",
                "run_qualification": "QUALIFIED",
                "projection_conflicts": [],
            }
            stored = record_run_qualification_snapshot(root, snapshot)
            self.assertEqual(stored["run_qualification"], "QUALIFIED")
            with open_storage(root) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "UPDATE execution_run_qualification_snapshots SET payload='{}'"
                    )

    def test_schema_four_does_not_restore_legacy_component_log_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / WORKSPACE_DIRECTORY / "logs"
            logs.mkdir(parents=True)
            (logs / "inbox.log").write_text(
                '{"timestamp":"2026-08-02T12:00:00+00:00","event":"watcher_started"}\n',
                encoding="utf-8",
            )
            with activate_storage_schema(root) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT payload FROM engineering_component_logs WHERE component='inbox'"
                    ).fetchone()
                )
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM engineering_component_logs WHERE component='inbox'"
                    ).fetchone()[0],
                    0,
                )

    def test_refuses_unknown_non_versioned_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = database_path(root)
            path.parent.mkdir()
            with sqlite_connection(path) as connection:
                connection.execute("CREATE TABLE unrelated(value TEXT)")
            with self.assertRaisesRegex(EngineeringStorageError, "no recognized schema history"):
                open_storage(root)

    def test_upgrades_the_pre_release_schema_without_losing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = database_path(root)
            path.parent.mkdir()
            with sqlite_connection(path) as connection:
                connection.execute(
                    "CREATE TABLE ep_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE ep_status(name TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
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
                connection.execute(
                    "INSERT INTO ep_transactions VALUES('inbox-123','{}','COMPLETE','now')"
                )
                connection.execute(
                    "INSERT INTO ep_artifacts VALUES(1,'report','inbox-123','report.md',X'65766964656E6365','now')"
                )
                connection.execute(
                    "INSERT INTO ep_component_logs VALUES(1,'inbox','{\"event\":\"watcher_started\"}','now')"
                )
            with activate_storage_schema(root) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT payload FROM engineering_status WHERE name='canonical'"
                    ).fetchone()[0],
                    '{"watcher_state":"WATCHER_IDLE"}',
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT phase FROM engineering_transactions WHERE run_id='inbox-123'"
                    ).fetchone()[0],
                    "COMPLETE",
                )
                self.assertEqual(
                    connection.execute("SELECT content FROM engineering_artifacts").fetchone()[0],
                    b"evidence",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT component FROM engineering_component_logs"
                    ).fetchone()[0],
                    "inbox",
                )

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
                    connection.execute(
                        "SELECT MAX(version) FROM engineering_schema_migrations"
                    ).fetchone()[0],
                    ENGINEERING_STORAGE_SCHEMA_VERSION,
                )
            from engineering_platform import storage

            migrations = dict(storage.MIGRATIONS)
            migrations[ENGINEERING_STORAGE_SCHEMA_VERSION + 1] = lambda _: None
            environment = {
                "ENGINEERING_PLATFORM_ADMITTED_STORAGE_ROOT": str(root),
                "ENGINEERING_PLATFORM_ADMITTED_STORAGE_SCHEMA": str(
                    ENGINEERING_STORAGE_SCHEMA_VERSION
                ),
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch.object(
                    storage,
                    "ENGINEERING_STORAGE_SCHEMA_VERSION",
                    ENGINEERING_STORAGE_SCHEMA_VERSION + 1,
                ),
                patch.object(storage, "MIGRATIONS", migrations),
            ):
                with self.assertRaisesRegex(EngineeringStorageError, "migration is deferred"):
                    storage.open_storage(root)
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT MAX(version) FROM engineering_schema_migrations"
                    ).fetchone()[0],
                    ENGINEERING_STORAGE_SCHEMA_VERSION,
                )

    def test_existing_shared_store_requires_controlled_activation_for_a_new_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root):
                pass
            from engineering_platform import storage

            migrations = dict(storage.MIGRATIONS)
            migrations[ENGINEERING_STORAGE_SCHEMA_VERSION + 1] = lambda _: None
            with (
                patch.object(
                    storage,
                    "ENGINEERING_STORAGE_SCHEMA_VERSION",
                    ENGINEERING_STORAGE_SCHEMA_VERSION + 1,
                ),
                patch.object(storage, "MIGRATIONS", migrations),
            ):
                with self.assertRaisesRegex(
                    EngineeringStorageError, "controlled post-merge activation"
                ):
                    open_storage(root)
                with activate_storage_schema(root) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT MAX(version) FROM engineering_schema_migrations"
                        ).fetchone()[0],
                        ENGINEERING_STORAGE_SCHEMA_VERSION + 1,
                    )

    def test_storage_activation_requirement_probe_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root):
                pass
            from engineering_platform import storage

            self.assertFalse(storage.storage_activation_required(root))
            with patch.object(
                storage,
                "ENGINEERING_STORAGE_SCHEMA_VERSION",
                ENGINEERING_STORAGE_SCHEMA_VERSION + 1,
            ):
                self.assertTrue(storage.storage_activation_required(root))

    def test_controlled_activation_refuses_active_execution_or_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root):
                pass
            from engineering_platform import storage
            from engineering_platform.execution_lease import acquire

            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState("inbox-schema-activation", "repo", "prompt.md", "INITIALIZE")
            )
            acquire(root, "inbox-schema-activation", identity="test", instance_id="test-instance")
            migrations = dict(storage.MIGRATIONS)
            migrations[ENGINEERING_STORAGE_SCHEMA_VERSION + 1] = lambda _: None
            with (
                patch.object(
                    storage,
                    "ENGINEERING_STORAGE_SCHEMA_VERSION",
                    ENGINEERING_STORAGE_SCHEMA_VERSION + 1,
                ),
                patch.object(storage, "MIGRATIONS", migrations),
            ):
                with self.assertRaisesRegex(EngineeringStorageError, "no active execution lease"):
                    activate_storage_schema(root)
                with sqlite_connection(database_path(root)) as connection:
                    connection.execute("UPDATE execution_run_leases SET lease_state='RELEASED'")
                    connection.execute(
                        "INSERT OR REPLACE INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                        (
                            "inbox-schema-activation",
                            "{}",
                            "VALIDATION",
                            "2026-08-30T00:00:00+00:00",
                        ),
                    )
                with self.assertRaisesRegex(EngineeringStorageError, "no non-terminal execution"):
                    activate_storage_schema(root)
                with sqlite_connection(database_path(root)) as connection:
                    connection.execute(
                        "UPDATE engineering_transactions SET phase='COMPLETE' WHERE run_id='inbox-schema-activation'"
                    )
                # Legacy Dashboard/Inbox watcher locks are no longer lifecycle
                # authority and cannot block CENTRAL schema activation.
                with activate_storage_schema(root):
                    pass

    def test_storage_activation_command_reports_the_activated_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root):
                pass
            from engineering_platform import storage

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
            self.assertEqual(
                load_projection(root, "watcher_status"), {"watcher_state": "WATCHER_IDLE"}
            )
            self.assertTrue(verify_artifact_integrity(root, "report-1"))
            payload.write_text("changed", encoding="utf-8")
            self.assertFalse(verify_artifact_integrity(root, "report-1"))
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT integrity_status FROM execution_artifact_records WHERE artifact_id='report-1'"
                    ).fetchone()[0],
                    "MISMATCH",
                )
