"""Coupled recovery regressions using real authority, leases and temporary Git."""
from contextlib import redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from engineering_platform import execution_host as host
from engineering_platform.agent_state import TransactionState
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_lease import acquire, LeaseHeartbeat
from engineering_platform.execution_models import AgentResult
from engineering_platform.execution_repository import SubprocessRepositoryClient
from engineering_platform.local_repository_binding import unbind_local_repository
from engineering_platform.managed_adoption import verify_selection
from engineering_platform.provider_recovery import load_recovery_state
from tests.engineering import test_managed_adoption as adoption_fixture


class RecoveryAuthorityRegressions(unittest.TestCase):
    def setUp(self):
        # Reuse the actual owning installation/Git fixture; its external
        # provider-readiness observer does not replace adoption authority.
        self.case = adoption_fixture.AdoptionLifecycleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.calls = []
        self.agent, self.github = self.case.lifecycle_adapters()

    def snapshot(self):
        c = self.case
        tracked = c.git('ls-files').splitlines()
        digest = hashlib.sha256()
        for name in sorted(tracked):
            digest.update(name.encode())
            digest.update((c.root / name).read_bytes())
        return (c.git('rev-parse', 'HEAD'), c.git('rev-parse', 'HEAD^{tree}'),
                c.git('status', '--porcelain'), digest.hexdigest())

    def available(self):
        c = self.case
        state = TransactionState('adopt-run', 'qualification/managed', str(c.prompt),
            'LOCAL_REPOSITORY_VALIDATION', owner_authorized=True,
            last_verified_sha=c.sha, implementation_head_sha=c.sha)
        state = verify_selection(selection=c.selection, state=state, root=c.root,
            repository=c.repository, central_database=c.database, owner_authorized=True)
        c.store.save(state)
        runner = EngineeringRunner(c.root, c.store, c.repository, self.github, self.agent, lambda _: None)
        self.addCleanup(c.stop_host, runner)
        state, error = runner._confirm_deterministic_admission(state)
        self.assertIsNone(error)
        runner.active_lease = acquire(c.root, state.run_id, identity=runner.host_identity,
            instance_id=runner.host_instance_id, process_id=os.getpid(), central_database=c.database)
        runner.lease_heartbeat = LeaseHeartbeat(c.root, runner.active_lease, central_database=c.database)
        runner.lease_heartbeat.start()
        capture = host.capture_worktree_provenance
        def interrupted(*args, **kwargs):
            value = capture(*args, **kwargs)
            if kwargs.get('stage') == 'interrupted' and kwargs.get('phase') == 'REPAIR_AGENT':
                self.assertTrue(value)
                raise SystemExit('complete availability boundary')
            return value
        # Observe the real controlled interruption only after durable evidence.
        with patch.dict(os.environ, {'ENGINEERING_PLATFORM_TEST_INTERRUPT_PROVIDER_ONCE': 'adopt-run:REPAIR_AGENT'}), \
             patch.object(host, 'capture_worktree_provenance', side_effect=interrupted):
            with self.assertRaisesRegex(SystemExit, 'complete availability boundary'):
                runner._repair(state, 'local validation failed. Correct the documentation heading.')
        c.stop_host(runner)
        state = c.store.load('adopt-run')
        recovery = load_recovery_state(c.root, state.run_id, central_database=c.database)
        self.assertEqual(recovery['state'], 'RECOVERY_AVAILABLE')
        self.assertEqual(state.repair_iterations, 1)
        return state, recovery

    def replacement(self):
        c = self.case
        self.agent.process_callback = None
        self.agent.set_process_callback = lambda callback: setattr(self.agent, 'process_callback', callback)
        def invoke(root, prompt):
            self.calls.append('replacement')
            self.assertIn('Integral repair method:', prompt)
            child = subprocess.Popen((sys.executable, '-c', 'import time;time.sleep(30)'), start_new_session=True)
            try:
                self.agent.process_callback({'pid': child.pid, 'process_group': child.pid})
                (root / 'README.md').write_text('# Repaired under current authority\n')
                c.git('add', 'README.md')
                c.git('commit', '-qm', 'same bounded replacement')
                return AgentResult('COMPLETE', 'codex/existing', commit_sha=c.git('rev-parse', 'HEAD'))
            finally:
                child.terminate()
                child.wait(timeout=10)
                self.agent.process_callback(None)
        self.agent.invoke = invoke

    def resume(self):
        c = self.case
        runner = EngineeringRunner(c.root, c.store, c.repository, self.github, self.agent, lambda _: None)
        self.addCleanup(c.stop_host, runner)
        return runner.run(c.prompt, run_id='adopt-run', resume=True)

    def assert_denied(self, before, recovery, snapshot):
        with sqlite3.connect(self.case.database) as connection:
            invocations = connection.execute('SELECT * FROM provider_invocations ORDER BY invocation_id').fetchall()
        self.replacement()
        after = self.resume()
        with sqlite3.connect(self.case.database) as connection:
            self.assertEqual(connection.execute('SELECT * FROM provider_invocations ORDER BY invocation_id').fetchall(), invocations)
        self.assertEqual(self.calls, [], 'revoked authority must prevent every replacement call')
        self.assertEqual(self.snapshot(), snapshot, 'revoked authority must prevent target writes')
        self.assertEqual((after.phase, after.next_action), ('BLOCKED', 'managed_candidate_adoption_invalid'))
        self.assertEqual(self.github.creates, 0)
        self.assertEqual(after.repair_iterations, before.repair_iterations)
        self.assertEqual(after.repair_audit, before.repair_audit)
        self.assertEqual(after.specialist_records, before.specialist_records)
        self.assertEqual(load_recovery_state(self.case.root, before.run_id,
                         central_database=self.case.database), recovery)

    def test_unbound_available_repair_has_zero_calls_and_target_writes(self):
        before, recovery = self.available()
        self.case.unbind()
        self.assert_denied(before, recovery, self.snapshot())

    def test_inactive_project_available_repair_has_zero_calls_and_target_writes(self):
        before, recovery = self.available()
        with sqlite3.connect(self.case.database) as connection:
            connection.execute("UPDATE ep_project_registrations SET status='DISABLED' WHERE project_id='project'")
        self.assert_denied(before, recovery, self.snapshot())

    def test_current_declaration_drift_has_zero_calls_and_target_writes(self):
        before, recovery = self.available()
        path = self.case.root / '.engineering-platform/repository.json'
        declaration = json.loads(path.read_text())
        declaration['project']['id'] = 'foreign-project'
        path.write_text(json.dumps(declaration))
        self.assert_denied(before, recovery, self.snapshot())

    def test_rebound_target_available_repair_has_zero_calls_and_target_writes(self):
        before, recovery = self.available()
        c = self.case
        other = c.area / 'rebound-repository'
        subprocess.run(('git', 'clone', '-q', '--local', str(c.root), str(other)), check=True)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(adoption_fixture.server.main(['rebind-repository', '--data-root', str(c.data),
                '--project-id', 'project', '--repository-id', 'repo', '--path', str(other)]), 0)
        self.assert_denied(before, recovery, self.snapshot())

    def test_actual_installation_owner_mismatch_denies_effect(self):
        from engineering_platform.managed_adoption import effect_authority
        state, _ = self.available()
        c = self.case
        runner = EngineeringRunner(c.root, c.store, c.repository, self.github, self.agent)
        runner.active_lease = acquire(c.root, state.run_id, identity=runner.host_identity,
            instance_id=runner.host_instance_id, process_id=os.getpid(), central_database=c.database)
        runner.lease_heartbeat = LeaseHeartbeat(c.root, runner.active_lease, central_database=c.database)
        runner.lease_heartbeat.start()
        self.addCleanup(c.stop_host, runner)
        # A selected installation whose real directory belongs to another UID
        # cannot inherit the durable owner receipt. No UID/authority mock.
        parent = Path('/tmp').resolve()
        if parent.stat().st_uid == os.geteuid():
            self.fail('owner mismatch qualification requires the system-owned temporary directory')
        descriptor, name = tempfile.mkstemp(prefix='ep-owner-authority-', suffix='.sqlite', dir=parent)
        os.close(descriptor)
        target = Path(name)
        self.addCleanup(target.unlink, missing_ok=True)
        with sqlite3.connect(c.database) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        snapshot = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'actual installation owner'):
            with effect_authority(state=state, root=c.root, central_database=target, lease=runner.active_lease):
                self.calls.append('forbidden owner effect')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), snapshot)

    def test_revocation_at_provider_effect_boundary_has_zero_calls_and_preserves_consumption(self):
        before, _ = self.available()
        self.replacement()
        c = self.case
        setter = self.agent.set_process_callback
        captured = []
        def external_callback_registration(callback):
            setter(callback)
            current = load_recovery_state(c.root, before.run_id, central_database=c.database)
            if current['state'] == 'RECOVERY_STARTING' and not captured:
                with sqlite3.connect(c.database) as connection:
                    ledger = connection.execute('SELECT * FROM provider_invocations ORDER BY invocation_id').fetchall()
                captured.append((current, ledger, self.snapshot()))
                c.unbind()  # Real canonical revocation after intent, before launch.
        self.agent.set_process_callback = external_callback_registration
        after = self.resume()
        self.assertEqual(len(captured), 1)
        self.assertIn('Managed adoption project binding is unavailable', after.diagnostic)
        self.assertEqual(self.calls, [])
        self.assertEqual(after.next_action, 'managed_candidate_adoption_invalid')
        self.assertEqual(self.github.creates, 0)
        self.assertEqual(self.snapshot(), captured[0][2])
        self.assertEqual(after.repair_audit, before.repair_audit)
        self.assertEqual(after.repair_iterations, before.repair_iterations)
        self.assertEqual(load_recovery_state(c.root, before.run_id, central_database=c.database), captured[0][0])
        with sqlite3.connect(c.database) as connection:
            self.assertEqual(connection.execute('SELECT * FROM provider_invocations ORDER BY invocation_id').fetchall(), captured[0][1])

    def test_bound_available_repair_preserves_identity_and_qualifies(self):
        before, recovery = self.available()
        self.replacement()
        after = self.resume()
        self.assertEqual(self.calls, ['replacement'])
        self.assertEqual(after.phase, 'WAIT_FOR_OPERATOR_MERGE', after)
        current = load_recovery_state(self.case.root, before.run_id, central_database=self.case.database)
        self.assertEqual(current['state'], 'RECOVERED')
        self.assertEqual(current['replacement_invocation_id'], recovery['replacement_invocation_id'])
        self.assertEqual(after.repair_iterations, 1)
        self.assertEqual(len(after.repair_audit), 1)
        self.assertEqual(after.repair_audit[0]['repair_id'], before.repair_audit[0]['repair_id'])
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(after.publication_intent['candidate_sha'], self.case.git('rev-parse', 'HEAD'))

    def process_restart(self, revoked):
        c = self.case
        spec = c.area / 'recovery-process.json'
        spec.write_text(json.dumps({**{key: str(getattr(c, key)) for key in
            ('root', 'remote', 'data', 'area', 'prompt', 'database')},
            'sha': c.sha, 'base': c.base, 'selection': c.selection}))
        command = (sys.executable, '-m', 'tests.engineering.recovery_authority_process', str(spec))
        crashed = subprocess.run((*command, 'start'), text=True, capture_output=True, timeout=30)
        self.assertEqual(crashed.returncode, 73, crashed.stdout + crashed.stderr)
        before = c.store.load('adopt-run')
        recovery = load_recovery_state(c.root, before.run_id, central_database=c.database)
        self.assertEqual(recovery['state'], 'RECOVERY_AVAILABLE')
        snapshot = self.snapshot()
        if revoked:
            c.unbind()
        with sqlite3.connect(c.database) as connection:
            expiry = connection.execute("SELECT expires_at FROM execution_run_leases WHERE run_id='adopt-run' AND lease_state='ACTIVE'").fetchone()[0]
        delay = (datetime.fromisoformat(expiry) - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            time.sleep(delay + .1)  # Actual product lease expiry, no reset or clock double.
        resumed = subprocess.run((*command, 'resume'), text=True, capture_output=True, timeout=30)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        receipt = json.loads(spec.with_suffix('.result.json').read_text())
        after = c.store.load('adopt-run')
        current = load_recovery_state(c.root, before.run_id, central_database=c.database)
        self.assertEqual(after.repair_iterations, before.repair_iterations)
        self.assertEqual(after.managed_candidate_adoption, before.managed_candidate_adoption)
        self.assertEqual(current['replacement_invocation_id'], recovery['replacement_invocation_id'])
        if revoked:
            self.assertEqual(receipt['calls'], [])
            self.assertEqual(receipt['creates'], 0)
            self.assertEqual(after.next_action, 'managed_candidate_adoption_invalid')
            self.assertEqual(self.snapshot(), snapshot)
            self.assertEqual(current, recovery)
            self.assertEqual(after.repair_audit, before.repair_audit)
            self.assertEqual(after.specialist_records, before.specialist_records)
        else:
            self.assertEqual(receipt['calls'], ['replacement'])
            self.assertEqual(receipt['creates'], 1)
            self.assertEqual(after.phase, 'WAIT_FOR_OPERATOR_MERGE')
            self.assertEqual(current['state'], 'RECOVERED')
            self.assertEqual(len(after.repair_audit), 1)
            self.assertEqual(after.repair_audit[0]['repair_id'], before.repair_audit[0]['repair_id'])

    def test_real_process_available_repair_revocation_preserves_zero_effects(self):
        self.process_restart(True)

    def test_real_process_available_repair_valid_resume_preserves_replacement(self):
        self.process_restart(False)

    def recovered_result(self):
        self.available()
        self.replacement()
        c = self.case
        runner = EngineeringRunner(c.root, c.store, c.repository, self.github, self.agent, lambda _: None)
        self.addCleanup(c.stop_host, runner)
        project = runner._project_durable_recovery
        def stop_after_completed_projection(state, recovery):
            result = project(state, recovery)
            if recovery.get('state') == 'RECOVERED':
                raise SystemExit('actual replacement result durably recovered')
            return result
        with patch.object(runner, '_project_durable_recovery', side_effect=stop_after_completed_projection):
            with self.assertRaisesRegex(SystemExit, 'actual replacement result durably recovered'):
                runner.run(c.prompt, run_id='adopt-run', resume=True)
        c.stop_host(runner)
        before = c.store.load('adopt-run')
        recovery = load_recovery_state(c.root, before.run_id, central_database=c.database)
        self.assertEqual(recovery['state'], 'RECOVERED')
        self.assertEqual(self.calls, ['replacement'])
        self.calls.clear()
        return before, recovery

    def test_actual_recovered_result_revocation_has_no_replay_or_publication(self):
        before, recovery = self.recovered_result()
        self.case.unbind()
        self.assert_denied(before, recovery, self.snapshot())

    def test_actual_recovered_result_valid_resume_has_no_replay(self):
        before, recovery = self.recovered_result()
        after = self.resume()
        self.assertEqual(self.calls, [])
        self.assertEqual(after.phase, 'WAIT_FOR_OPERATOR_MERGE', after)
        self.assertEqual(after.repair_iterations, before.repair_iterations)
        self.assertEqual(len(after.repair_audit), 1)
        self.assertEqual(self.github.creates, 1)
        current = load_recovery_state(self.case.root, before.run_id, central_database=self.case.database)
        self.assertEqual(current['replacement_invocation_id'], recovery['replacement_invocation_id'])

    def test_effect_guard_requires_actual_lease_and_current_checkpoint(self):
        from engineering_platform.managed_adoption import effect_authority
        state, _ = self.available()
        c = self.case
        runner = EngineeringRunner(c.root, c.store, c.repository, self.github, self.agent)
        runner.active_lease = acquire(c.root, state.run_id, identity=runner.host_identity,
            instance_id=runner.host_instance_id, process_id=os.getpid(), central_database=c.database)
        runner.lease_heartbeat = LeaseHeartbeat(c.root, runner.active_lease, central_database=c.database)
        runner.lease_heartbeat.start()
        self.addCleanup(c.stop_host, runner)
        snapshot = self.snapshot()
        for lease in (None, replace(runner.active_lease, host_instance_id='foreign-owner')):
            with self.subTest(lease=lease), self.assertRaisesRegex(RuntimeError, 'exclusive run ownership'):
                with effect_authority(state=state, root=c.root, central_database=c.database, lease=lease):
                    self.calls.append('forbidden effect')
        c.store.save(replace(state, phase='LOCAL_REPOSITORY_VALIDATION'))
        with self.assertRaisesRegex(RuntimeError, 'checkpoint changed'):
            with effect_authority(state=state, root=c.root, central_database=c.database, lease=runner.active_lease):
                self.calls.append('stale checkpoint effect')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), snapshot)

    def test_effect_guard_serializes_actual_unbind_commit(self):
        from engineering_platform.managed_adoption import effect_authority
        state, _ = self.available()
        c = self.case
        runner = EngineeringRunner(c.root, c.store, c.repository, self.github, self.agent)
        runner.active_lease = acquire(c.root, state.run_id, identity=runner.host_identity,
            instance_id=runner.host_instance_id, process_id=os.getpid(), central_database=c.database)
        runner.lease_heartbeat = LeaseHeartbeat(c.root, runner.active_lease, central_database=c.database)
        runner.lease_heartbeat.start()
        self.addCleanup(c.stop_host, runner)
        started, committed = threading.Event(), threading.Event()
        errors = []
        def revoke():
            try:
                with sqlite3.connect(c.database, timeout=10) as connection:
                    started.set()
                    connection.execute('BEGIN IMMEDIATE')
                    unbind_local_repository(connection, project_id='project', repository_id='repo')
                committed.set()
            except BaseException as error:
                errors.append(error)
        with effect_authority(state=state, root=c.root, central_database=c.database, lease=runner.active_lease):
            thread = threading.Thread(target=revoke)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(committed.wait(.1), 'unbind cannot commit between authority check and effect start')
            self.calls.append('authorized synchronous effect')
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(committed.is_set())
        with self.assertRaisesRegex(RuntimeError, 'binding is unavailable'):
            with effect_authority(state=state, root=c.root, central_database=c.database, lease=runner.active_lease):
                self.calls.append('forbidden effect')
        self.assertEqual(self.calls, ['authorized synchronous effect'])


