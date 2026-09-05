"""Focused read-only qualification for central-store migration preflight."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import central_store_migration as migration
from engineering_platform import storage
from engineering_platform.storage import open_storage


class CentralStoreMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self._authority_pointer = Path(self.temporary.name) / "authority" / "store-authority.json"
        self._authority_pointer_patch = patch.object(storage, "_authority_pointer_path", return_value=self._authority_pointer)
        self._authority_pointer_patch.start()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.source = self.root / ".engineering" / "engineering.db"
        with open_storage(self.root) as connection:
            connection.execute("INSERT INTO local_api_consumer_registrations(consumer_id,project_id,status,created_at,updated_at) VALUES(?,?,?,?,?)", ("workspace-client", "project-alpha", "ACTIVE", "now", "now"))
            connection.execute("INSERT INTO local_api_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)", ("credential-alpha", "workspace-client", "project-alpha", b"v" * 32, b"f" * 32, "now"))

    def tearDown(self) -> None:
        self._authority_pointer_patch.stop()
        self.temporary.cleanup()

    def _preflight(self) -> dict[str, object]:
        data_root = Path(self.temporary.name) / "installation"
        with patch.object(migration, "installation_data_root", return_value=data_root), patch.object(migration, "central_store_path", return_value=data_root / "engineering.db"):
            return migration.preflight(self.root)

    def _cutover_environment(self):
        data_root = Path(self.temporary.name) / "installation"
        pointer = data_root / "runtime" / "store-authority.json"

        def resolve(_repo: Path) -> Path:
            if pointer.exists():
                return data_root / "engineering.db"
            return self.source

        return data_root, patch.object(migration, "installation_data_root", return_value=data_root), patch.object(
            migration, "central_store_path", return_value=data_root / "engineering.db"
        ), patch.object(migration, "database_path", side_effect=resolve)

    def _held_lock(self, name: str, component: str, *, process_id: int | None = None):
        path = self.source.parent / "locks" / name
        path.parent.mkdir(exist_ok=True)
        handle = path.open("w+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        json.dump({"component": component, "pid": process_id if process_id is not None else os.getpid()}, handle)
        handle.flush()
        return handle

    @contextmanager
    def _services_restarted_receipt(self):
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()

        class Services:
            def __init__(self) -> None:
                self.ready = True

            def running(self, _label: str) -> bool:
                return self.ready

            def stop(self, _label: str) -> None:
                pass

            def stopped(self, _label: str) -> bool:
                return True

            def start(self, _label: str) -> None:
                pass

        services = Services()
        with root_patch, target_patch, resolver_patch:
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            receipt = migration.controlled_cutover(self.root, services=services)
            self.assertEqual(receipt["state"], "SERVICES_RESTARTED")
            yield data_root, services, receipt

    def test_data_root_is_portable_and_central_path_is_deterministic(self) -> None:
        self.assertEqual(migration.installation_data_root().name, "Engineering Platform")
        self.assertEqual(migration.central_store_path().name, "engineering.db")

    def test_platform_data_roots_honor_xdg_contract(self) -> None:
        with patch.object(migration.sys, "platform", "linux"), \
             patch.dict(os.environ, {"XDG_DATA_HOME": "/portable/data"}, clear=False):
            self.assertEqual(migration.user_data_dir("EP"), Path("/portable/data/EP"))

    def test_launchagent_control_fails_closed_when_launchd_cannot_confirm_state(self) -> None:
        control = migration.LaunchAgentServiceControl(uid=501)
        label = "com.example.engineering"
        with patch.object(control._launchd, "quiesce", side_effect=OSError("offline")):
            with self.assertRaisesRegex(migration.CutoverError, "SERVICE_STOP_FAILED"):
                control.stop(label)
        with patch.object(control._launchd, "quiesce"), patch.object(control, "stopped", return_value=False):
            with self.assertRaisesRegex(migration.CutoverError, "SERVICE_STOP_FAILED"):
                control.stop(label)
        with patch.object(control._launchd, "resume", side_effect=OSError("offline")):
            with self.assertRaisesRegex(migration.CutoverError, "SERVICE_RESTART_FAILED"):
                control.start(label)
        with patch.object(control._launchd, "resume"), patch.object(control, "running", return_value=False):
            with self.assertRaisesRegex(migration.CutoverError, "SERVICE_RESTART_FAILED"):
                control.start(label)
        with patch("engineering_platform.central_store_migration.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "state = running", "")):
            self.assertTrue(control.running(label))
        with patch("engineering_platform.central_store_migration.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "state = stopped", "")):
            self.assertFalse(control.running(label))

    def test_corrupt_source_and_target_are_never_accepted_as_migratable(self) -> None:
        corrupt = Path(self.temporary.name) / "corrupt.db"
        corrupt.write_text("not a sqlite database", encoding="utf-8")
        candidate = migration.StoreCandidate(str(corrupt), str(corrupt.resolve()), ("test",))
        inspected = migration.inspect_source(candidate)
        self.assertIn("SOURCE_INTEGRITY_FAILED", inspected["blocking_codes"])
        self.assertEqual(migration.classify_target(corrupt)["state"], "CORRUPT_UNREADABLE")

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
        self.assertNotIn("migration_id", result)
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

    def test_operator_freeze_is_durable_and_snapshot_copy_is_equivalent(self) -> None:
        migration_id = "migration-test"
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()
        with root_patch, target_patch, resolver_patch:
            frozen = migration.set_admission_freeze(self.root, migration_id=str(migration_id), reason="controlled test")
            self.assertEqual(frozen["state"], "ACTIVE")
            self.assertEqual(migration.admission_status(self.root)["migration_id"], migration_id)
            self.assertEqual(migration.load_receipt(str(migration_id))["state"], "ADMISSION_FROZEN")
        copy = Path(self.temporary.name) / "copy.db"
        migration.copy_snapshot(self.source, copy)
        self.assertTrue(migration.validate_target_equivalence(self.source, copy)["equivalent"])
        thawed = migration.thaw_admission(self.root, migration_id=str(migration_id))
        self.assertEqual(thawed["state"], "INACTIVE")

    def test_pre_stop_allows_only_verified_running_dashboard_and_watcher_locks(self) -> None:
        dashboard = self._held_lock("dashboard.lock", "dashboard", process_id=101)
        watcher = self._held_lock("inbox-watcher.lock", "inbox-watcher", process_id=102)

        class Services:
            def running(self, _label: str) -> bool:
                return True

        try:
            with patch.object(migration, "_process_command", side_effect=lambda pid: "python -m engineering_platform.dashboard run" if pid == 101 else "python -m engineering_platform.inbox_watcher run"):
                facts = migration.inspect_quiescence(self.source, pre_stop=True, services=Services())
            self.assertTrue(facts["eligible"])
            self.assertEqual(facts["lock_classifications"]["dashboard.lock"], "EXPECTED_RUNNING_SERVICE_LOCK")
            self.assertEqual(facts["lock_classifications"]["inbox-watcher.lock"], "EXPECTED_RUNNING_SERVICE_LOCK")
        finally:
            fcntl.flock(dashboard.fileno(), fcntl.LOCK_UN)
            fcntl.flock(watcher.fileno(), fcntl.LOCK_UN)
            dashboard.close()
            watcher.close()

    def test_unexpected_pre_stop_lock_blocks_without_cutover_mutation(self) -> None:
        lock = self._held_lock("execution-host.lock", "execution-host")
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()

        class Services:
            stopped_labels: list[str] = []
            def running(self, _label: str) -> bool:
                return True
            def stop(self, label: str) -> None:
                self.stopped_labels.append(label)
            def stopped(self, _label: str) -> bool:
                return True

        try:
            with root_patch, target_patch, resolver_patch:
                migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
                services = Services()
                with self.assertRaises(migration.CutoverError) as error:
                    migration.controlled_cutover(self.root, services=services)
                self.assertEqual(error.exception.code, "QUIESCENCE_FAILED")
                self.assertEqual(services.stopped_labels, [])
                self.assertFalse((data_root / "engineering.db").exists())
                self.assertFalse((data_root / "runtime" / "store-authority.json").exists())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def test_pre_stop_identity_is_informational_and_not_the_copy_baseline(self) -> None:
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()
        with root_patch, target_patch, resolver_patch:
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            self.assertEqual(migration.admission_status(self.root)["migration_id"], "migration-a")
            with sqlite3.connect(self.source) as connection:
                connection.execute("INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)", ("unexpected", "{}", "COMPLETE", "now"))
            receipt = migration.controlled_cutover(self.root)
            self.assertEqual(receipt["state"], "AUTHORITY_SWITCHED")
            self.assertIn("pre_stop_source_identity", receipt)
            self.assertEqual(receipt["quiescent_source_baseline"]["state"], "QUIESCENT_SOURCE_BASELINE")

    def test_shutdown_write_precedes_the_authoritative_quiescent_baseline(self) -> None:
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()

        class Services:
            def __init__(self, source: Path) -> None:
                self.source = source
            def running(self, _label: str) -> bool:
                return True
            def stop(self, _label: str) -> None:
                with sqlite3.connect(self.source) as connection:
                    connection.execute(
                        "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                        ("inbox", '{"event":"watcher_shutdown_completed"}', "now"),
                    )
            def stopped(self, _label: str) -> bool:
                return True
            def start(self, _label: str) -> None:
                pass

        with root_patch, target_patch, resolver_patch:
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            before = migration.load_receipt("migration-a")["pre_stop_source_identity"]["fingerprint_sha256"]
            receipt = migration.controlled_cutover(self.root, services=Services(self.source))
            baseline = receipt["quiescent_source_baseline"]
            self.assertEqual(receipt["state"], "SERVICES_RESTARTED")
            self.assertNotEqual(before, baseline["source"]["fingerprint_sha256"])
            self.assertEqual(baseline["state"], "QUIESCENT_SOURCE_BASELINE")
            self.assertIn("critical_table_counts", baseline)
            self.assertEqual(baseline["project_scope"]["consumer_registrations"], 1)

    def test_post_baseline_mutation_blocks_before_backup(self) -> None:
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()
        original_target_state = migration.classify_target

        def mutate_before_backup(path: Path) -> dict[str, object]:
            with sqlite3.connect(self.source) as connection:
                connection.execute("INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)", ("rogue", "{}", "COMPLETE", "now"))
            return original_target_state(path)

        with root_patch, target_patch, resolver_patch, patch.object(migration, "classify_target", side_effect=mutate_before_backup):
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            with self.assertRaises(migration.CutoverError) as error:
                migration.controlled_cutover(self.root)
            self.assertEqual(error.exception.code, "SOURCE_CHANGED_AFTER_PREFLIGHT")
            self.assertEqual(migration.load_receipt("migration-a")["state"], "QUIESCENT_SOURCE_BASELINE")
            self.assertFalse((data_root / "backups").exists())

    def test_resume_reuses_persisted_quiescent_baseline_without_rebaselining(self) -> None:
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()
        with root_patch, target_patch, resolver_patch:
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            receipt = migration.load_receipt("migration-a")
            baseline = migration.quiescent_source_baseline(migration.discover_legacy_stores(self.root)[0])
            migration.transition_receipt(receipt, "QUIESCENT_SOURCE_BASELINE", source=baseline["source"], quiescent_source_baseline=baseline)
            with sqlite3.connect(self.source) as connection:
                connection.execute("INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)", ("rogue", "{}", "COMPLETE", "now"))
            with self.assertRaises(migration.CutoverError) as error:
                migration.controlled_cutover(self.root)
            self.assertEqual(error.exception.code, "SOURCE_CHANGED_AFTER_PREFLIGHT")
            self.assertEqual(
                migration.load_receipt("migration-a")["quiescent_source_baseline"]["source"]["fingerprint_sha256"],
                baseline["source"]["fingerprint_sha256"],
            )

    def test_conflicting_freeze_is_rejected(self) -> None:
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()
        with root_patch, target_patch, resolver_patch:
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            with self.assertRaises(migration.CutoverError) as error:
                migration.set_admission_freeze(self.root, migration_id="migration-b", reason="conflict")
        self.assertEqual(error.exception.code, "ADMISSION_FREEZE_FAILED")

    def test_abort_pre_handoff_preserves_freeze_evidence_and_thaws_separately(self) -> None:
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()
        with root_patch, target_patch, resolver_patch:
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            aborted = migration.abort_pre_handoff(
                self.root,
                migration_id="migration-a",
                reason="PRE_HANDOFF_CONTROLLER_DEFECT",
                operator="operator-test",
            )
            self.assertEqual(aborted["state"], "ABORTED_PRE_HANDOFF")
            self.assertEqual(migration.admission_status(self.root)["state"], "ACTIVE")
            self.assertEqual(aborted["historical_freeze"]["migration_id"], "migration-a")
            self.assertEqual(aborted["abort"]["reason"], "PRE_HANDOFF_CONTROLLER_DEFECT")
            self.assertTrue(migration.abort_pre_handoff(self.root, migration_id="migration-a", reason="PRE_HANDOFF_CONTROLLER_DEFECT")["already_aborted"])
            thawed = migration.thaw_admission(self.root, migration_id="migration-a", operator="operator-test")
            self.assertEqual(thawed["state"], "INACTIVE")
            historical = migration.load_receipt("migration-a")
            self.assertEqual(historical["state"], "ABORTED_PRE_HANDOFF")
            self.assertEqual(historical["thaw"]["state"], "INACTIVE")
            self.assertFalse((data_root / "engineering.db").exists())
            self.assertFalse((data_root / "runtime" / "store-authority.json").exists())

    def test_abort_rejects_wrong_migration_and_post_handoff_state(self) -> None:
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()
        with root_patch, target_patch, resolver_patch:
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            with self.assertRaises(migration.CutoverError) as wrong:
                migration.abort_pre_handoff(self.root, migration_id="migration-b", reason="PRE_HANDOFF_CONTROLLER_DEFECT")
            self.assertEqual(wrong.exception.code, "ABORT_PRE_HANDOFF_FAILED")
            migration.write_authority_pointer(
                migration_id="migration-a", authority=self.source, legacy=self.source, state="AUTHORITY_SWITCHED"
            )
            with self.assertRaises(migration.CutoverError) as handoff:
                migration.abort_pre_handoff(self.root, migration_id="migration-a", reason="PRE_HANDOFF_CONTROLLER_DEFECT")
            self.assertEqual(handoff.exception.code, "ABORT_PRE_HANDOFF_FAILED")

    def test_abort_cli_requires_explicit_execute_and_cannot_be_prompt_driven(self) -> None:
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()
        with root_patch, target_patch, resolver_patch:
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            self.assertEqual(
                migration.main(["abort", "--repo", str(self.root), "--migration-id", "migration-a", "--reason", "PRE_HANDOFF_CONTROLLER_DEFECT"]),
                2,
            )
            self.assertEqual(migration.admission_status(self.root)["state"], "ACTIVE")
            self.assertEqual(
                migration.main(["abort", "--repo", str(self.root), "--migration-id", "migration-a", "--reason", "PRE_HANDOFF_CONTROLLER_DEFECT", "--execute", "--json"]),
                0,
            )
            self.assertEqual(migration.load_receipt("migration-a")["state"], "ABORTED_PRE_HANDOFF")

    def test_post_stop_remaining_lock_blocks_before_backup_or_authority_switch(self) -> None:
        dashboard = self._held_lock("dashboard.lock", "dashboard", process_id=101)
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()

        class Services:
            def running(self, _label: str) -> bool:
                return True
            def stop(self, _label: str) -> None:
                pass
            def stopped(self, _label: str) -> bool:
                return True

        try:
            with root_patch, target_patch, resolver_patch, patch.object(migration, "_process_command", return_value="python -m engineering_platform.dashboard run"):
                migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
                with self.assertRaises(migration.CutoverError) as error:
                    migration.controlled_cutover(self.root, services=Services())
                self.assertEqual(error.exception.code, "QUIESCENCE_FAILED")
                self.assertFalse((data_root / "backups").exists())
                self.assertFalse((data_root / "engineering.db").exists())
                self.assertFalse((data_root / "runtime" / "store-authority.json").exists())
        finally:
            fcntl.flock(dashboard.fileno(), fcntl.LOCK_UN)
            dashboard.close()

    def test_happy_path_reuses_freeze_migration_id_and_allows_expected_pre_stop_locks(self) -> None:
        dashboard = self._held_lock("dashboard.lock", "dashboard", process_id=101)
        watcher = self._held_lock("inbox-watcher.lock", "inbox-watcher", process_id=102)
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()

        class Services:
            def __init__(self) -> None:
                self.handles = {
                    "com.djconnect.engineering-dashboard": dashboard,
                    "com.djconnect.engineering-inbox": watcher,
                }
                self.stopped_labels: list[str] = []
                self.started_labels: list[str] = []
            def running(self, _label: str) -> bool:
                return True
            def stop(self, label: str) -> None:
                self.stopped_labels.append(label)
                handle = self.handles.get(label)
                if handle is not None:
                    # A real service exit closes its lock file as well as
                    # releasing the advisory lock.  Closing here keeps the
                    # strict post-stop gate independent of platform-specific
                    # same-process flock behaviour.
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    Path(handle.name).unlink(missing_ok=True)
                    handle.close()
            def stopped(self, _label: str) -> bool:
                return True
            def start(self, label: str) -> None:
                self.started_labels.append(label)

        try:
            def command(process_id: int) -> str:
                if process_id == 101:
                    return "python -m engineering_platform.dashboard run"
                return "python -m engineering_platform.inbox_watcher run"

            with root_patch, target_patch, resolver_patch, patch.object(migration, "_process_command", side_effect=command):
                migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
                services = Services()
                receipt = migration.controlled_cutover(self.root, services=services)
                self.assertEqual(receipt["migration_id"], "migration-a")
                self.assertEqual(receipt["state"], "SERVICES_RESTARTED")
                self.assertTrue((data_root / "engineering.db").is_file())
                self.assertEqual(migration.controlled_cutover(self.root, services=services)["migration_id"], "migration-a")
        finally:
            if not dashboard.closed:
                dashboard.close()
            if not watcher.closed:
                watcher.close()

    def test_stage_a_advances_from_services_restarted_only_after_all_readiness_gates(self) -> None:
        with self._services_restarted_receipt() as (_data_root, services, _receipt):
            completed = migration.complete_stage_a(
                self.root,
                migration_id="migration-a",
                services=services,
                desired_state_check=lambda _repo: True,
            )
        self.assertEqual(completed["state"], "LEGACY_ROLLBACK_COMPATIBLE")
        self.assertEqual(completed["readonly_qualification"]["authority"], "CENTRAL")
        self.assertEqual(completed["readonly_qualification"]["freeze"], "ACTIVE")
        self.assertEqual(completed["readonly_qualification"]["desired_state"], "MATCH")
        self.assertEqual(completed["readonly_qualification"]["central_managed_production_writes"], 0)

    def test_stage_a_readiness_failure_does_not_advance_the_receipt(self) -> None:
        with self._services_restarted_receipt() as (_data_root, services, _receipt):
            services.ready = False
            with self.assertRaises(migration.CutoverError) as error:
                migration.complete_stage_a(
                    self.root,
                    migration_id="migration-a",
                    services=services,
                    desired_state_check=lambda _repo: True,
                )
            self.assertEqual(error.exception.code, "POST_CUTOVER_READINESS_FAILED")
            self.assertEqual(migration.load_receipt("migration-a")["state"], "SERVICES_RESTARTED")

    def test_stage_a_mixed_binding_or_legacy_drift_fails_closed(self) -> None:
        with self._services_restarted_receipt() as (_data_root, services, _receipt):
            with patch.object(migration, "service_binding_proof", return_value={"consistent": False}):
                with self.assertRaises(migration.CutoverError) as binding:
                    migration.complete_stage_a(
                        self.root,
                        migration_id="migration-a",
                        services=services,
                        desired_state_check=lambda _repo: True,
                    )
            self.assertEqual(binding.exception.code, "POST_CUTOVER_READINESS_FAILED")
            with sqlite3.connect(self.source) as connection:
                connection.execute("INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)", ("legacy-drift", "{}", "COMPLETE", "now"))
            with self.assertRaises(migration.CutoverError) as drift:
                migration.complete_stage_a(
                    self.root,
                    migration_id="migration-a",
                    services=services,
                    desired_state_check=lambda _repo: True,
                )
        self.assertEqual(drift.exception.code, "POST_CUTOVER_READINESS_FAILED")

    def test_stage_a_requires_active_freeze(self) -> None:
        with self._services_restarted_receipt() as (_data_root, services, _receipt):
            migration.thaw_admission(self.root, migration_id="migration-a")
            with self.assertRaises(migration.CutoverError) as frozen:
                migration.complete_stage_a(
                    self.root,
                    migration_id="migration-a",
                    services=services,
                    desired_state_check=lambda _repo: True,
                )
            self.assertEqual(frozen.exception.code, "POST_CUTOVER_READINESS_FAILED")

    def test_stage_a_is_idempotent_after_completion(self) -> None:
        with self._services_restarted_receipt() as (_data_root, services, _receipt):
            completed = migration.complete_stage_a(
                self.root,
                migration_id="migration-a",
                services=services,
                desired_state_check=lambda _repo: True,
            )
            transitions = list(completed["transitions"])
            self.assertEqual(
                migration.complete_stage_a(self.root, migration_id="migration-a"),
                completed,
            )
            self.assertEqual(migration.load_receipt("migration-a")["transitions"], transitions)

    def test_stage_a_rejects_earlier_and_post_write_states(self) -> None:
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()
        with root_patch, target_patch, resolver_patch:
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            with self.assertRaises(migration.CutoverError) as earlier:
                migration.complete_stage_a(self.root, migration_id="migration-a", desired_state_check=lambda _repo: True)
            self.assertEqual(earlier.exception.code, "POST_CUTOVER_READINESS_FAILED")
        with self._services_restarted_receipt() as (_data_root, services, _receipt):
            migration.complete_stage_a(
                self.root,
                migration_id="migration-a",
                services=services,
                desired_state_check=lambda _repo: True,
            )
            migration.mark_central_post_write(self.root)
            with self.assertRaises(migration.CutoverError) as post_write:
                migration.complete_stage_a(self.root, migration_id="migration-a")
        self.assertEqual(post_write.exception.code, "POST_CUTOVER_READINESS_FAILED")

    def test_authority_pointer_is_atomic_and_bound_to_migration(self) -> None:
        root = Path(self.temporary.name) / "installation"
        with patch.object(migration, "installation_data_root", return_value=root):
            pointer = migration.write_authority_pointer(
                migration_id="migration-test", authority=self.source, legacy=self.source,
                state="AUTHORITY_SWITCHED",
            )
            self.assertEqual(pointer["migration_id"], "migration-test")
            loaded = __import__("json").loads((root / "runtime" / "store-authority.json").read_text())
            self.assertEqual(loaded["authoritative_path"], str(self.source.resolve()))

    def test_receipt_transitions_are_durable_monotonic_and_cannot_skip(self) -> None:
        root = Path(self.temporary.name) / "installation"
        receipt = {"migration_id": "migration-test"}
        with patch.object(migration, "installation_data_root", return_value=root):
            migration.transition_receipt(receipt, "PRECHECK")
            migration.transition_receipt(receipt, "ADMISSION_FROZEN")
            self.assertEqual(migration.load_receipt("migration-test")["state"], "ADMISSION_FROZEN")
            with self.assertRaises(migration.CutoverError) as error:
                migration.transition_receipt(receipt, "TARGET_VERIFIED")
        self.assertEqual(error.exception.code, "AUTHORITY_SWITCH_FAILED")

    def test_first_central_write_retires_direct_rollback(self) -> None:
        root = Path(self.temporary.name) / "installation"
        receipt = {"migration_id": "migration-test", "state": "LEGACY_ROLLBACK_COMPATIBLE"}
        with patch.object(migration, "installation_data_root", return_value=root):
            migration._atomic_json(migration.receipt_path("migration-test"), receipt)
            migration.write_authority_pointer(
                migration_id="migration-test", authority=self.source, legacy=self.source,
                state="LEGACY_ROLLBACK_COMPATIBLE",
            )
            migration.mark_central_post_write(self.root)
            self.assertEqual(migration.load_receipt("migration-test")["state"], "CENTRAL_STORE_ACTIVE_POST_WRITE")

    def test_service_binding_proof_uses_the_single_resolver(self) -> None:
        proof = migration.service_binding_proof(self.root, expected=self.source)
        self.assertTrue(proof["consistent"])
        self.assertEqual(set(proof["services"]), set(migration.SERVICE_STOP_ORDER))

    def test_cutover_service_control_uses_durable_quiesce_and_resume(self) -> None:
        control = migration.LaunchAgentServiceControl(uid=501)
        with patch.object(control._launchd, "quiesce") as quiesce, patch.object(
            control._launchd, "inspect", return_value=False
        ), patch.object(control._launchd, "resume") as resume, patch.object(control, "running", return_value=True):
            control.stop("com.djconnect.engineering-inbox")
            control.start("com.djconnect.engineering-inbox")
        expected = Path.home() / "Library" / "LaunchAgents" / "com.djconnect.engineering-inbox.plist"
        quiesce.assert_called_once_with("com.djconnect.engineering-inbox", expected)
        resume.assert_called_once_with("com.djconnect.engineering-inbox", expected)

    def test_service_stop_failure_blocks_before_target_or_backup(self) -> None:
        data_root, root_patch, target_patch, resolver_patch = self._cutover_environment()

        class Services:
            def running(self, _label: str) -> bool:
                return True
            def stop(self, _label: str) -> None:
                raise migration.CutoverError("SERVICE_STOP_FAILED")
            def stopped(self, _label: str) -> bool:
                return False

        with root_patch, target_patch, resolver_patch:
            migration.set_admission_freeze(self.root, migration_id="migration-a", reason="test")
            with self.assertRaises(migration.CutoverError) as error:
                migration.controlled_cutover(self.root, services=Services())
            self.assertEqual(error.exception.code, "SERVICE_STOP_FAILED")
            self.assertFalse((data_root / "backups").exists())
            self.assertFalse((data_root / "engineering.db").exists())
            self.assertFalse((data_root / "runtime" / "store-authority.json").exists())
