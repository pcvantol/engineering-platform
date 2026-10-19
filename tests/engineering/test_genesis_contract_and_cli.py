"""Closed untrusted inputs and owning CLI parity; no installed-product claim."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from engineering_platform import genesis_authority as authority, genesis_contract as contract
from engineering_platform import genesis_cli as cli, genesis_native as native, genesis_service as service
from tests.engineering import test_genesis_bootstrap as fixture


class UntrustedContractTests(unittest.TestCase):
    def setUp(self): self.original = json.loads(fixture.FIXTURE.read_bytes())

    def test_transport_duplicate_nonfinite_encoding_and_limit_are_errors(self):
        for raw, error in ((b'{"a":1,"a":2}', 'DUPLICATE_JSON_KEY'), (b'{"a":NaN}', 'NONFINITE_JSON'),
                           (b'\xff', 'JSON_INVALID'), (b'{', 'JSON_INVALID'),
                           (b' '* (contract.MAX_BODY+1), 'BODY_LIMIT')):
            with self.subTest(error=error), self.assertRaisesRegex(contract.BootstrapError, error): contract.load(raw)

    def test_finite_paths_bytes_modes_and_hashes_cannot_be_extended(self):
        variants = []
        value = deepcopy(self.original); value['files']['../escape'] = value['files'].pop('README.md'); variants.append((value, 'FINITE_PATH_SET_INVALID'))
        value = deepcopy(self.original); value['files']['README.md']['utf8'] += 'changed'; variants.append((value, 'PAYLOAD_DIGEST_CONFLICT'))
        value = deepcopy(self.original); value['files']['README.md']['mode'] = '0777'; variants.append((value, 'FIELDS_INVALID'))
        value = deepcopy(self.original); value['files']['README.md']['utf8'] = 1; variants.append((value, 'PAYLOAD_INVALID'))
        value = deepcopy(self.original); value['files']['README.md']['utf8'] = '\ud800'; variants.append((value, 'PAYLOAD_INVALID'))
        value = deepcopy(self.original); value['files']['README.md']['utf8'] = 'x'*131073; variants.append((value, 'PAYLOAD_LIMIT'))
        value = deepcopy(self.original); value['kind'] = 'EXECUTABLE'; variants.append((value, 'BUNDLE_FORMAT_UNSUPPORTED'))
        for value, error in variants:
            with self.subTest(error=error), self.assertRaisesRegex(contract.BootstrapError, error): contract.bundle(value)

    def _manifest(self, mutate):
        value = deepcopy(self.original); path = 'engineering/bootstrap-manifest.json'
        manifest = json.loads(value['files'][path]['utf8']); mutate(manifest)
        manifest['manifest_digest'] = contract.digest(contract.canonical({k:v for k,v in manifest.items() if k != 'manifest_digest'}))
        raw = contract.canonical(manifest); value['files'][path] = {'sha256': contract.digest(raw), 'utf8': raw.decode()}
        return value

    def test_valid_hash_does_not_authorize_unsupported_profile_or_executable_mode(self):
        for mutate, error in ((lambda m: m.update(state='COMPLETE'), 'PROFILE_UNSUPPORTED'),
            (lambda m: m.update(artifacts=[]), 'ARTIFACT_SET_INVALID'),
            (lambda m: m['artifacts'][0].update(relative_path='../escape'), 'ARTIFACT_SET_INVALID'),
            (lambda m: m['artifacts'][0].update(file_mode='0755'), 'MANIFEST_FILE_CONFLICT'),
            (lambda m: m['artifacts'][0].update(expected_before='EXISTING'), 'MANIFEST_FILE_CONFLICT')):
            with self.subTest(error=error), self.assertRaisesRegex(contract.BootstrapError, error): contract.bundle(self._manifest(mutate))

    def test_native_runtime_adapter_refuses_missing_wrong_version_and_spawn_errors(self):
        from engineering_platform.effect_contract import EffectContractError
        with patch.object(native, 'codex_cli_executable', return_value=None):
            with self.assertRaisesRegex(contract.BootstrapError, 'RUNTIME_UNAVAILABLE'): native.runtime()
        with patch.object(native, 'codex_cli_executable', return_value='/trusted/native'), \
             patch.object(native.effect_workspace, 'native_runtime_file', return_value=Path('/trusted/native')):
            for result in (subprocess.CompletedProcess([], 0, b'codex-cli 0.160.1\n'),
                           subprocess.CompletedProcess([], 0, b'codex-cli wrong\n'),
                           subprocess.CompletedProcess([], 1, b'codex-cli 0.160.1\n')):
                with patch.object(native.subprocess, 'run', return_value=result):
                    if result.returncode == 0 and result.stdout == b'codex-cli 0.160.1\n':
                        self.assertEqual(native.runtime(), Path('/trusted/native'))
                    else:
                        with self.assertRaisesRegex(contract.BootstrapError, 'RUNTIME_UNQUALIFIED'): native.runtime()
            with patch.object(native.subprocess, 'run', side_effect=OSError('actual fixture spawn failure')):
                with self.assertRaisesRegex(contract.BootstrapError, 'RUNTIME_UNAVAILABLE'): native.runtime()
        environment = native.environment(Path('/trusted/native'))
        self.assertEqual(environment['GIT_CONFIG_GLOBAL'], os.devnull)
        self.assertEqual(environment['GIT_NO_LAZY_FETCH'], '1')


class OwningBoundaryTests(unittest.TestCase):
    setUp = fixture.GenesisOwnerFixture.setUp
    _create = fixture.GenesisOwnerFixture._create
    _git_initialize = fixture.GenesisOwnerFixture._git_initialize
    _admitted_committed = fixture.GenesisOwnerFixture._admitted_committed

    def written(self):
        from engineering_platform import genesis_worker as worker
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant['credential'], self.headers)
        _, original = contract.request(self.original_request)
        known = self._create(scope); known.update(worker.write_files(scope, known, original.files))
        return scope, known, original

    def test_private_marker_and_existing_lock_do_not_allow_foreign_adoption(self):
        marker = self.data/'g.identity.json'; original = marker.read_bytes()
        changed = json.loads(original); changed['host_id'] = 'foreign'; marker.write_bytes(contract.canonical(changed))
        with self.assertRaisesRegex(contract.BootstrapError, 'PRIVATE_EVIDENCE_UNAVAILABLE'): service.initialize(self.data, expected_instance=self.instance)
        marker.write_bytes(original)
        with service._lock(self.data, 'same-operation'):
            with self.assertRaisesRegex(contract.BootstrapError, 'RESOURCE_BUSY'):
                with service._lock(self.data, 'same-operation'): self.fail('Parallel resource acquired')
        path = self.data/'g/locks/public.lock'; path.write_bytes(b''); path.chmod(0o644)
        with self.assertRaisesRegex(contract.BootstrapError, 'RESOURCE_LOCK_UNSAFE'):
            with service._lock(self.data, 'public'): self.fail('Unsafe lock acquired')

    def test_schema_or_instance_drift_is_not_an_empty_new_source(self):
        with authority.store(self.data, write=True) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE ep_installations SET schema_version=76')
        with authority.store(self.data) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM ep_installations').fetchone()[0], 77)
        with self.assertRaisesRegex(contract.BootstrapError, 'INSTANCE_MISMATCH'):
            service.initialize(self.data, expected_instance='foreign-instance')
        with authority.store(self.data, write=True) as db: db.execute('DROP TABLE ep_genesis_operations')
        with self.assertRaisesRegex(contract.BootstrapError, 'LEDGER_SCHEMA_INVALID'):
            with authority.store(self.data): self.fail('Missing ledger accepted')

    def test_actual_trusted_validator_accepts_exact_original_docs_and_refuses_replaced_pin(self):
        from engineering_platform import genesis_worker as worker
        scope, known, original = self.written()
        receipt = worker.validate_documents(scope, known, original.files, original.manifest_digest)
        self.assertEqual(receipt['validation']['state'], 'VALIDATED_PROPOSAL')
        self.validator.write_text('raise SystemExit(0)')
        with self.assertRaisesRegex(contract.BootstrapError, 'TRUSTED_VALIDATION_FAILED'):
            worker.validate_documents(scope, known, original.files, original.manifest_digest)
        self.assertFalse((self.workspace/'new-repository/.git').exists())

    def test_exact_file_recovery_cannot_accept_changed_modes_bytes_or_extra_entries(self):
        from engineering_platform import genesis_worker as worker
        scope, known, original = self.written()
        root = self.workspace/'new-repository'; path = root/'README.md'; before = path.read_bytes()
        path.chmod(0o600)
        with self.assertRaisesRegex(contract.BootstrapError, 'FILE_CHANGED'): worker.write_files(scope, known, original.files)
        path.chmod(0o644); path.write_bytes(b'changed')
        with self.assertRaisesRegex(contract.BootstrapError, 'FILE_CHANGED'): worker.verify_files(scope, known, original.files, git=False)
        path.write_bytes(before)
        extra = root/'extra'; extra.write_bytes(b'foreign')
        with self.assertRaisesRegex(contract.BootstrapError, 'INVENTORY_CONFLICT'): worker.verify_files(scope, known, original.files, git=False)
        extra.unlink()
        self.assertEqual(worker.write_files(scope, known, original.files)['file_identities'], known['file_identities'])

    def test_worker_protocol_requires_exact_original_bundle_step_and_fresh_permit(self):
        from engineering_platform import genesis_worker as worker
        with authority.store(self.data) as db:
            _, scope = authority.authenticate(db, self.grant['credential'], self.headers)
        from engineering_platform import genesis_git_runtime
        runtime = (genesis_git_runtime.capture(operation_id=scope['operation_id'], host_id=scope['host_id'])
            if sys.platform == 'darwin' else None)
        job = {'scope': scope, 'known': {}, 'git_runtime': runtime,
               'step': 'PREFLIGHT', 'bundle': self.bundle, 'accepted_at': 1792137600}
        for step, bound, permit, expected in (('PREFLIGHT', job['scope']['bundle_digest'], b'EXECUTE\n', 0),
                ('PREFLIGHT', job['scope']['bundle_digest'], b'NO\n', 1),
                ('UNKNOWN', job['scope']['bundle_digest'], b'EXECUTE\n', 1),
                ('PREFLIGHT', '0'*64, b'EXECUTE\n', 1)):
            value = deepcopy(job); value['step'] = step; value['scope']['bundle_digest'] = bound
            output, errors = io.StringIO(), io.TextIOWrapper(io.BytesIO())
            input_stream = io.TextIOWrapper(io.BytesIO(contract.canonical(value)+permit))
            with patch.object(sys, 'stdin', input_stream), redirect_stdout(output), patch.object(sys, 'stderr', errors):
                self.assertEqual(worker.main(), expected)
            receipts = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(receipts[-1]['state'], 'STEP_COMPLETED' if expected == 0 else 'STEP_NOT_COMPLETED')
            if expected == 0: self.assertEqual(receipts[-1]['result']['business_effects'], 0)

    def test_real_controller_child_handshake_permit_and_durable_logs(self):
        # A fixed protocol-only Python fixture tests the controller, not native
        # isolation or physical Genesis delivery. Installed qualification is separate.
        service.admit(self.data, token=self.grant['credential'], headers=self.headers, value=self.original_request)
        marker = self.root/'protocol-fixture-ready'
        code = (f'import sys,json,hashlib;from pathlib import Path;Path({str(marker)!r}).write_text("ready"); '
                'j=json.loads(sys.stdin.buffer.readline()); '
                'raw=(json.dumps(j,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\\n").encode(); '
                'print(json.dumps({"state":"STEP_READY","job_digest":hashlib.sha256(raw).hexdigest()}),flush=True); '
                'assert sys.stdin.buffer.readline()==b"EXECUTE\\n"; '
                'print(json.dumps({"state":"STEP_COMPLETED","step":j["step"],"result":{"business_effects":0,"git_runtime":j.get("git_runtime"),"git_version_sha256":"0"*64}}),flush=True)')
        original_popen = subprocess.Popen
        def initialized_child(*args, **kwargs):
            # Wait for this modeled worker's actual exec before returning its
            # Popen handle. Identity capture remains the real OS adapter.
            child = original_popen(*args, **kwargs)
            until = time.monotonic()+5
            while not marker.exists():
                if child.poll() is not None or time.monotonic() >= until:
                    child.kill(); child.wait(timeout=5)
                    self.fail('Protocol fixture failed before its actual exec marker')
                time.sleep(.01)
            return child
        with service._lock(self.data, 'synthetic-operation') as fd, \
             patch.object(service.genesis_native, 'runtime', return_value=Path(sys.executable)), \
             patch.object(service.genesis_native, 'command', return_value=(sys.executable, '-I', '-c', code)), \
             patch.object(service.subprocess, 'Popen', side_effect=initialized_child):
            result = service._execute_step(self.data, operation_id='synthetic-operation', step='PREFLIGHT',
                token=self.grant['credential'], headers=self.headers, source_identity=None, lock_fd=fd)
        self.assertEqual(result['business_effects'], 0)
        with authority.store(self.data) as db:
            evidence = service._row(db, 'synthetic-operation', self.grant['credential'])['evidence']
        self.assertEqual(evidence['completed_steps'], ['PREFLIGHT'])
        self.assertEqual(evidence['attempts'][0]['state'], 'STEP_COMPLETED')
        self.assertEqual(evidence['attempts'][0]['exit_code'], 0)
        self.assertTrue(list((self.data/'g/evidence').glob('*.stdout')))
        self.assertFalse((self.workspace/'new-repository').exists())

    def test_valid_existing_commit_and_registration_resume_have_original_identity(self):
        from engineering_platform import genesis_worker as worker
        scope, known = self._admitted_committed()
        self.assertEqual(self._git_initialize(scope, known, contract.bundle(self.bundle).files)['git_identity'], known['git_identity'])
        self.assertEqual(worker.commit_object(scope, known)['commit_object'], known['commit'])
        self.assertEqual(worker.commit_ref(scope, known)['commit_ref'], known['commit_ref'])
        results = []
        service.operation(self.data, token=self.grant['credential'], headers=self.headers, operation_id='synthetic-operation',
            resume=True, publish=lambda result, instance, check: (check(), results.append(result)))
        self.assertEqual((results[0]['state'], results[0]['commit']), ('COMPLETE', known['commit']))

    def test_real_git_object_plumbing_under_modeled_security_sha1_prohibition(self):
        from engineering_platform import genesis_worker as worker
        original_sha1 = worker.hashlib.sha1
        def compatibility_hash(raw, **kwargs):
            if kwargs.get('usedforsecurity') is not False:
                raise ValueError('modeled security policy forbids SHA1 security use')
            return original_sha1(raw, **kwargs)
        # This models only the constructor policy; Git object creation and
        # verification are real. It does not claim an actual FIPS-qualified OS.
        with patch.object(worker.hashlib, 'sha1', side_effect=compatibility_hash):
            scope, known = self._admitted_committed()
        self.assertTrue(known['committed_declaration_verified'])
        with authority.store(self.data) as db:
            self.assertEqual(authority.authenticate(db, self.grant['credential'], self.headers)[0], 'synthetic-grant')

    def invoke(self, command, *, value=None, token=None, extra=()):
        args = [command, '--data-root', str(self.data), '--expected-instance-id', self.instance, *extra]
        if value is not None:
            path = self.root/'owner.json'; path.write_bytes(contract.canonical(value)); path.chmod(0o600)
            args += ['--config', str(path)]
        if token is not None: args += ['--credential-stdin']
        output = io.StringIO()
        with redirect_stdout(output), patch.object(sys, 'stdin', io.StringIO(token or '')):
            status = cli.main(args)
        return status, json.loads(output.getvalue())

    def test_owner_init_status_revoke_and_replay_do_not_issue_execution_credentials(self):
        status, body = self.invoke('init'); self.assertEqual((status, body['state']), (0, 'ALREADY_INITIALIZED'))
        status, body = self.invoke('status', extra=('--grant-id', 'synthetic-grant'))
        self.assertEqual((status, body['state']), (0, 'ACTIVE')); self.assertNotIn('credential', body)
        for _ in range(2):
            status, body = self.invoke('revoke', extra=('--grant-id', 'synthetic-grant', '--operation-id', 'original-revoke'))
            self.assertEqual((status, body['state']), (0, 'REVOKED'))
        status, body = self.invoke('revoke', extra=('--grant-id', 'synthetic-grant', '--operation-id', 'changed-revoke'))
        self.assertEqual((status, body['error']), (2, 'BOOTSTRAP_REVOCATION_CONFLICT'))
        with authority.store(self.data) as db:
            with self.assertRaisesRegex(contract.BootstrapError, 'GRANT_INACTIVE'): authority.authenticate(db, self.grant['credential'], self.headers)
        self.assertFalse((self.workspace/'new-repository').exists())

    def test_cli_read_and_identical_apply_use_same_original_reserved_operation(self):
        service.admit(self.data, token=self.grant['credential'], headers=self.headers, value=self.original_request)
        config = {'headers': self.headers, 'request': self.original_request}
        for cmd in ('read', 'apply'):
            status, body = self.invoke(cmd, value=config, token=self.grant['credential'], extra=('--operation-id', 'synthetic-operation'))
            self.assertEqual((status, body['state']), (0, 'RESERVED'))
            self.assertEqual(body['operation_id'], 'synthetic-operation'); self.assertEqual(body['step_effects'], [])
        self.assertFalse((self.workspace/'new-repository').exists())
        status, body = self.invoke('read', value=config, extra=('--operation-id', 'synthetic-operation'))
        self.assertEqual((status, body['error']), (2, 'BOOTSTRAP_EXPLICIT_CREDENTIAL_CONTEXT_REQUIRED'))
        status, body = self.invoke('read', value={}, token=self.grant['credential'])
        self.assertEqual((status, body['error']), (2, 'BOOTSTRAP_COMMAND_FAILED'))

    def test_cli_config_must_be_private_regular_owned_and_nofollow(self):
        path = self.root/'public.json'; path.write_text('{}'); path.chmod(0o644)
        with self.assertRaisesRegex(contract.BootstrapError, 'PRIVATE_CONFIG_REQUIRED'): cli.config(path)
        linked = self.root/'linked'; linked.symlink_to(path)
        with self.assertRaises(OSError): cli.config(linked)
        self.assertEqual(cli.config(None), {})

    def test_wrong_scope_and_changed_request_never_reserve_a_second_operation(self):
        for key in authority.HEADERS:
            with self.subTest(key=key), authority.store(self.data) as db:
                changed = {**self.headers, key: 'foreign'}
                with self.assertRaisesRegex(contract.BootstrapError, 'SCOPE_DENIED'): authority.authenticate(db, self.grant['credential'], changed)
        for token in (None, 'a', 'x'*129, '\u2603'*40, 'x'*40):
            with authority.store(self.data) as db, self.assertRaisesRegex(contract.BootstrapError, 'UNAUTHENTICATED'):
                authority.authenticate(db, token, self.headers)
        changed = deepcopy(self.original_request); changed['effect_plan_binding']['plan_digest'] = 'a'*64
        with self.assertRaisesRegex(contract.BootstrapError, 'REQUEST_SCOPE_CONFLICT'):
            service.admit(self.data, token=self.grant['credential'], headers=self.headers, value=changed)
        with authority.store(self.data) as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM ep_genesis_operations').fetchone()[0], 0)

    def test_missing_or_corrupt_initialized_store_is_never_recreated_or_empty_success(self):
        database = self.data/'epdata.sqlite'; saved = self.data/'original.sqlite'; database.rename(saved)
        for write in (False, True):
            with self.assertRaises(FileNotFoundError):
                with authority.store(self.data, write=write): self.fail('Missing source opened')
            self.assertFalse(database.exists())
        database.write_bytes(b'not a database')
        with self.assertRaises(sqlite3.DatabaseError):
            with authority.store(self.data): self.fail('Corrupt source opened')
        self.assertEqual(database.read_bytes(), b'not a database')
        database.unlink(); saved.rename(database)
        with authority.store(self.data) as db: self.assertEqual(authority.status(db, 'synthetic-grant')['state'], 'ACTIVE')

    def test_untrusted_owner_setup_cannot_bypass_placement_or_validator_pin(self):
        for mutate, error in ((lambda c:c.update(ttl_seconds=0), 'LIFETIME_INVALID'),
                              (lambda c:c.update(workspace_root=None), 'OWNER_PLACEMENT_REQUIRED')):
            value = deepcopy(self.config); mutate(value)
            with self.assertRaisesRegex(contract.BootstrapError, error): authority.issue(self.data, expected_instance=self.instance, config=value)
        self.validator.write_text('raise SystemExit(0)')
        with self.assertRaisesRegex(contract.BootstrapError, 'EXTERNAL_VALIDATOR_PIN_INVALID'):
            authority.issue(self.data, expected_instance=self.instance, config=self.config)
        self.assertFalse((self.workspace/'new-repository').exists())

    def test_canonical_project_identity_conflict_is_refused_before_target_effects(self):
        from engineering_platform import project_topology
        declaration = json.loads(self.bundle['files']['.engineering-platform/repository.json']['utf8'])
        with authority.store(self.data, write=True) as db:
            project_topology.register_server_local_topology(db, declaration=declaration)
        with self.assertRaisesRegex(contract.BootstrapError, 'PROJECT_ALREADY_REGISTERED'):
            service.admit(self.data, token=self.grant['credential'], headers=self.headers, value=self.original_request)
        self.assertFalse((self.workspace/'new-repository').exists())

    def test_native_scope_has_no_network_fallback_and_rejects_ambiguous_placement(self):
        with patch.object(native.effect_workspace, 'native_runtime_file', side_effect=lambda p:p):
            cmd = native.command(native=Path(sys.executable), anchor=self.data/'g/a', target=self.workspace/'new-repository',
                writable=False, readable=(self.validator,), profile_id='ep-genesis-test')
            self.assertIn('permissions.ep-genesis-test.network.enabled=false', cmd)
            for kwargs, error in (({'profile_id': 'bad profile'}, 'PROFILE_INVALID'),
                                  ({'target': Path('/')}, 'PLACEMENT_UNSUPPORTED'),
                                  ({'git_metadata': True}, 'GIT_SCOPE_INVALID'),
                                  ({'prepared': self.data/'g/a/not-capability', 'writable': True}, 'PREPARATION_UNSAFE')):
                args = dict(native=Path(sys.executable), anchor=self.data/'g/a', target=self.workspace/'new-repository', writable=False, profile_id='ep-genesis-test')
                args.update(kwargs)
                with self.assertRaisesRegex(contract.BootstrapError, error): native.command(**args)
