"""Actual remote observations and Git metadata fences for r33 round6."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

from engineering_platform import merge_delegation, submission_service
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_lease import acquire, release, LeaseHeartbeat
from engineering_platform.managed_adoption import AdoptionAuthorityError, effect_authority, verify_selection
from engineering_platform.agent_state import TransactionState
from engineering_platform.providers import GitProvider, process_effect_scope
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher, _historical_admission_environment
from engineering_platform.storage import record_submission, sqlite_connection
from tests.engineering import test_managed_adoption as adoption
from tests.engineering import test_submission_service as submissions


def target_bytes(root):
    # Includes refs, objects, index, config and FETCH_HEAD. Own audit files are
    # deliberately separate from the target and never restore target bytes.
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and '.engineering' not in p.parts and '__pycache__' not in p.parts}


class GitEffectAuthorityRegressions(unittest.TestCase):
    def setUp(self):
        self.case = adoption.AdoptionLifecycleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_fresh_revocation_has_zero_git_mutations_and_identical_metadata(self):
        c = self.case
        agent, github = c.lifecycle_adapters()
        real = c.transport.execute
        revoked, mutations = [], []
        def external_transport(root, *args):
            if any(word in args for word in ('fetch', 'ls-remote')) and not revoked:
                c.unbind()
                revoked.append(target_bytes(c.root))
            if revoked and any(word in args for word in ('fetch', 'push', 'switch', 'checkout', 'merge', 'add', 'commit', 'update-ref')):
                mutations.append(args)
            return real(root, *args)
        c.transport.execute = external_transport
        runner = EngineeringRunner(c.root, c.store, c.repository, github, agent, lambda _:None)
        self.addCleanup(c.stop_host, runner)
        result = runner.run(c.prompt, run_id='adopt-run', owner_authorized=True, managed_candidate=c.selection)
        self.assertEqual(mutations, [], 'revoked fresh admission must prevent every mutating Git start')
        self.assertEqual(len(revoked), 1)
        self.assertEqual(target_bytes(c.root), revoked[0])
        self.assertEqual((result.phase,result.next_action), ('BLOCKED','managed_candidate_adoption_invalid'))
        self.assertEqual(agent.prompts, [])
        self.assertEqual(github.creates, 0)
        self.assertEqual(result.repair_iterations, 0)

    def test_current_remote_observation_does_not_fetch_or_trust_stale_origin(self):
        c = self.case
        # Advance only the real external bare origin. The local cached ref is
        # deliberately stale; observation must get the current remote identity.
        c.transport.command(c.root, 'git', 'push', 'origin', 'HEAD:refs/heads/qualified-candidate')
        subprocess.run(('git','--git-dir',str(c.remote),'update-ref','refs/heads/main',c.sha),check=True)
        self.assertEqual(c.git('rev-parse','origin/main'),c.base)
        before = target_bytes(c.root)
        self.assertEqual(c.repository.protected_main_revision(c.root),c.sha)
        self.assertEqual(target_bytes(c.root),before, 'current remote observation must be write-free')

    def test_fresh_bound_adoption_keeps_original_positive_delivery(self):
        c = self.case
        agent, github = c.lifecycle_adapters()
        runner = EngineeringRunner(c.root,c.store,c.repository,github,agent,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        result=runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE',result)
        self.assertEqual(github.creates,1)
        self.assertEqual(result.managed_candidate_adoption,c.selection)
        self.assertEqual(result.repair_iterations,0)

    def delegated(self, revoke):
        c = self.case
        payload=submissions.CanonicalSubmissionServiceTest().forge_planning_context_payload('git-effect-r6')
        payload['repository_id']='repo';payload['prompt']=c.prompt.read_text()
        payload['constraints']['forge_execution']['repository_id']='repo'
        grant_id='b'*32;actor='local-uid:'+str(os.getuid())
        payload['constraints']['forge_execution']['execution_constraints']=['ep-merge-delegation:'+grant_id]
        with sqlite_connection(c.database) as connection:
            merge_delegation.reserve(connection,delegation_id=grant_id,actor_reference=actor,
                project_id='project',repository_id='repo',github_repository='qualification/managed',
                base_branch='main',roles=('IMPLEMENTATION',),
                expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
            merge_delegation.activate(connection,delegation_id=grant_id,mission_id=payload['mission_id'],
                mission_revision='1',actor_reference=actor,github_repository='qualification/managed')
            accepted=submission_service.submit(connection,submission_service.request_from_mapping('project',payload,transport='HTTP'))
        _,_,run_id,_,reused=ParityLifecycleDispatcher(c.data)._claim(accepted.submission_id)
        self.assertFalse(reused)
        with _historical_admission_environment(c.root,c.data):
            record_submission(c.root,submission_id=accepted.submission_id,producer_id='forge',producer_type='FORGE',
                producer_version='2.7.2',contract_version='1.0',prompt_content=c.prompt.read_text(),
                prompt_metadata={'constraints':payload['constraints']},
                target_identity={'project_id':'project','repository_id':'repo','path':str(c.root)},
                original_envelope=payload,correlation_id=payload['correlation_id'],mission_id=payload['mission_id'],
                engineering_action_id=payload['engineering_action_id'],link_run_id=run_id,
                received_at=datetime.now(timezone.utc).isoformat())
        c.selection['run_id']=run_id
        agent,github=c.lifecycle_adapters();github.repository='qualification/managed'
        original_read=github.pull_request
        observed={'merge_calls':0,'revoked':[],'mutating_git_after_unbind':[], 'merge_checkpoint':None}
        original_execute=c.transport.execute
        def transport(root,*args):
            if observed['revoked'] and any(word in args for word in ('fetch','push','switch','checkout','merge','branch','update-ref')):
                observed['mutating_git_after_unbind'].append(args)
            return original_execute(root,*args)
        c.transport.execute=transport
        def remote_read(number):
            evidence=replace(original_read(number),merge_state_status='CLEAN')
            if observed['merge_calls']:
                observed['merge_checkpoint'] = c.store.load(run_id)
                evidence=replace(evidence,state='MERGED',merge_commit=c.sha)
                if revoke and not observed['revoked']:
                    c.unbind();observed['revoked'].append(target_bytes(c.root))
            return evidence
        github.pull_request=remote_read
        github.delegated_merge_qualification=lambda number,sha:{'conclusion':'PASS','exact_qualified_sha':sha,
            'pull_request_id':number,'strict_checks':True,'reviewers':['independent-external-review'],'base_revision':c.base}
        def remote_merge(number,*,expected_head_sha):
            self.assertEqual(expected_head_sha,c.sha)
            subprocess.run(('git','--git-dir',str(c.remote),'update-ref','refs/heads/main',c.sha),check=True)
            observed['merge_calls']+=1
        github.merge=remote_merge
        runner=EngineeringRunner(c.root,c.store,c.repository,github,agent,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        if revoke:
            result=runner.run(c.prompt,run_id=run_id,owner_authorized=True,managed_candidate=c.selection)
            self.assertEqual(observed['mutating_git_after_unbind'],[], 'revoked post-merge readback must prevent every mutating Git start')
            self.assertEqual(target_bytes(c.root),observed['revoked'][0])
            self.assertEqual((result.phase,result.next_action),('BLOCKED','managed_candidate_adoption_invalid'))
            self.assertEqual(result.delegated_merge_attempt,f'71:{c.sha}')
            self.assertEqual(result.repair_iterations,0)
        else:
            # The real public entry must finish merge readback and guarded
            # synchronization, then hand the ordinary Finalization to provider.
            with self.assertRaisesRegex(SystemExit,'external provider handoff'):
                runner.run(c.prompt,run_id=run_id,owner_authorized=True,managed_candidate=c.selection)
            current=c.store.load(run_id)
            self.assertEqual(current.transaction_kind,'FINALIZATION')
            self.assertEqual(c.git('rev-parse','origin/main'),c.sha)
            self.assertEqual(observed['merge_checkpoint'].delegated_merge_attempt,f'71:{c.sha}')
            self.assertEqual(observed['merge_checkpoint'].repair_iterations,0)
        self.assertEqual(github.creates,1)
        self.assertEqual(observed['merge_calls'],1)
        return observed

    def test_postmerge_unbind_preserves_all_target_metadata_and_attempt(self):
        self.delegated(True)

    def test_postmerge_bound_continuation_reaches_normal_finalization(self):
        self.delegated(False)

    def owned_later_state(self, kind='FINALIZATION'):
        c = self.case
        state = TransactionState('adopt-run', 'qualification/managed', str(c.prompt),
            'LOCAL_REPOSITORY_VALIDATION', owner_authorized=True,
            last_verified_sha=c.sha, implementation_head_sha=c.sha)
        state = verify_selection(selection=c.selection, state=state, root=c.root,
            repository=c.repository, central_database=c.database, owner_authorized=True)
        state = replace(state, transaction_kind=kind)
        c.store.save(state)
        agent, github = c.lifecycle_adapters()
        runner = EngineeringRunner(c.root,c.store,c.repository,github,agent,lambda _:None)
        runner.active_lease = acquire(c.root,state.run_id,identity=runner.host_identity,
            instance_id=runner.host_instance_id,process_id=os.getpid(),central_database=c.database)
        runner.lease_heartbeat = LeaseHeartbeat(c.root,runner.active_lease,central_database=c.database)
        runner.lease_heartbeat.start()
        self.addCleanup(c.stop_host,runner)
        return state, runner

    def test_later_git_effect_requires_current_lease_and_checkpoint(self):
        c = self.case
        state, runner = self.owned_later_state()
        before = target_bytes(c.root)
        for lease in (None, replace(runner.active_lease,host_instance_id='foreign-owner')):
            with self.subTest(lease=lease), self.assertRaisesRegex(AdoptionAuthorityError,'exclusive run ownership'):
                with process_effect_scope(lambda: effect_authority(state=state,root=c.root,
                        central_database=c.database,lease=lease,git_effect=True)):
                    c.repository.refresh_main_reference(c.root)
        c.store.save(replace(state,phase='REPOSITORY_CLEANUP'))
        with self.assertRaisesRegex(AdoptionAuthorityError,'checkpoint changed'):
            with process_effect_scope(lambda: effect_authority(state=state,root=c.root,
                    central_database=c.database,lease=runner.active_lease,git_effect=True)):
                c.repository.refresh_main_reference(c.root)
        self.assertEqual(target_bytes(c.root),before)

    def test_later_git_contention_serializes_unbind_and_denies_next_start(self):
        from engineering_platform.local_repository_binding import unbind_local_repository
        import sqlite3
        c = self.case
        state, runner = self.owned_later_state('RECONCILIATION')
        started, committed = threading.Event(), threading.Event()
        failures=[]
        def revoke():
            try:
                with sqlite3.connect(c.database,timeout=10) as connection:
                    started.set()
                    connection.execute('BEGIN IMMEDIATE')
                    unbind_local_repository(connection,project_id='project',repository_id='repo')
                committed.set()
            except BaseException as error:
                failures.append(error)
        with effect_authority(state=state,root=c.root,central_database=c.database,
                              lease=runner.active_lease,git_effect=True):
            thread=threading.Thread(target=revoke);thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(committed.wait(.1))
        thread.join(10)
        self.assertFalse(thread.is_alive());self.assertEqual(failures,[])
        self.assertTrue(committed.is_set())
        before=target_bytes(c.root)
        with self.assertRaisesRegex(AdoptionAuthorityError,'binding is unavailable'):
            with process_effect_scope(lambda: effect_authority(state=state,root=c.root,
                    central_database=c.database,lease=runner.active_lease,git_effect=True)):
                c.repository.refresh_main_reference(c.root)
        self.assertEqual(target_bytes(c.root),before)

    def test_native_clone_obeys_later_git_effect_authority(self):
        c = self.case
        state,runner=self.owned_later_state()
        destination=c.area/'authorized-clone'
        with process_effect_scope(lambda: effect_authority(state=state,root=c.root,
                central_database=c.database,lease=runner.active_lease,git_effect=True)):
            GitProvider().clone_branch(c.root,str(c.remote),'main',destination)
        self.assertTrue((destination/'.git').is_dir())
        c.unbind();before=target_bytes(c.root);denied=c.area/'denied-clone'
        with self.assertRaisesRegex(AdoptionAuthorityError,'binding is unavailable'):
            with process_effect_scope(lambda: effect_authority(state=state,root=c.root,
                    central_database=c.database,lease=runner.active_lease,git_effect=True)):
                GitProvider().clone_branch(c.root,str(c.remote),'main',denied)
        self.assertFalse(denied.exists())
        self.assertEqual(target_bytes(c.root),before)

    def restarted_git(self, revoked):
        c=self.case
        state,runner=self.owned_later_state()
        c.stop_host(runner)
        if revoked:
            c.unbind()
        before=target_bytes(c.root)
        child = r"""
