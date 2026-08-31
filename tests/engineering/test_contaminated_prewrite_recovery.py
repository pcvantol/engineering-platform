"""Isolated controller coverage for contaminated pre-write authority recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
import hashlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import central_store_migration as migration
from engineering_platform import storage


class _Services:
    def __init__(self) -> None:
        self.states = {label: True for label in migration.SERVICE_START_ORDER}

    def stop(self, label: str) -> None:
        self.states[label] = False

    def start(self, label: str) -> None:
        self.states[label] = True

    def stopped(self, label: str) -> bool:
        return not self.states.get(label, False)

    def running(self, label: str) -> bool:
        return self.states.get(label, False)


class _FailingServices(_Services):
    def stopped(self, label: str) -> bool:
        return False if label == migration.SERVICE_STOP_ORDER[0] else super().stopped(label)


class ContaminatedPrewriteRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ["DJCONNECT_EP_TEST_INSTALLATION_ROOT"])
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.data = Path(self.temporary.name) / "installation"
        self.legacy = self.repo / ".engineering" / "engineering.db"
        with storage.open_storage(self.repo):
            pass
        self.migration_id = "recovery-test"
        self.services = _Services()
        self.root_patch = patch.object(migration, "installation_data_root", return_value=self.data)
        self.target_patch = patch.object(migration, "central_store_path", return_value=self.data / "engineering.db")
        self.pointer_patch = patch.object(storage, "_authority_pointer_path", return_value=self.data / "runtime" / "store-authority.json")
        self.root_patch.start()
        self.target_patch.start()
        self.pointer_patch.start()
        migration.set_admission_freeze(self.repo, migration_id=self.migration_id, reason="test")
        self.central = self.data / "engineering.db"
        migration.copy_snapshot(self.legacy, self.central)
        with sqlite3.connect(self.central) as connection:
            connection.execute("CREATE TABLE backup_probe (value TEXT)")
            connection.execute("INSERT INTO backup_probe VALUES ('fixture')")
            connection.execute("INSERT INTO engineering_schema_migrations(version) VALUES(41)")
        pointer = migration.write_authority_pointer(migration_id=self.migration_id, authority=self.central, legacy=self.legacy, state="AUTHORITY_SWITCHED")
        self.database_patch = patch.object(
            migration, "database_path", side_effect=lambda _repo: self.central if (self.data / "runtime" / "store-authority.json").exists() and json.loads((self.data / "runtime" / "store-authority.json").read_text())["authoritative_path"] == str(self.central.resolve()) else self.legacy
        )
        self.database_patch.start()
        receipt = migration.load_receipt(self.migration_id)
        assert receipt is not None
        baseline = {"source": {"fingerprint_sha256": migration._fingerprint(self.legacy)}}
        receipt.update({"state": "SERVICES_RESTARTED", "legacy_path": str(self.legacy), "quiescent_source_baseline": baseline, "authority_pointer": pointer})
        migration._atomic_json(migration.receipt_path(self.migration_id), receipt)

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.pointer_patch.stop()
        self.target_patch.stop()
        self.root_patch.stop()
        self.temporary.cleanup()

    def test_eligible_recovery_preserves_central_and_returns_to_legacy(self) -> None:
        before = migration._fingerprint(self.central)
        result = migration.recover_contaminated_prewrite(self.repo, migration_id=self.migration_id, services=self.services)
        self.assertEqual(result["state"], "ROLLBACK_COMPLETED")
        self.assertEqual(migration._fingerprint(self.central), before)
        self.assertEqual(migration.database_path(self.repo), self.legacy)
        self.assertEqual(migration.admission_status(self.repo)["state"], "ACTIVE")
        self.assertEqual(migration.classify_target(self.central)["state"], "FORENSIC_CONTAMINATED_NON_AUTHORITATIVE")
        self.assertEqual(migration.recover_contaminated_prewrite(self.repo, migration_id=self.migration_id, services=self.services)["state"], "ROLLBACK_COMPLETED")

    def test_legitimate_human_submission_blocks_before_service_stop(self) -> None:
        with sqlite3.connect(self.central) as connection:
            connection.execute(
                "INSERT INTO execution_submissions (submission_id, producer_id, producer_type, prompt_content, prompt_metadata, target_identity, original_envelope, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("human-submission", "ingress", "HUMAN", "redacted", "{}", "target", "{}", "2026-01-01T00:00:00Z"),
            )
        with self.assertRaisesRegex(migration.CutoverError, "LEGITIMATE_CENTRAL_WRITE_PRESENT"):
            migration.recover_contaminated_prewrite(self.repo, migration_id=self.migration_id, services=self.services)
        self.assertTrue(all(self.services.states.values()))
        self.assertEqual(migration.load_receipt(self.migration_id)["state"], "SERVICES_RESTARTED")

    def test_unknown_contamination_fails_closed(self) -> None:
        with sqlite3.connect(self.central) as connection:
            connection.execute("DROP TABLE backup_probe")
            connection.execute("DELETE FROM engineering_schema_migrations WHERE version=41")
        with self.assertRaisesRegex(migration.CutoverError, "CONTAMINATION_PROVENANCE_UNRESOLVED"):
            migration.recover_contaminated_prewrite(self.repo, migration_id=self.migration_id, services=self.services)

    def test_authority_independent_baseline_domains_fail_closed_on_delta(self) -> None:
        baseline = migration.authority_independent_baseline_attestation(self.legacy, self.central)
        self.assertFalse(baseline["credential_delta"])
        self.assertFalse(baseline["registration_delta"])
        self.assertFalse(baseline["project_scope_delta"])
        with sqlite3.connect(self.central) as connection:
            connection.execute(
                "INSERT INTO local_api_credentials (credential_id, consumer_id, project_id, verifier, fingerprint, issued_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("delta", "consumer", "project", b"v" * 32, b"f" * 32, "2026-01-01T00:00:00Z"),
            )
        changed = migration.authority_independent_baseline_attestation(self.legacy, self.central)
        self.assertTrue(changed["credential_delta"])
        self.assertEqual(changed["domains"]["credentials"]["classification"], "CONTAMINATION_PROVENANCE_UNRESOLVED")
        with self.assertRaisesRegex(migration.CutoverError, "CONTAMINATION_PROVENANCE_UNRESOLVED"):
            migration.recover_contaminated_prewrite(self.repo, migration_id=self.migration_id, services=self.services)

    def test_registration_and_project_scope_delta_fail_closed(self) -> None:
        with sqlite3.connect(self.central) as connection:
            connection.execute(
                "INSERT INTO local_api_consumer_registrations (consumer_id, project_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("delta-consumer", "delta-project", "ACTIVE", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        changed = migration.authority_independent_baseline_attestation(self.legacy, self.central)
        self.assertTrue(changed["registration_delta"])
        self.assertTrue(changed["project_scope_delta"])
        self.assertEqual(changed["domains"]["registrations"]["classification"], "CONTAMINATION_PROVENANCE_UNRESOLVED")
        self.assertEqual(changed["domains"]["project_scope"]["classification"], "CONTAMINATION_PROVENANCE_UNRESOLVED")

    def test_legacy_drift_and_service_quiescence_fail_closed(self) -> None:
        with sqlite3.connect(self.legacy) as connection:
            connection.execute("CREATE TABLE recovery_drift (value TEXT)")
        with self.assertRaisesRegex(migration.CutoverError, "LEGACY_BASELINE_MISMATCH"):
            migration.recover_contaminated_prewrite(self.repo, migration_id=self.migration_id, services=self.services)

        self.setUp_baseline_after_drift()
        with self.assertRaisesRegex(migration.CutoverError, "RECOVERY_SERVICE_QUIESCENCE_FAILED"):
            migration.recover_contaminated_prewrite(self.repo, migration_id=self.migration_id, services=_FailingServices())
        self.assertEqual(migration.database_path(self.repo), self.central)

    def setUp_baseline_after_drift(self) -> None:
        receipt = migration.load_receipt(self.migration_id)
        assert receipt is not None
        receipt["quiescent_source_baseline"] = {"source": {"fingerprint_sha256": migration._fingerprint(self.legacy)}}
        migration._atomic_json(migration.receipt_path(self.migration_id), receipt)

    def _submission(self, path: Path, run_id: str, producer_type: str = "HUMAN") -> None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO execution_submissions (submission_id,producer_id,producer_type,prompt_content,prompt_metadata,target_identity,original_envelope,execution_run_id,received_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"submission-{run_id}", "fixture", producer_type, "redacted", "{}", "target", "{}", run_id, "2026-01-01T00:00:00Z"),
            )

    def _history(self, path: Path, run_id: str, title: str = "Fixture") -> None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,updated_at) VALUES(?,?,?,?,?)",
                (run_id, "COMPLETE", title, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )

    def test_provider_recovery_delta_resolves_only_its_new_legitimate_descendant(self) -> None:
        self._submission(self.legacy, "run-a")
        migration.copy_snapshot(self.legacy, self.central)
        with sqlite3.connect(self.central) as connection:
            connection.execute(
                "INSERT INTO provider_recovery_attempts(run_id,recovery_ordinal,maximum_attempts,triggering_invocation_id,replacement_invocation_id,lifecycle_phase,state,requested_at) VALUES(?,?,?,?,?,?,?,?)",
                ("run-a", 1, 1, "original", "replacement", "PROVIDER_EXECUTION", "RECOVERED", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO provider_invocation_receipts(receipt_id,run_id,invocation_id,launch_state,started_at) VALUES(?,?,?,?,?)",
                ("receipt-a", "run-a", "replacement", "TERMINAL", "2026-01-01T00:00:00Z"),
            )
        result = migration.managed_lineage_attestation(self.legacy, self.central)
        self.assertEqual(result["production_component_count"], 1)
        self.assertEqual(result["unresolved_component_count"], 0)
        self.assertEqual(result["categories"]["recovery"], 1)
        self.assertEqual(result["categories"]["provider"], 1)

    def test_finalization_and_reconciliation_delta_deny_recovery_without_reclassifying_history(self) -> None:
        self._submission(self.legacy, "run-a")
        self._history(self.legacy, "run-a", "Historical")
        migration.copy_snapshot(self.legacy, self.central)
        with sqlite3.connect(self.central) as connection:
            connection.execute(
                "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                ("run-a", '{"phase":"FINALIZATION"}', "FINALIZE_AGENT", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO execution_run_reconciliations(run_id,outcome,reason,reconciled_at,updated_at) VALUES(?,?,?,?,?)",
                ("run-a", "TERMINAL_EVIDENCE_PRESENT", "fixture", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        result = migration.managed_lineage_attestation(self.legacy, self.central)
        self.assertEqual(result["production_component_count"], 1)
        self.assertEqual(result["categories"]["finalization"], 1)
        self.assertEqual(result["categories"]["reconciliation"], 1)
        self.assertNotIn("prompt_execution_history", result["changed_rows"])

    def test_baseline_equal_historical_lineage_is_not_seeded_by_test_contamination(self) -> None:
        self._submission(self.legacy, "run-a")
        self._history(self.legacy, "run-a", "Historical")
        migration.copy_snapshot(self.legacy, self.central)
        self._submission(self.central, "run-b", "TEST_HARNESS")
        self._history(self.central, "run-b", "Harness")
        result = migration.managed_lineage_attestation(self.legacy, self.central)
        self.assertEqual(result["production_component_count"], 0)
        self.assertEqual(result["test_component_count"], 1)
        self.assertEqual(result["unresolved_component_count"], 0)
        self.assertEqual(result["changed_run_nodes"], {"prompt_execution_history": ["run-b"], "execution_submissions": ["run-b"]})

    def test_orphan_and_modified_or_removed_production_evidence_fail_closed(self) -> None:
        self._history(self.central, "orphan")
        orphan = migration.managed_lineage_attestation(self.legacy, self.central)
        self.assertEqual(orphan["unresolved_component_count"], 1)
        self._submission(self.legacy, "run-a")
        self._history(self.legacy, "run-a", "Before")
        migration.copy_snapshot(self.legacy, self.central)
        with sqlite3.connect(self.central) as connection:
            connection.execute("UPDATE prompt_execution_history SET prompt_title='After' WHERE run_id='run-a'")
        modified = migration.managed_lineage_attestation(self.legacy, self.central)
        self.assertEqual(modified["production_component_count"], 1)
        with sqlite3.connect(self.central) as connection:
            connection.execute("DELETE FROM prompt_execution_history WHERE run_id='run-a'")
        removed = migration.managed_lineage_attestation(self.legacy, self.central)
        self.assertEqual(removed["production_component_count"], 1)

    def test_run_bound_table_without_primary_or_unique_key_is_a_schema_blocker(self) -> None:
        with sqlite3.connect(self.central) as connection:
            connection.execute("CREATE TABLE unkeyed_run_evidence (run_id TEXT, payload TEXT)")
            connection.execute("INSERT INTO unkeyed_run_evidence VALUES ('orphan','{}')")
        with self.assertRaisesRegex(migration.CutoverError, "run-bound table has no deterministic key: unkeyed_run_evidence"):
            migration.managed_lineage_attestation(self.legacy, self.central)

    def _historical_authority_fixture(self) -> None:
        with sqlite3.connect(self.central) as connection:
            registrations = (
                ("workspace-client", "project-alpha", "ACTIVE", "now", "now", None, "{}"),
                ("consumer", "project", "DISABLED", "2026-08-31 12:44:24", "2026-08-31 12:44:26", "2026-08-31 12:44:26", '{"action":"DISABLE"}'),
                ("rotate", "project", "ACTIVE", "2026-08-31 12:44:26", "2026-08-31 12:44:26", None, '{"action":"REGISTER"}'),
            )
            connection.executemany("INSERT INTO local_api_consumer_registrations(consumer_id,project_id,status,created_at,updated_at,disabled_at,audit_metadata) VALUES(?,?,?,?,?,?,?)", registrations)
            credentials = (
                ("credential-alpha", "workspace-client", "project-alpha", "now", None),
                ("production-consumer", "consumer", "project", "2026-08-31 12:44:25", "2026-08-31 12:44:26"),
                ("production-rotate-old", "rotate", "project", "2026-08-31 12:44:26", "2026-08-31 12:44:27"),
                ("production-rotate-new", "rotate", "project", "2026-08-31 12:44:26", None),
                ("qualification-fixture", "qualification-client", "qualification-project", "2026-08-31 12:44:27", None),
            )
            for credential_id, consumer_id, project_id, issued_at, revoked_at in credentials:
                expires_at = "2026-08-31 12:59:27" if credential_id.startswith("qualification-") else None
                connection.execute("INSERT INTO local_api_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at,expires_at,revoked_at) VALUES(?,?,?,?,?,?,?,?)", (credential_id, consumer_id, project_id, hashlib.sha256(("v:" + credential_id).encode()).digest(), hashlib.sha256(("f:" + credential_id).encode()).digest(), issued_at, expires_at, revoked_at))

    def test_historical_attestation_covers_exact_fixture_components(self) -> None:
        self._historical_authority_fixture()
        attestation = migration.create_contamination_attestation(self.repo, migration_id=self.migration_id, operator="operator")
        self.assertEqual(len(attestation["components"]), 4)
        self.assertEqual(attestation["eligibility"], "PROVEN_NON_PRODUCTION_CONTAMINATION")
        status = migration.contaminated_prewrite_status(self.repo, migration_id=self.migration_id)
        self.assertTrue(status["eligible"])
        self.assertEqual(status["historical_contamination_attestation"]["attestation_id"], attestation["attestation_id"])

    def test_attestation_fingerprint_and_partial_coverage_drift_fail_closed(self) -> None:
        self._historical_authority_fixture()
        attestation = migration.create_contamination_attestation(self.repo, migration_id=self.migration_id, operator="operator")
        path = migration.contamination_attestation_path(self.migration_id)
        partial = {**attestation, "components": attestation["components"][:-1]}
        migration._atomic_json(path, partial)
        with self.assertRaisesRegex(migration.CutoverError, "CONTAMINATION_PROVENANCE_UNRESOLVED"):
            migration.contaminated_prewrite_status(self.repo, migration_id=self.migration_id)
        path.unlink()
        migration._atomic_json(path, attestation)
        with sqlite3.connect(self.central) as connection:
            connection.execute("CREATE TABLE attestation_drift (value TEXT)")
        with self.assertRaisesRegex(migration.CutoverError, "CONTAMINATION_PROVENANCE_UNRESOLVED"):
            migration.contaminated_prewrite_status(self.repo, migration_id=self.migration_id)

    def test_attestation_legacy_drift_fails_closed(self) -> None:
        self._historical_authority_fixture()
        migration.create_contamination_attestation(self.repo, migration_id=self.migration_id, operator="operator")
        with sqlite3.connect(self.legacy) as connection:
            connection.execute("CREATE TABLE legacy_attestation_drift (value TEXT)")
        with self.assertRaisesRegex(migration.CutoverError, "LEGACY_BASELINE_MISMATCH"):
            migration.contaminated_prewrite_status(self.repo, migration_id=self.migration_id)

    def test_attestation_pointer_drift_fails_closed(self) -> None:
        self._historical_authority_fixture()
        migration.create_contamination_attestation(self.repo, migration_id=self.migration_id, operator="operator")
        pointer_path = migration.authority_pointer_path()
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["forensic_test_pointer_drift"] = True
        migration._atomic_json(pointer_path, pointer)
        with self.assertRaisesRegex(migration.CutoverError, "CONTAMINATION_PROVENANCE_UNRESOLVED"):
            migration.contaminated_prewrite_status(self.repo, migration_id=self.migration_id)

    def test_attestation_delta_drift_and_production_precedence_fail_closed(self) -> None:
        self._historical_authority_fixture()
        migration.create_contamination_attestation(self.repo, migration_id=self.migration_id, operator="operator")
        with sqlite3.connect(self.central) as connection:
            connection.execute(
                "INSERT INTO local_api_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)",
                ("orphan-authority", "orphan", "project", b"v" * 32, b"f" * 32, "2026-08-31 12:45:00"),
            )
        with self.assertRaisesRegex(migration.CutoverError, "CONTAMINATION_PROVENANCE_UNRESOLVED"):
            migration.contaminated_prewrite_status(self.repo, migration_id=self.migration_id)

    def test_valid_attestation_cannot_override_managed_production_write(self) -> None:
        self._historical_authority_fixture()
        migration.create_contamination_attestation(self.repo, migration_id=self.migration_id, operator="operator")
        self._submission(self.central, "managed", "HUMAN")
        with self.assertRaisesRegex(migration.CutoverError, "LEGITIMATE_CENTRAL_WRITE_PRESENT"):
            migration.contaminated_prewrite_status(self.repo, migration_id=self.migration_id)

    def test_weak_historical_fixture_is_rejected(self) -> None:
        with sqlite3.connect(self.central) as connection:
            connection.execute("INSERT INTO local_api_consumer_registrations(consumer_id,project_id,status,created_at,updated_at) VALUES(?,?,?,?,?)", ("similar", "values", "ACTIVE", "2026-01-01", "2026-01-01"))
        with self.assertRaisesRegex(migration.CutoverError, "CONTAMINATION_PROVENANCE_UNRESOLVED"):
            migration.create_contamination_attestation(self.repo, migration_id=self.migration_id, operator="operator")
