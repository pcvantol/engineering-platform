"""Original capability recovery and controller migration; synthetic owners only."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from engineering_platform import genesis_authority as authority, genesis_contract as contract
from engineering_platform import genesis_service as service, genesis_worker as worker
from tests.engineering import test_genesis_bootstrap as fixture


class OriginalGitRecoveryTests(unittest.TestCase):
    setUp = fixture.GenesisOwnerFixture.setUp
    _create = fixture.GenesisOwnerFixture._create
    _git_initialize = fixture.GenesisOwnerFixture._git_initialize

    def prepared(self):
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant['credential'], self.headers)
        files = contract.bundle(self.bundle).files
        known = self._create(scope)
        known.update(worker.write_files(scope, known, files))
        return scope, known, files

    def failed_original_publication(self):
        scope, known, files = self.prepared()
        with patch.object(worker, '_run_git', side_effect=subprocess.TimeoutExpired('fixed own init', 5)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self._git_initialize(scope, known, files)
        metadata = self.workspace/scope['target_name']/'.git'
        self.assertEqual(list(metadata.iterdir()), [])
        self.assertEqual([metadata.stat().st_dev,metadata.stat().st_ino], scope['git_staging_identity'])
        return scope, known, files, metadata

    def test_original_empty_capability_recovers_without_new_inode_or_documents(self):
        scope, known, files, metadata = self.failed_original_publication()
        original = metadata.stat().st_ino
        receipt = self._git_initialize(scope, known, files)
        self.assertTrue(receipt['recovered_original_empty_git_capability'])
        self.assertEqual(metadata.stat().st_ino, original)
        self.assertEqual(receipt['git_identity'], scope['git_staging_identity'])
        worker.verify_files(scope, known, files, git=True)
        self.assertFalse(Path(scope['git_staging_path']).exists())

    def test_foreign_empty_inode_is_never_adopted(self):
        scope, known, files = self.prepared()
        metadata = self.workspace/scope['target_name']/'.git'; metadata.mkdir()
        before = metadata.stat().st_ino
        with patch.object(worker, '_run_git') as git:
            with self.assertRaisesRegex(contract.BootstrapError, 'OWNERSHIP_UNCERTAIN'):
                self._git_initialize(scope, known, files)
            git.assert_not_called()
        self.assertEqual(metadata.stat().st_ino, before)
        self.assertEqual(list(metadata.iterdir()), [])
        self.assertTrue(Path(scope['git_staging_path']).exists())

    def test_unproven_nonempty_original_inode_is_preserved_and_refused(self):
        scope, known, files, metadata = self.failed_original_publication()
        (metadata/'unproven').write_bytes(b'preserve this original unproved state')
        with patch.object(worker, '_run_git') as git:
            with self.assertRaisesRegex(contract.BootstrapError, 'OWNERSHIP_UNCERTAIN'):
                self._git_initialize(scope, known, files)
            git.assert_not_called()
        self.assertEqual((metadata/'unproven').read_bytes(), b'preserve this original unproved state')

    def test_changed_document_prevents_even_original_empty_inode_recovery(self):
        scope, known, files, metadata = self.failed_original_publication()
        name = next(iter(files))
        (self.workspace/scope['target_name']/name).write_bytes(b'changed original manifest file')
        with patch.object(worker, '_run_git') as git:
            with self.assertRaises(contract.BootstrapError): self._git_initialize(scope, known, files)
            git.assert_not_called()
        self.assertEqual(list(metadata.iterdir()), [])

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS runtime migration only')
    def test_only_missing_legacy_pin_gets_new_readiness_attempt_without_history_rewrite(self):
        service.admit(self.data, token=self.grant['credential'], headers=self.headers, value=self.original_request)
        historical = {'attempt_id':'a'*32,'step':'PREFLIGHT','state':'STEP_COMPLETED','exit_code':0}
        with authority.store(self.data, write=True) as db:
            row = service._row(db, 'synthetic-operation', self.grant['credential'])
            row['evidence'].pop('git_runtime_policy')
            row['evidence']['completed_steps'] = ['PREFLIGHT']
            row['evidence']['attempts'] = [historical]
            row['evidence']['known'] = {'native_profile_started':True,'business_effects':0}
            service._save(db, row, self.grant['credential'])
        # Selection of the next finite step only. Real installed migration and
        # native execution remain separate qualification, not this double.
        with patch.object(service, '_execute_step', side_effect=contract.BootstrapError('OWN_STOP',409)) as step:
            with self.assertRaisesRegex(contract.BootstrapError, 'OWN_STOP'):
                service.execute(self.data, operation_id='synthetic-operation', token=self.grant['credential'], headers=self.headers)
            self.assertEqual(step.call_args.kwargs['step'], 'PREFLIGHT')
        with authority.store(self.data) as db:
            row = service._row(db, 'synthetic-operation', self.grant['credential'])
        self.assertEqual(row['evidence']['completed_steps'], ['PREFLIGHT'])
        self.assertEqual(row['evidence']['attempts'], [historical])
        self.assertFalse((self.workspace/'new-repository').exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS runtime migration only')
    def test_current_missing_pin_is_red_without_repin_or_worker_start(self):
        service.admit(self.data, token=self.grant['credential'], headers=self.headers, value=self.original_request)
        with authority.store(self.data, write=True) as db:
            row = service._row(db, 'synthetic-operation', self.grant['credential'])
            row['evidence']['completed_steps'] = ['PREFLIGHT']
            service._save(db, row, self.grant['credential'])
        with patch.object(service.genesis_native, 'runtime', return_value=Path(sys.executable)), \
             patch.object(service.genesis_git_runtime, 'capture') as capture, \
             patch.object(service.subprocess, 'Popen') as start:
            with self.assertRaisesRegex(contract.BootstrapError, 'PIN_INVALID'):
                service.execute(self.data, operation_id='synthetic-operation', token=self.grant['credential'], headers=self.headers)
            capture.assert_not_called(); start.assert_not_called()
        self.assertFalse((self.workspace/'new-repository').exists())

    def test_revoked_original_scope_starts_no_readiness_or_worker(self):
        service.admit(self.data, token=self.grant['credential'], headers=self.headers, value=self.original_request)
        with authority.store(self.data, write=True) as db:
            authority.revoke(db, grant_id='synthetic-grant', operation_id='synthetic-operation')
        with patch.object(service.genesis_git_runtime, 'capture') as capture, \
             patch.object(service.genesis_native, 'runtime') as native, \
             patch.object(service.subprocess, 'Popen') as start:
            with self.assertRaises(contract.BootstrapError):
                service.execute(self.data, operation_id='synthetic-operation', token=self.grant['credential'], headers=self.headers)
            capture.assert_not_called(); native.assert_not_called(); start.assert_not_called()
        self.assertFalse((self.workspace/'new-repository').exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS runtime pin only')
    def test_failed_readiness_keeps_original_candidate_and_refuses_selection_drift(self):
        service.admit(self.data, token=self.grant['credential'], headers=self.headers, value=self.original_request)
        with authority.store(self.data, write=True) as db:
            row = service._row(db, 'synthetic-operation', self.grant['credential'])
            _, scope = authority.authenticate(db, self.grant['credential'], self.headers)
            pin = service.genesis_git_runtime.capture(operation_id=scope['operation_id'], host_id=scope['host_id'])
            original = {'attempt_id':'b'*32,'step':'PREFLIGHT','state':'STEP_PERMITTED','git_runtime':pin}
            row['evidence']['attempts'] = [original]
            service._save(db, row, self.grant['credential'])
        changed = deepcopy(pin); changed['executable']['sha256'] = '0'*64
        with patch.object(service.genesis_native, 'runtime', return_value=Path(sys.executable)), \
             patch.object(service.genesis_git_runtime, 'capture', return_value=changed) as current_selection, \
             patch.object(service.subprocess, 'Popen') as start:
            with self.assertRaisesRegex(contract.BootstrapError, 'GIT_RUNTIME_CHANGED'):
                service.execute(self.data, operation_id='synthetic-operation', token=self.grant['credential'], headers=self.headers)
            self.assertEqual(current_selection.call_count, 1)  # Verification, not replacement followed by verification.
            start.assert_not_called()
        with authority.store(self.data) as db:
            evidence = service._row(db, 'synthetic-operation', self.grant['credential'])['evidence']
        self.assertEqual(evidence['attempts'][0], original)
        self.assertNotIn('git_runtime', evidence['known'])
        self.assertFalse((self.workspace/'new-repository').exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS runtime pin only')
    def test_late_binary_check_refuses_before_git_start(self):
        scope, known, files = self.prepared()
        known.update(self._git_initialize(scope, known, files))
        with patch.object(worker, '_git_executable', side_effect=('/fixed-own-git',
                contract.BootstrapError('BOOTSTRAP_GIT_RUNTIME_CHANGED',409))), \
             patch.object(worker, '_run_git') as git:
            with self.assertRaisesRegex(contract.BootstrapError, 'GIT_RUNTIME_CHANGED'):
                worker._git(self.workspace/scope['target_name'], 'status', known=known, scope=scope)
            git.assert_not_called()

    @unittest.skipUnless(sys.platform == 'darwin', 'actual macOS pinned runtime check')
    def test_late_same_inode_metadata_is_refused_on_execution_descriptor(self):
        scope, known, files, metadata = self.failed_original_publication()
        original = worker._git_executable
        calls = []
        def insert_during_runtime_check(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                (metadata/'unproved-late').write_bytes(b'actual late same-inode bytes')
            return original(*args, **kwargs)
        with patch.object(worker, '_git_executable', side_effect=insert_during_runtime_check), \
             patch.object(worker, '_run_git') as git:
            with self.assertRaisesRegex(contract.BootstrapError, 'OWNERSHIP_UNCERTAIN'):
                self._git_initialize(scope, known, files)
            git.assert_not_called()
        self.assertEqual((metadata/'unproved-late').read_bytes(), b'actual late same-inode bytes')

    @unittest.skipUnless(sys.platform == 'darwin', 'actual macOS pinned runtime check')
    def test_late_document_change_is_refused_before_original_empty_init(self):
        scope, known, files, metadata = self.failed_original_publication()
        original = worker._git_executable
        calls = []
        changed = self.workspace/scope['target_name']/next(iter(files))
        def replace_document_during_runtime_check(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2: changed.write_bytes(b'late original manifest conflict')
            return original(*args, **kwargs)
        with patch.object(worker, '_git_executable', side_effect=replace_document_during_runtime_check), \
             patch.object(worker, '_run_git') as git:
            with self.assertRaises(contract.BootstrapError):
                self._git_initialize(scope, known, files)
            git.assert_not_called()
        self.assertEqual(changed.read_bytes(), b'late original manifest conflict')
        self.assertEqual(list(metadata.iterdir()), [])