import json, os, sys
from pathlib import Path
from engineering_platform.agent_state import StateStore
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_lease import acquire, release
from engineering_platform.execution_repository import SubprocessRepositoryClient
from engineering_platform.managed_adoption import AdoptionAuthorityError, effect_authority
from engineering_platform.providers import process_effect_scope
from tests.engineering.test_managed_adoption import LocalGitHubTransport
root, database, remote = map(Path,sys.argv[1:])
store=StateStore(root/'.engineering/engineering-runs',central_database=database,emit_local_projection=False)
state=store.load('adopt-run')
repository=SubprocessRepositoryClient(LocalGitHubTransport(remote))
runner=EngineeringRunner(root,store,repository,None,None,lambda _:None)
lease=acquire(root,state.run_id,identity=runner.host_identity,instance_id=runner.host_instance_id,
              process_id=os.getpid(),central_database=database)
try:
    with process_effect_scope(lambda: effect_authority(state=state,root=root,
            central_database=database,lease=lease,git_effect=True)):
        repository.refresh_main_reference(root)
    result='AUTHORIZED_FETCH'
except AdoptionAuthorityError:
    result='DENIED_BEFORE_GIT_START'
finally:
    release(root,lease,central_database=database)
print(json.dumps({'result':result,'pid':os.getpid(),'repair_iterations':store.load(state.run_id).repair_iterations,
    'adoption':store.load(state.run_id).managed_candidate_adoption}))