class GenesisConsumerRegression(unittest.TestCase):
    def test_empty_specialist_consumer_supports_local_genesis_without_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(('git', 'init', '-q', '-b', 'main', str(root)), check=True)
            subprocess.run(('git', '-C', str(root), 'config', 'user.email', 'qualification@example.invalid'), check=True)
            subprocess.run(('git', '-C', str(root), 'config', 'user.name', 'qualification'), check=True)
            (root / 'BOOTSTRAP.md').write_text('# Local Genesis\n')
            subprocess.run(('git', '-C', str(root), 'add', 'BOOTSTRAP.md'), check=True)
            subprocess.run(('git', '-C', str(root), 'commit', '-qm', 'local Genesis'), check=True)
            runner = EngineeringRunner(root, None, SubprocessRepositoryClient(), None, None)
            evidence = runner._inspect_assurance_candidate(root, 'GENESIS')
            state = TransactionState('genesis-regression', root.name, str(root / 'prompt.md'),
                                     'EXECUTE_AGENT', execution_mode='GENESIS')
            result = AgentResult('COMPLETE', terminal_condition='local_commit_reconciled',
                                 repository_path=str(root), commit_sha=evidence.head_sha)
            self.assertEqual(runner._consume_optional_specialists(state, result, evidence), state)
            self.assertEqual(subprocess.run(('git', '-C', str(root), 'remote'), text=True,
                                           capture_output=True, check=True).stdout, '')
            with self.assertRaisesRegex(RuntimeError, "No such remote 'origin'"):
                runner.repository.inspect(root)  # Managed remains strict.
