"""Real public later-kind resumption and write-free promisor profile evidence."""
from dataclasses import replace
import os
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest

from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_models import AgentResult, PullRequestEvidence
from engineering_platform.execution_repository import GhCliClient
from engineering_platform.execution_lease import acquire, LeaseHeartbeat
from engineering_platform.validation_profile import changed_paths, ValidationProfileResolutionError
from tests.engineering.test_git_effect_authority_regressions import target_bytes
from tests.engineering import test_managed_adoption as adoption_fixture


class LaterAdoptionAndProfileAuthority(unittest.TestCase):
    def setUp(self):
        self.case = adoption_fixture.AdoptionLifecycleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def later_resume(self, revoke, *, provider_boundary=False):
        c=self.case
        agent,github=c.lifecycle_adapters()
        runner=EngineeringRunner(c.root,c.store,c.repository,github,agent,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        waiting=runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        self.assertEqual(waiting.phase,'WAIT_FOR_OPERATOR_MERGE')
        c.git('switch','main');c.git('merge','--ff-only','codex/existing')
        c.transport.command(c.root,'git','push','origin','main')
        runner.active_lease=acquire(c.root,waiting.run_id,identity=runner.host_identity,
            instance_id=runner.host_instance_id,process_id=os.getpid(),central_database=c.database)
        runner.lease_heartbeat=LeaseHeartbeat(c.root,runner.active_lease,central_database=c.database)
        runner.lease_heartbeat.start()
        with self.assertRaisesRegex(SystemExit,'external provider handoff'):
            runner._start_finalization(waiting,71)
        c.stop_host(runner)
        checkpoint=c.store.load('adopt-run')
        self.assertEqual((checkpoint.phase,checkpoint.transaction_kind),('FINALIZE_AGENT','FINALIZATION'))
        if revoke and not provider_boundary:c.unbind()
        before=target_bytes(c.root)
        snapshots=[before] if revoke and not provider_boundary else []
        starts,calls=[],[]
        def audit(event,args):
            if (event=='subprocess.Popen' and (not revoke or snapshots) and args[2] is not None and os.fsdecode(args[2])==str(c.root)
                    and any(part in args[1] for part in ('fetch','switch','merge','push','update-ref','commit','add','-d','-D'))):
                starts.append(args[1])
        sys.addaudithook(audit)
        def external_provider(root,prompt):
            calls.append('finalization')
            raise SystemExit('observed provider handoff without fixture Git')
        if provider_boundary:
            class ExternalProviderBoundary:
                def __getattr__(self,name):return getattr(agent,name)
                @property
                def invoke(self):
                    if not snapshots:
                        c.unbind();snapshots.append(target_bytes(c.root))
                    return external_provider
            resumed_agent=ExternalProviderBoundary()
        else:
            agent.invoke=external_provider
            resumed_agent=agent
        resumed=EngineeringRunner(c.root,c.store,c.repository,github,resumed_agent,lambda _:None)
        self.addCleanup(c.stop_host,resumed)
        if revoke:
            try:
                result=resumed.run(c.prompt,run_id='adopt-run',resume=True)
            except SystemExit as error:
                self.assertEqual(str(error),'observed provider handoff without fixture Git')
                result=c.store.load('adopt-run')
            self.assertEqual(starts,[], 'revoked later resume must deny every mutating host Git start')
            self.assertEqual(calls,[], 'revoked later resume must deny the mutating provider')
            self.assertEqual(len(snapshots),1)
            self.assertEqual(target_bytes(c.root),snapshots[0])
            self.assertEqual((result.phase,result.next_action),('BLOCKED','managed_candidate_adoption_invalid'))
        else:
            with self.assertRaisesRegex(SystemExit,'observed provider handoff without fixture Git'):
                resumed.run(c.prompt,run_id='adopt-run',resume=True)
            self.assertEqual(calls,['finalization'])
            self.assertTrue(any('fetch' in args for args in starts))
        after=c.store.load('adopt-run')
        self.assertEqual(after.managed_candidate_adoption,checkpoint.managed_candidate_adoption)
        self.assertEqual(after.publication_intent,checkpoint.publication_intent)
        self.assertEqual(after.repair_iterations,checkpoint.repair_iterations)
        self.assertEqual(github.creates,1)

    def test_public_finalization_resume_after_unbind_has_zero_native_effects(self):
        self.later_resume(True)

    def test_finalization_provider_start_revalidates_after_valid_sync(self):
        self.later_resume(True,provider_boundary=True)

    def test_public_finalization_valid_resume_preserves_original_handoff(self):
        self.later_resume(False)

    def partial_fixture(self, materialize):
        c=self.case
        c.transport.command(c.root,'git','push','origin','HEAD:refs/heads/codex/existing')
        for key in ('uploadpack.allowFilter','uploadpack.allowAnySHA1InWant'):
            subprocess.run(('git','--git-dir',str(c.remote),'config',key,'true'),check=True)
        local=c.area/'original-local-data'
        if (c.root/'.engineering').exists():shutil.copytree(c.root/'.engineering',local)
        shutil.rmtree(c.root)
        subprocess.run(('git','clone','-q','--filter=tree:0','--branch','codex/existing',
                        'file://'+str(c.remote),str(c.root)),check=True)
        if local.exists():shutil.copytree(local,c.root/'.engineering')
        c.git('branch','main',c.base)
        # Only the declared external Git transport is local. The canonical
        # declaration and GitHub-shaped origin/actual authority remain real.
        c.git('remote','set-url','origin','git@github.com:qualification/managed')
        ssh=c.area/'external-local-git.sh'
        ssh.write_text('#!/bin/sh\nexec git-upload-pack '+shlex.quote(str(c.remote))+'\n')
        ssh.chmod(0o700)
        c.git('config','core.sshCommand',str(ssh));c.git('config','ssh.variant','ssh')
        if materialize:
            # Explicit owning fixture preparation happens before the measured
            # admission/revocation boundary. Never restore metadata afterwards.
            self.assertEqual(c.git('diff','--name-only','main...HEAD'),'README.md')

    def test_partial_adoption_revocation_never_fetches_promisor_objects(self):
        c=self.case;self.partial_fixture(False)
        original=c.transport.execute;revoked=[]
        def external_transport(root,*args):
            result=original(root,*args)
            if 'ls-remote' in args and not revoked:
                c.unbind();revoked.append(target_bytes(c.root))
            return result
        c.transport.execute=external_transport
        agent,github=c.lifecycle_adapters()
        runner=EngineeringRunner(c.root,c.store,c.repository,github,agent,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        result=runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        self.assertEqual(len(revoked),1)
        self.assertEqual(target_bytes(c.root),revoked[0], 'revoked profile observation must keep all target Git metadata unchanged')
        self.assertEqual((result.phase,result.next_action),('BLOCKED','managed_candidate_adoption_invalid'))
        self.assertEqual((agent.prompts,github.creates,result.repair_iterations),([],0,0))

    def test_unavailable_partial_profile_is_write_free_and_not_empty_success(self):
        c=self.case;self.partial_fixture(False)
        before=target_bytes(c.root)
        with self.assertRaisesRegex(ValidationProfileResolutionError,'evidence is unavailable'):
            changed_paths(c.root,'main')
        self.assertEqual(target_bytes(c.root),before)

    def test_available_partial_profile_and_valid_adoption_preserve_delivery(self):
        c=self.case;self.partial_fixture(True)
        before=target_bytes(c.root)
        self.assertEqual(changed_paths(c.root,'main'),('README.md',))
        self.assertEqual(target_bytes(c.root),before)
        agent,github=c.lifecycle_adapters()
        runner=EngineeringRunner(c.root,c.store,c.repository,github,agent,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        result=runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE',result)
        self.assertEqual((github.creates,result.repair_iterations),(1,0))
        self.assertEqual(result.managed_candidate_adoption,c.selection)

    def test_invalid_diff_identity_never_becomes_empty_profile(self):
        with self.assertRaisesRegex(ValidationProfileResolutionError,'evidence is unavailable'):
            changed_paths(self.case.root,'nonexistent-control-base')

    def later_publication(self, kind, revoke):
        c=self.case
        agent,github=c.lifecycle_adapters()
        runner=EngineeringRunner(c.root,c.store,c.repository,github,agent,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        waiting=runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        self.assertEqual(waiting.phase,'WAIT_FOR_OPERATOR_MERGE')
        branch='codex/own-'+kind.lower()
        c.git('switch','-qc',branch)
        (c.root/'README.md').write_text('# Actual later delivery candidate\n')
        c.git('add','README.md');c.git('commit','-qm','later delivery candidate')
        sha=c.git('rev-parse','HEAD')
        state=replace(waiting,transaction_kind=kind,branch=branch,pull_request=None,
                      phase='FINALIZE_AGENT' if kind=='FINALIZATION' else 'RECONCILE_AGENT',
                      finalization_branch=branch if kind=='FINALIZATION' else waiting.finalization_branch)
        c.store.save(state)
        runner.active_lease=acquire(c.root,state.run_id,identity=runner.host_identity,
            instance_id=runner.host_instance_id,process_id=os.getpid(),central_database=c.database)
        runner.lease_heartbeat=LeaseHeartbeat(c.root,runner.active_lease,central_database=c.database)
        runner.lease_heartbeat.start()
        events,snapshots=[],[]
        def readback(number):
            if revoke=='readback' and not snapshots:
                c.unbind();snapshots.append(target_bytes(c.root))
            return PullRequestEvidence(number,'OPEN',True,True,head_branch=branch,base_branch='main',head_sha=sha)
        github.pull_request=readback
        class ExternalGitHubTransport:
            def github(self,*args):
                events.append(args[:2])
                if args[:2]==('pr','view'):
                    if revoke=='view' and not snapshots:
                        c.unbind();snapshots.append(target_bytes(c.root))
                    return json.dumps({'body':'One\\nTwo'})
                return ''
        client=GhCliClient(ExternalGitHubTransport(),repository='qualification/managed')
        def normalize(number):
            result=client.normalize_markdown_body(number)
            if revoke=='edit' and not snapshots:
                c.unbind();snapshots.append(target_bytes(c.root))
            return result
        github.normalize_markdown_body=normalize;github.ready=client.ready
        result=AgentResult('COMPLETE',branch=branch,pull_request=81,commit_sha=sha)
        after=(runner._advance_after_finalization_agent_result(state,result) if kind=='FINALIZATION'
               else runner._advance_after_reconciliation_agent_result(state,result))
        mutations=[event for event in events if event in (('pr','edit'),('pr','ready'))]
        expected=[] if revoke in ('readback','view') else [('pr','edit')] if revoke=='edit' else [('pr','edit'),('pr','ready')]
        self.assertEqual(mutations,expected,'withdrawn later publication must deny each next remote mutation')
        self.assertEqual(after.phase,'BLOCKED' if revoke else 'WAIT_FOR_OPERATOR_MERGE',after)
        if revoke:
            self.assertEqual(after.next_action,'managed_candidate_adoption_invalid')
            self.assertEqual(target_bytes(c.root),snapshots[0])
        self.assertEqual(after.managed_candidate_adoption,waiting.managed_candidate_adoption)
        self.assertEqual(after.publication_intent,waiting.publication_intent)
        self.assertEqual((after.repair_iterations,github.creates),(waiting.repair_iterations,1))

    def test_finalization_publication_unbind_during_remote_read_denies_edit_ready(self):
        self.later_publication('FINALIZATION','view')

    def test_reconciliation_publication_unbind_during_remote_read_denies_edit_ready(self):
        self.later_publication('RECONCILIATION','view')

    def test_finalization_publication_after_edit_denies_next_ready(self):
        self.later_publication('FINALIZATION','edit')

    def test_reconciliation_publication_after_edit_denies_next_ready(self):
        self.later_publication('RECONCILIATION','edit')

    def test_finalization_bound_publication_keeps_one_receipt(self):
        self.later_publication('FINALIZATION',None)

    def test_reconciliation_bound_publication_keeps_one_receipt(self):
        self.later_publication('RECONCILIATION',None)

    def test_missing_git_transport_is_unavailable_not_empty_profile(self):
        from unittest.mock import patch
        c=self.case
        before=target_bytes(c.root)
        missing=c.area/'missing-external-git';missing.mkdir()
        # Change only actual external executable availability. No authority,
        # profile function, storage, lease or clock is mocked.
        with patch.dict(os.environ,{'PATH':str(missing)}):
            with self.assertRaisesRegex(ValidationProfileResolutionError,'evidence is unavailable'):
                changed_paths(c.root,'main')
        self.assertEqual(target_bytes(c.root),before)