"""
        outcome=subprocess.run((sys.executable,'-c',child,str(c.root),str(c.database),str(c.remote)),
                               text=True,capture_output=True,timeout=30)
        self.assertEqual(outcome.returncode,0,outcome.stdout+outcome.stderr)
        receipt=json.loads(outcome.stdout)
        self.assertNotEqual(receipt['pid'],os.getpid())
        self.assertEqual(receipt['repair_iterations'],state.repair_iterations)
        self.assertEqual(receipt['adoption'],state.managed_candidate_adoption)
        self.assertEqual(receipt['result'],'DENIED_BEFORE_GIT_START' if revoked else 'AUTHORIZED_FETCH')
        if revoked:
            self.assertEqual(target_bytes(c.root),before)
        else:
            self.assertTrue((c.root/'.git/FETCH_HEAD').is_file())

    def test_real_process_later_git_revocation_preserves_all_metadata(self):
        self.restarted_git(True)

    def test_real_process_later_git_valid_resume_preserves_identity(self):
        self.restarted_git(False)

    def revoked_later_caller(self, caller):
        c=self.case
        state,runner=self.owned_later_state('IMPLEMENTATION' if caller=='finalization' else 'FINALIZATION')
        if caller=='poll':
            state=replace(state,pull_request=71,phase='WAIT_FOR_TERMINAL_EVIDENCE')
            c.store.save(state)
            from engineering_platform.execution_models import PullRequestEvidence
            def read(number):
                c.unbind(); snapshots.append(target_bytes(c.root))
                return PullRequestEvidence(number,'MERGED',False,True,head_branch=state.branch,
                    base_branch='main',head_sha=c.sha,merge_commit=c.sha)
            runner.github.pull_request=read
        snapshots, native_starts=[],[]
        def audit(event,args):
            if (event=='subprocess.Popen' and snapshots and args[2] is not None
                    and Path(args[2])==c.root
                    and any(command in args[1] for command in ('fetch','push','switch','checkout','merge','update-ref','add','commit','-d','-D'))):
                native_starts.append(args[1])
        sys.addaudithook(audit)
        real=c.transport.execute
        def transport(root,*args):
            if 'fetch' in args and not snapshots:
                c.unbind();snapshots.append(target_bytes(c.root))
            return real(root,*args)
        c.transport.execute=transport
        if caller=='poll': result=runner._poll(state)
        elif caller=='finalization': result=runner._start_finalization(state,71)
        elif caller=='reconciliation': result=runner._start_automatic_reconciliation(state)
        else: result=runner._cleanup(state)
        self.assertEqual((result.phase,result.next_action),('BLOCKED','managed_candidate_adoption_invalid'))
        self.assertEqual(len(snapshots),1)
        self.assertEqual(native_starts,[], 'withdrawal must deny the actual native Git process start')
        self.assertEqual(target_bytes(c.root),snapshots[0])
        self.assertEqual(result.managed_candidate_adoption,state.managed_candidate_adoption)
        self.assertEqual(result.repair_iterations,state.repair_iterations)
        self.assertEqual(runner.agent.prompts,[])
        self.assertEqual(runner.github.creates,0)

    def test_passive_merge_readback_revocation_denies_actual_fetch(self):
        self.revoked_later_caller('poll')

    def test_finalization_sync_revocation_denies_actual_fetch(self):
        self.revoked_later_caller('finalization')

    def test_reconciliation_sync_revocation_denies_actual_fetch(self):
        self.revoked_later_caller('reconciliation')

    def test_cleanup_revocation_denies_actual_fetch_and_branch_writes(self):
        self.revoked_later_caller('cleanup')
