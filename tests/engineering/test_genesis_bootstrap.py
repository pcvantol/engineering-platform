"""Owning finite Genesis tests; isolated synthetic owner grants, never user repos."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import os
import subprocess
import sys
import time
from unittest.mock import patch
import unittest

from engineering_platform import genesis_authority as authority, genesis_contract as contract, server
from engineering_platform import genesis_service as service

FIXTURE = Path(__file__).parent / "fixtures/genesis_local/authority-bundle.json"


class GenesisOwnerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ep-genesis-source-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "server"
        self.instance = server.initialize(self.data).instance_id
        service.initialize(self.data, expected_instance=self.instance)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.bundle = json.loads(FIXTURE.read_text())
        self.validator = self.root / "trusted-validator.py"
        self.validator.write_text(self.bundle["files"][contract.BASE_PATH+"validate.py"]["utf8"])
        self.original_request = {"contract_version": "1.0", "operation_id": "synthetic-operation", "target_ref": "owner-target",
            "effect_plan_binding": {"plan_id": "synthetic-plan", "plan_revision": 1, "plan_digest": "1"*64,
                "decision_id": "synthetic-effect-decision", "decision_digest": "2"*64,
                "proposal_id": "original-r50", "proposal_digest": "3"*64,
                "source_instance_id": "synthetic-forge", "source_actor_id": "synthetic-actor", "context_id": "synthetic-context"},
            "bundle": self.bundle}
        self.config = {"grant_id": "synthetic-grant", "issuance_id": "synthetic-issue", "consumer_id": "forge",
            "principal": "synthetic-service", "target_name": "new-repository", "workspace_root": str(self.workspace),
            "validator_path": str(self.validator), "request": self.original_request, "ttl_seconds": 900}
        self.grant = authority.issue(self.data, expected_instance=self.instance, config=self.config)
        self.headers = dict(zip(authority.HEADERS, ("1.0", self.instance, authority.host_id(), "forge", "synthetic-service", "genesis-bootstrap")))

    def _create(self, scope):
        from engineering_platform import genesis_worker as worker
        from engineering_platform.preproject_inspection import directory
        with directory(Path(scope["staging_path"]).parent) as fd:
            return worker.create(scope, prepared_parent_fd=fd)

    def _git_initialize(self, scope, known, files):
        from engineering_platform import genesis_worker as worker
        from engineering_platform.preproject_inspection import directory
        with directory(Path(scope["git_staging_path"]).parent) as fd:
            return worker.git_initialize(scope, known, files, prepared_parent_fd=fd)

    def test_real_finite_owner_scope_uses_original_R50_identity_and_bytes(self):
        original, rendered = contract.request(self.original_request)
        self.assertEqual(rendered.bundle_digest, "0c2c0fb41a68793be0960bb4289d7590060c8092ebb976852e1c4674b0f66b1d")
        self.assertEqual(len(rendered.files), 37)
        with authority.store(self.data) as db:
            grant_id, scope = authority.authenticate(db, self.grant["credential"], self.headers)
        self.assertEqual(grant_id, "synthetic-grant")
        self.assertEqual(scope["project_id"], "sample-product")
        self.assertFalse((self.workspace / "new-repository").exists())

    def test_changed_stored_scope_cannot_reauthorize_another_target(self):
        with authority.store(self.data, write=True) as db:
            old = db.execute("SELECT scope FROM ep_genesis_grants WHERE grant_id='synthetic-grant'").fetchone()[0]
            changed = json.loads(old)
            changed["target_name"] = "foreign-target"
            db.execute("UPDATE ep_genesis_grants SET scope=? WHERE grant_id='synthetic-grant'", (contract.canonical(changed).decode(),))
        with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_GRANT_CORRUPT"):
            with authority.store(self.data) as db:
                authority.authenticate(db, self.grant["credential"], self.headers)
        self.assertFalse((self.workspace / "foreign-target").exists())

    def test_real_local_commit_uses_root_relative_manifest_from_pinned_metadata(self):
        from engineering_platform import genesis_worker as worker
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant["credential"], self.headers)
        _, original = contract.request(self.original_request)
        known = self._create(scope)
        known.update(worker.write_files(scope, known, original.files))
        known.update(self._git_initialize(scope, known, original.files))
        known.update(worker.prepare_commit(scope, known, original.files, 1792134000))
        known.update(worker.commit_object(scope, known))
        known.update(worker.commit_ref(scope, known))
        result = worker.verify_commit(scope, known, original.files)
        self.assertTrue(result["committed_declaration_verified"])
        self.assertEqual(set(result["committed_files"]), set(original.files))
        self.assertEqual(result["commit"], known["commit"])

    def _admitted_committed(self):
        from engineering_platform import genesis_worker as worker
        service.initialize(self.data, expected_instance=self.instance)
        service.admit(self.data, token=self.grant["credential"], headers=self.headers, value=self.original_request)
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant["credential"], self.headers)
        _, original = contract.request(self.original_request)
        known = self._create(scope)
        known.update(worker.write_files(scope, known, original.files))
        known.update(self._git_initialize(scope, known, original.files))
        known.update(worker.prepare_commit(scope, known, original.files, 1792134000))
        known.update(worker.commit_object(scope, known))
        known.update(worker.commit_ref(scope, known))
        known.update(worker.verify_commit(scope, known, original.files))
        with authority.store(self.data, write=True) as db:
            row = service._row(db, "synthetic-operation", self.grant["credential"])
            row["evidence"]["known"] = known
            row["evidence"]["completed_steps"] = list(service.STEPS)
            row["state"] = "EXECUTING"
            service._save(db, row, self.grant["credential"])
        return scope, known

    def test_changed_git_after_verification_cannot_register(self):
        self._admitted_committed()
        head = self.workspace / "new-repository/.git/HEAD"
        head.write_text("ref: refs/heads/foreign\n")
        with self.assertRaises(contract.BootstrapError):
            service.execute(self.data, operation_id="synthetic-operation", token=self.grant["credential"], headers=self.headers)
        with authority.store(self.data) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ep_project_registrations").fetchone()[0], 0)

    def test_unidentified_spawn_is_stopped_and_primary_failure_retained(self):
        service.initialize(self.data, expected_instance=self.instance)
        service.admit(self.data, token=self.grant["credential"], headers=self.headers, value=self.original_request)
        children = []
        original_popen = subprocess.Popen
        def spawned(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child
        try:
            with service._lock(self.data, "synthetic-operation") as lock_fd:
                with patch.object(service.genesis_native, "runtime", return_value=Path(sys.executable)), \
                     patch.object(service.genesis_native, "command", return_value=(sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()")), \
                     patch.object(service, "capture_process_identity", return_value=None), \
                     patch.object(service.subprocess, "Popen", side_effect=spawned):
                    with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_HOST_IDENTITY_UNAVAILABLE"):
                        service._execute_step(self.data, operation_id="synthetic-operation", step="PREFLIGHT",
                            token=self.grant["credential"], headers=self.headers, source_identity=None, lock_fd=lock_fd)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertEqual(len(list((self.data / "g/evidence").glob("*.stdout"))), 1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_changed_target_git_filter_never_executes_during_prepare(self):
        from engineering_platform import genesis_worker as worker
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant["credential"], self.headers)
        _, original = contract.request(self.original_request)
        known = self._create(scope)
        known.update(worker.write_files(scope, known, original.files))
        known.update(self._git_initialize(scope, known, original.files))
        root = self.workspace / "new-repository"
        marker = root / "forbidden-filter-effect"
        original_verify = worker.verify_files
        def race_after_verified(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            with (root / ".git/config").open("a") as config:
                config.write('[filter "probe"]\n\tclean = touch forbidden-filter-effect; cat\n')
            (root / ".gitattributes").write_text("* filter=probe\n")
            return result
        try:
            with patch.object(worker, "verify_files", side_effect=race_after_verified):
                worker.prepare_commit(scope, known, original.files, 1792134000)
        except contract.BootstrapError:
            pass
        self.assertFalse(marker.exists(), "Git clean filter executed after file/config guard")

    def test_new_target_replacement_is_never_adopted(self):
        from engineering_platform import genesis_worker as worker
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant["credential"], self.headers)
        original_open = os.open
        original_mkdir = os.mkdir
        swapped = []
        def replacement(path, flags, *args, **kwargs):
            if path == scope["target_name"] and "dir_fd" in kwargs and not swapped:
                parent = kwargs["dir_fd"]
                os.rename(path, path + ".original", src_dir_fd=parent, dst_dir_fd=parent)
                original_mkdir(path, 0o700, dir_fd=parent)
                swapped.append(True)
            return original_open(path, flags, *args, **kwargs)
        with patch.object(worker.os, "open", side_effect=replacement):
            with self.assertRaises(contract.BootstrapError):
                self._create(scope)
        self.assertTrue(swapped)
        self.assertEqual(list((self.workspace / "new-repository").iterdir()), [])

    def test_initialized_bootstrap_evidence_is_finitely_preserved_in_reset(self):
        from engineering_platform import central_operational_reset as reset
        service.initialize(self.data, expected_instance=self.instance)
        _, preserved, unknown, _ = reset._external_inventory(self.data, expected_installation_id=self.instance)
        self.assertNotIn("g", unknown)
        self.assertNotIn("g.identity.json", unknown)
        self.assertTrue(any(row["path"] == "g.identity.json" and row["effect"] == "PRESERVE" for row in preserved))
        (self.data / "g/unknown-business-file").write_text("foreign")
        _, _, unknown, _ = reset._external_inventory(self.data, expected_installation_id=self.instance)
        self.assertIn("g/unknown-business-file", unknown)

    def test_prepared_publication_refuses_existing_and_foreign_capabilities(self):
        from engineering_platform import genesis_worker as worker
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant["credential"], self.headers)
        target = self.workspace / scope["target_name"]
        target.mkdir(mode=0o700)
        (target / "foreign").write_text("preserved")
        with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_TARGET_EXISTS"):
            self._create(scope)
        self.assertEqual((target / "foreign").read_text(), "preserved")
        self.assertTrue(Path(scope["staging_path"]).exists())
        prepared = Path(scope["staging_path"])
        prepared.rename(prepared.with_name(prepared.name + ".original"))
        prepared.mkdir(mode=0o700)
        with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_PREPARED_CAPABILITY_CHANGED"):
            self._create(scope)

    def test_actual_pipe_upload_deadline_and_maintenance_lock(self):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(write_fd, "wb", buffering=0) as stream:
                before = time.monotonic()
                with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_HOST_DEADLINE"):
                    service._write(stream, b"x" * 524288, before + 0.05)
                self.assertLess(time.monotonic() - before, 1)
        finally:
            os.close(read_fd)
        self.assertFalse(service.maintenance_activity(self.data))
        with service._lock(self.data, "synthetic-operation"):
            self.assertTrue(service.maintenance_activity(self.data))
        self.assertFalse(service.maintenance_activity(self.data))

    def test_valid_current_committed_capability_registers_once(self):
        self._admitted_committed()
        service.execute(self.data, operation_id="synthetic-operation", token=self.grant["credential"], headers=self.headers)
        result = service.read(self.data, operation_id="synthetic-operation", token=self.grant["credential"], headers=self.headers)
        self.assertEqual(result["state"], "COMPLETE")
        service.execute(self.data, operation_id="synthetic-operation", token=self.grant["credential"], headers=self.headers)
        with authority.store(self.data) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ep_project_registrations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ep_local_repository_bindings").fetchone()[0], 1)

    def test_committed_genesis_has_clean_index_and_worktree(self):
        from engineering_platform import genesis_worker as worker
        scope, known = self._admitted_committed()
        self.assertEqual(worker._git(self.workspace / scope["target_name"], "status", "--porcelain=v1", known=known), b"")

    def test_foreign_git_metadata_is_not_adopted_at_creation(self):
        from engineering_platform import genesis_worker as worker
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant["credential"], self.headers)
        _, original = contract.request(self.original_request)
        known = self._create(scope)
        known.update(worker.write_files(scope, known, original.files))
        original_open = os.open
        swapped = []
        def replacement(path, flags, *args, **kwargs):
            if path == ".git" and "dir_fd" in kwargs and not swapped:
                parent = kwargs["dir_fd"]
                os.rename(path, ".git.original", src_dir_fd=parent, dst_dir_fd=parent)
                os.mkdir(path, 0o700, dir_fd=parent)
                swapped.append(True)
            return original_open(path, flags, *args, **kwargs)
        with patch.object(worker.os, "open", side_effect=replacement):
            with self.assertRaises(contract.BootstrapError):
                self._git_initialize(scope, known, original.files)
        self.assertTrue(swapped)
        self.assertFalse((self.workspace / scope["target_name"] / ".git/config").exists())

    def test_actual_stderr_flood_is_bounded_before_process_exit(self):
        service.admit(self.data, token=self.grant["credential"], headers=self.headers, value=self.original_request)
        code = "import os,time; os.write(2,b'x'*400000); time.sleep(20)"
        children = []
        original_popen = subprocess.Popen
        def spawned(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child
        before = time.monotonic()
        try:
            with service._lock(self.data, "synthetic-operation") as fd:
                with patch.object(service.genesis_native, "runtime", return_value=Path(sys.executable)), \
                     patch.object(service.genesis_native, "command", return_value=(sys.executable, "-I", "-c", code)), \
                     patch.object(service, "capture_process_identity", side_effect=lambda pid, group: service.ProcessIdentity(pid, group, "own-supervised-test", "own-supervised-test")), \
                     patch.object(service, "verify_process_identity", return_value="MATCH"), \
                     patch.object(service.subprocess, "Popen", side_effect=spawned):
                    with self.assertRaises(contract.BootstrapError) as found:
                        service._execute_step(self.data, operation_id="synthetic-operation", step="PREFLIGHT",
                            token=self.grant["credential"], headers=self.headers, source_identity=None, lock_fd=fd)
            primary = found.exception.primary if isinstance(found.exception, service.CleanupFailure) else found.exception
            self.assertEqual(primary.code, "BOOTSTRAP_HOST_ERROR_OUTPUT_LIMIT")
            self.assertLess(time.monotonic() - before, 5)
            self.assertIsNotNone(children[0].poll())
            errors = list((self.data / "g/evidence").glob("*.stderr"))
            self.assertEqual([f.stat().st_size for f in errors], [262144])
            with authority.store(self.data) as db:
                row = service._row(db, "synthetic-operation", self.grant["credential"])
                self.assertEqual(row["evidence"]["completed_steps"], [])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_primary_and_evidence_io_failures_are_both_retained(self):
        service.admit(self.data, token=self.grant["credential"], headers=self.headers, value=self.original_request)
        original_open = Path.open
        injected = FileExistsError("synthetic exclusive stdout evidence collision")
        def fail_stdout(path, *args, **kwargs):
            if path.name.endswith(".stdout"):
                raise injected
            return original_open(path, *args, **kwargs)
        with service._lock(self.data, "synthetic-operation") as fd:
            with patch.object(service.genesis_native, "runtime", return_value=Path(sys.executable)), \
                 patch.object(service.genesis_native, "command", return_value=(sys.executable, "-I", "-c", "import sys;sys.stdin.buffer.read()")), \
                 patch.object(service, "capture_process_identity", return_value=None), \
                 patch.object(Path, "open", fail_stdout):
                with self.assertRaises(service.CleanupFailure) as found:
                    service._execute_step(self.data, operation_id="synthetic-operation", step="PREFLIGHT",
                        token=self.grant["credential"], headers=self.headers, source_identity=None, lock_fd=fd)
        self.assertEqual(found.exception.primary.code, "BOOTSTRAP_HOST_IDENTITY_UNAVAILABLE")
        self.assertIn(injected, found.exception.cleanup_errors)
        self.assertIs(found.exception.__cause__, found.exception.primary)

    def test_completed_parent_does_not_hide_an_active_or_uncertain_group(self):
        record = service.ProcessIdentity(999999, 999999, "synthetic", "synthetic")
        from dataclasses import asdict
        with patch.object(service, "verify_process_identity", return_value="NOT_ACTIVE"), \
             patch.object(service.os, "killpg", return_value=None):
            with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_PRIOR_HOST_GROUP_UNCERTAIN"):
                service._active_previous({"evidence": {"attempts": [{"process": asdict(record), "state": "STEP_COMPLETED"}]}})

    def test_colon_operation_lock_is_classified_but_unknown_lock_is_not(self):
        from engineering_platform import central_operational_reset as reset
        with service._lock(self.data, "bootstrap:one"):
            _, preserved, unknown, _ = reset._external_inventory(self.data, expected_installation_id=self.instance)
            self.assertNotIn("g/locks/bootstrap:one.lock", unknown)
            self.assertTrue(any(row["path"] == "g/locks/bootstrap:one.lock" for row in preserved))
        (self.data / "g/locks/unknown!.lock").write_text("foreign")
        _, _, unknown, _ = reset._external_inventory(self.data, expected_installation_id=self.instance)
        self.assertIn("g/locks/unknown!.lock", unknown)

    def test_nested_foreign_directory_is_never_written(self):
        from engineering_platform import genesis_worker as worker
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant["credential"], self.headers)
        _, original = contract.request(self.original_request)
        known = self._create(scope)
        original_stat = os.stat
        swapped = []
        def replacement(path, *args, **kwargs):
            if path == ".engineering-platform" and "dir_fd" in kwargs and not swapped:
                parent = kwargs["dir_fd"]
                os.rename(path, path + ".original", src_dir_fd=parent, dst_dir_fd=parent)
                os.mkdir(path, 0o700, dir_fd=parent)
                swapped.append(True)
            return original_stat(path, *args, **kwargs)
        with patch.object(worker.os, "stat", side_effect=replacement):
            try:
                worker.write_files(scope, known, original.files)
            except contract.BootstrapError:
                pass
        self.assertTrue(swapped)
        self.assertFalse((self.workspace / scope["target_name"] / ".engineering-platform/repository.json").exists())

    def test_post_init_metadata_cannot_replace_the_original_claim(self):
        from engineering_platform import genesis_worker as worker
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant["credential"], self.headers)
        _, original = contract.request(self.original_request)
        known = self._create(scope)
        known.update(worker.write_files(scope, known, original.files))
        real_git = worker._git
        root = self.workspace / scope["target_name"]
        def replaced_after_init(*args, **kwargs):
            result = real_git(*args, **kwargs)
            if "init" in args:
                original_config = (root / ".git/config").read_bytes()
                (root / ".git").rename(root / ".git.original")
                (root / ".git").mkdir(mode=0o700)
                (root / ".git/config").write_bytes(original_config)
            return result
        with patch.object(worker, "_git", side_effect=replaced_after_init):
            with self.assertRaises(contract.BootstrapError):
                self._git_initialize(scope, known, original.files)

    def test_stale_foreign_registered_path_is_refused_before_admission(self):
        from engineering_platform import project_topology, local_repository_binding
        declaration = json.loads(self.bundle["files"][".engineering-platform/repository.json"]["utf8"])
        declaration["project"]["id"] = "foreign-product"
        declaration["project"]["authority_repository_id"] = "foreign-core"
        declaration["repository"]["id"] = "foreign-core"
        target = self.workspace / "new-repository"
        (target / ".engineering-platform").mkdir(mode=0o700, parents=True)
        (target / ".engineering-platform/repository.json").write_bytes(contract.canonical(declaration))
        with authority.store(self.data, write=True) as db:
            project_topology.register_server_local_topology(db, declaration=declaration)
            local_repository_binding.bind_local_repository(db, project_id="foreign-product", repository_id="foreign-core", local_root=target, data_root=self.data)
        target.rename(self.workspace / "foreign-original")
        with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_TARGET_ALREADY_BOUND"):
            service.admit(self.data, token=self.grant["credential"], headers=self.headers, value=self.original_request)
        self.assertFalse(target.exists())
        with authority.store(self.data) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ep_genesis_operations").fetchone()[0], 0)

    def test_cli_service_readback_keeps_initial_physical_source_through_publication(self):
        service.admit(self.data, token=self.grant["credential"], headers=self.headers, value=self.original_request)
        database = self.data / "epdata.sqlite"
        def publish(result, instance, check):
            original_bytes = database.read_bytes()
            database.rename(self.data / "epdata-original.sqlite")
            database.write_bytes(original_bytes)
            with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_SOURCE_CHANGED"):
                check()
        service.operation(self.data, token=self.grant["credential"], headers=self.headers,
            operation_id="synthetic-operation", publish=publish)
        self.assertFalse((self.workspace / "new-repository").exists())

    def test_corrupt_operation_cannot_claim_complete_without_commit_or_B8R(self):
        service.initialize(self.data, expected_instance=self.instance)
        service.admit(self.data, token=self.grant["credential"], headers=self.headers, value=self.original_request)
        with authority.store(self.data, write=True) as db:
            db.execute("UPDATE ep_genesis_operations SET state='COMPLETE' WHERE operation_id='synthetic-operation'")
        with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_OPERATION_CORRUPT"):
            service.read(self.data, operation_id="synthetic-operation", token=self.grant["credential"], headers=self.headers)
        with authority.store(self.data) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ep_repository_registrations").fetchone()[0], 0)
        self.assertFalse((self.workspace / "new-repository").exists())


if __name__ == "__main__":
    unittest.main()
