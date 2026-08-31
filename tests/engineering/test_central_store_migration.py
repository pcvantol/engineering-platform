"""Focused read-only qualification for central-store migration preflight."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering import central_store_migration as migration
from tools.engineering.storage import open_storage


class CentralStoreMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.source = self.root / ".engineering" / "engineering.db"
        with open_storage(self.root) as connection:
            connection.execute("INSERT INTO local_api_consumer_registrations(consumer_id,project_id,status,created_at,updated_at) VALUES(?,?,?,?,?)", ("workspace-client", "project-alpha", "ACTIVE", "now", "now"))
            connection.execute("INSERT INTO local_api_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)", ("credential-alpha", "workspace-client", "project-alpha", b"v" * 32, b"f" * 32, "now"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _preflight(self) -> dict[str, object]:
        data_root = Path(self.temporary.name) / "installation"
        with patch.object(migration, "installation_data_root", return_value=data_root), patch.object(migration, "central_store_path", return_value=data_root / "engineering.db"):
            return migration.preflight(self.root)

    def test_data_root_is_portable_and_central_path_is_deterministic(self) -> None:
        self.assertEqual(migration.installation_data_root().name, "Engineering Platform")
        self.assertEqual(migration.central_store_path().name, "engineering.db")

    def test_discovery_cardinality_is_fail_closed(self) -> None:
        self.assertEqual(migration.discover_legacy_stores(self.root.parent / "missing"), ())
        absent = migration.preflight(self.root.parent / "missing")
        self.assertEqual(absent["blocking_codes"], ["LEGACY_STORE_NOT_FOUND"])
        extra = Path(self.temporary.name) / "other"
        with open_storage(extra):
            pass
        discovered = migration.discover_legacy_stores(self.root, extra_runtime_roots=(extra,))
        self.assertEqual(len(discovered), 2)
        ambiguous = migration.preflight(self.root, extra_runtime_roots=(extra,))
        self.assertEqual(ambiguous["blocking_codes"], ["LEGACY_STORE_AMBIGUOUS"])
        result = self._preflight()
        self.assertEqual(len(result["source_candidates"]), 1)

    def test_target_states_are_never_repaired(self) -> None:
        target = Path(self.temporary.name) / "target.db"
        self.assertEqual(migration.classify_target(target)["state"], "ABSENT")
        target.touch()
        self.assertEqual(migration.classify_target(target)["state"], "EMPTY_NEW")
        target.unlink()
        with open_storage(target.parent / "target-root"):
            pass
        compatible = target.parent / "target-root" / ".engineering" / "engineering.db"
        self.assertEqual(migration.classify_target(compatible)["state"], "COMPATIBLE_EXISTING")
        conflicting = target.parent / "conflicting.db"
        with sqlite3.connect(conflicting) as connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
        self.assertEqual(migration.classify_target(conflicting)["state"], "CONFLICTING_EXISTING")
        target.write_bytes(b"not sqlite")
        self.assertEqual(migration.classify_target(target)["state"], "CORRUPT_UNREADABLE")

    def test_schema_integrity_and_required_structures_are_inspected(self) -> None:
        candidate = migration.discover_legacy_stores(self.root)[0]
        facts = migration.inspect_source(candidate)
        self.assertEqual(facts["schema_version"], 40)
        self.assertEqual(facts["integrity"], "PASS")
        self.assertFalse(facts["blocking_codes"])
        wrong = Path(self.temporary.name) / "wrong.db"
        sqlite3.connect(wrong).close()
        wrong_facts = migration.inspect_source(migration.StoreCandidate(str(wrong), str(wrong), ("test",)))
        self.assertIn("SOURCE_SCHEMA_MISMATCH", wrong_facts["blocking_codes"])
        self.assertIn("SOURCE_INTEGRITY_FAILED", wrong_facts["blocking_codes"])

    def test_quiescence_blocks_active_transaction_lease_recovery_and_lock(self) -> None:
        with sqlite3.connect(self.source) as connection:
            connection.execute("INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)", ("run-active", "{}", "RUNNING", "now"))
            connection.execute("INSERT INTO execution_run_leases(lease_id,run_id,host_identity,host_instance_id,acquired_at,last_heartbeat_at,expires_at,lease_state,lease_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("lease-active", "run-active", "host", "instance", "now", "now", "later", "ACTIVE", 1, "now", "now"))
            connection.execute("INSERT INTO provider_recovery_attempts(run_id,recovery_ordinal,maximum_attempts,triggering_invocation_id,replacement_invocation_id,lifecycle_phase,state,requested_at) VALUES(?,?,?,?,?,?,?,?)", ("run-active", 1, 1, "invocation-old", "invocation-new", "PROVIDER_EXECUTION", "RECOVERY_AVAILABLE", "now"))
        facts = migration.inspect_quiescence(self.source)
        self.assertIn("ACTIVE_EXECUTION", facts["blocking_codes"])
        self.assertIn("ACTIVE_LEASE", facts["blocking_codes"])
        self.assertIn("AUTHORITY_HANDOFF_NOT_SAFE", facts["blocking_codes"])

    def test_backup_snapshot_project_inventory_and_redaction_are_safe(self) -> None:
        candidate = migration.discover_legacy_stores(self.root)[0]
        identity = migration.source_identity(candidate)
        backup = migration.backup_readiness(identity, Path(self.temporary.name) / "installation")
        self.assertTrue(backup["ready"])
        self.assertNotIn("v" * 32, str(backup))
        snapshot = migration.snapshot_plan(migration.inspect_source(candidate))
        self.assertEqual(snapshot["strategy"], "sqlite_backup_api")
        inventory = migration.project_scope_inventory(self.source)
        self.assertEqual(inventory["project_ids"], ["project-alpha"])
        self.assertEqual(inventory["credential_scopes"], 1)
        self.assertFalse(inventory["plaintext_credential_columns"])

    def test_wal_is_reported_without_checkpoint_or_sidecar_mutation(self) -> None:
        wal = Path(self.temporary.name) / "wal.db"
        with sqlite3.connect(wal) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE facts (id INTEGER)")
        candidate = migration.StoreCandidate(str(wal), str(wal.resolve()), ("test",))
        plan = migration.snapshot_plan(migration.inspect_source(candidate))
        self.assertEqual(plan["checkpoint_requirement"], "PRAGMA wal_checkpoint(TRUNCATE) by the sole controlled writer before copy")
        self.assertTrue(plan["raw_copy_forbidden"])

    def test_equivalence_and_preflight_do_not_mutate_source(self) -> None:
        before = (hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source.stat().st_mtime_ns)
        result = self._preflight()
        after = (hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source.stat().st_mtime_ns)
        self.assertTrue(result["eligible"])
        self.assertFalse((Path(self.temporary.name) / "installation" / "engineering.db").exists())
        self.assertEqual(result["migration_id"], self._preflight()["migration_id"])
        self.assertEqual(before, after)
        target = Path(self.temporary.name) / "candidate.db"
        with sqlite3.connect(self.source) as source, sqlite3.connect(target) as copied:
            source.backup(copied)
        self.assertTrue(migration.validate_target_equivalence(self.source, target)["equivalent"])
        with sqlite3.connect(target) as connection:
            connection.execute("DELETE FROM local_api_consumer_registrations")
        compared = migration.validate_target_equivalence(self.source, target)
        self.assertFalse(compared["equivalent"])
        self.assertIn("TARGET_STORE_CONFLICT", compared["blocking_codes"])
