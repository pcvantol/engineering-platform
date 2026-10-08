"""Real control starts, canonical withdrawal and mandatory assurance continuity."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import sys
import unittest

import engineering_platform
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_lease import acquire, LeaseHeartbeat
from engineering_platform.managed_adoption import profile_digest
from engineering_platform.storage import record_validation_profile, sqlite_connection
from tests.engineering import test_managed_adoption as adoption
from tests.engineering.test_git_effect_authority_regressions import target_bytes


class ControlAssuranceAuthority(unittest.TestCase):
    def setUp(self):
        self.case = adoption.AdoptionLifecycleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def pipeline(self, boundary=None):
        c = self.case
        # The real repository's ordinary documentation test produces ignored output.
        c.git('switch','main')
        (c.root/'.gitignore').write_text((c.root/'.gitignore').read_text()+'validation-output.txt\n')
        (c.root/'tests/test_docs.py').write_text("import unittest\nfrom pathlib import Path\nclass Documentation(unittest.TestCase):\n    def test_heading(self):\n        Path('validation-output.txt').write_text('true control output\\n')\n        self.assertTrue(Path('README.md').read_text().startswith('# '))\n")
        c.git('add','.gitignore','tests/test_docs.py');c.git('commit','-qm','real owning control')
        c.base=c.git('rev-parse','HEAD');c.transport.command(c.root,'git','push','origin','main')
        c.git('branch','-f','codex/existing','main');c.git('switch','codex/existing')
        (c.root/'README.md').write_text('# Candidate\n\nApproved documentation.\n')
        c.git('add','README.md');c.git('commit','-qm','selected documentation')
        c.sha=c.git('rev-parse','HEAD')
        c.selection={**c.selection,'candidate_sha':c.sha,'base_sha':c.base,'validation_profile_digest':profile_digest(c.root,c.sha,0)}
        revoked, starts, reviews = [], [], []
        real = c.transport.execute
        def transport(root,*args):
            result=real(root,*args)
            try: state=c.store.load('adopt-run')
            except adoption.StateError: state=None
            if (state is not None and not revoked and state.managed_candidate_adoption is not None
                    and boundary=='before_control' and state.admission_decision=='PASS' and 'rev-parse' in args):
                c.unbind();revoked.append(target_bytes(c.root))
            return result
        c.transport.execute=transport
        def audit(event,args):
            if event=='subprocess.Popen' and args[2] is not None and os.fsdecode(args[2])==str(c.root):
                command=list(args[1])
                if '--check' in command or 'unittest' in command:
                    starts.append((bool(revoked),command))
        sys.addaudithook(audit)
        agent,github=c.lifecycle_adapters();review=agent.review
        def external_model(*args,**kwargs):
            role=args[1].reviewer;reviews.append((role,bool(revoked)))
            if boundary=='pause_in_quality' and role=='quality':
                raise SystemExit('external model interrupted after actual start')
            result=review(*args,**kwargs)
            if boundary in {'between_reviews','between_reviews_wait','checkpoint_during_quality','phase_during_quality'} and role=='quality':
                if boundary=='between_reviews_wait':
                    initial=runner.active_lease.expires_at
                    time.sleep(17)
                    with sqlite_connection(c.database) as db:
                        renewed=db.execute('SELECT expires_at FROM execution_run_leases WHERE lease_id=?',(runner.active_lease.lease_id,)).fetchone()[0]
                    self.assertGreater(renewed,initial,'review wait must permit the real heartbeat')
                    self.assertIsNone(runner.lease_heartbeat.error)
                if boundary in {'checkpoint_during_quality','phase_during_quality'}:
                    checkpoint=c.store.load('adopt-run')
                    c.store.save(replace(checkpoint,owner_authorized=False) if boundary=='checkpoint_during_quality' else replace(checkpoint,phase='LOCAL_REPOSITORY_VALIDATION'))
                else:c.unbind()
                revoked.append(target_bytes(c.root))
            return result
        agent.review=external_model
        if boundary in {'before_quality','pause_after_quality'}:
            original_agent=agent
            class BeforeReviewAdapter:
                def __getattr__(self,name): return getattr(original_agent,name)
                @property
                def review(self):
                    try: checkpoint=c.store.load('adopt-run')
                    except adoption.StateError: checkpoint=None
                    if boundary=='pause_after_quality' and checkpoint is not None and checkpoint.assurance_reviews:
                        raise SystemExit('external adapter paused before security')
                    if boundary=='before_quality' and checkpoint is not None and checkpoint.phase=='QUALITY_CONTROL_AGENT' and not revoked:
                        c.unbind();revoked.append(target_bytes(c.root))
                    return external_model
            agent=BeforeReviewAdapter()
        runner=EngineeringRunner(c.root,c.store,c.repository,github,agent,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        try:
            result=runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        except SystemExit:
            if boundary not in {'pause_after_quality','pause_in_quality'}: raise
            c.stop_host(runner)
            return c.store.load('adopt-run'),runner,agent,github
        with sqlite_connection(c.database) as connection:
            rows=[tuple(r) for r in connection.execute("SELECT phase,role FROM provider_invocations WHERE run_id='adopt-run' ORDER BY ordinal")]
        from engineering_platform.provider_usage import provider_usage_summary
        measured=provider_usage_summary(c.root,'adopt-run',central_database=c.database)
        if reviews:
            self.assertEqual(measured['provider_invocation_count'],len(reviews))
        else:
            self.assertEqual(measured,{'invocation_detail':'UNAVAILABLE'})
        if boundary is None:
            self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE',result)
            self.assertEqual(github.creates,1)
            self.assertEqual([r[0] for r in reviews],['quality','security'])
            self.assertTrue((c.root/'validation-output.txt').is_file())
        else:
            self.assertEqual(len(revoked),1)
            self.assertEqual(target_bytes(c.root),revoked[0], 'withdrawal must prevent every subsequent control target write')
            self.assertFalse(any(after for after,_ in starts), 'withdrawal must prevent every subsequent native control start')
            self.assertFalse(any(after for _,after in reviews), 'withdrawal must prevent every subsequent reviewer start')
            self.assertEqual((result.phase,result.next_action),('BLOCKED','managed_candidate_adoption_invalid'))
            self.assertEqual(github.creates,0)
            expected=['quality'] if boundary in {'between_reviews','between_reviews_wait','checkpoint_during_quality','phase_during_quality'} else []
            self.assertEqual([r[0] for r in reviews],expected)
            self.assertEqual(rows,[(phase,role) for role in expected for phase in ('MANDATORY_ASSURANCE_DISPATCH','MANDATORY_ASSURANCE')])
            self.assertEqual([r['reviewer'] for r in result.assurance_reviews],expected)
            self.assertEqual(agent.prompts,[])
            self.assertEqual(result.repair_iterations,0)
        return result,runner,agent,github

    def test_original_control_regression_has_zero_effects(self):
        self.pipeline('before_control')

    def test_valid_controls_both_reviews_and_publication(self):
        self.pipeline()

    def test_last_control_to_quality_withdrawal(self):
        self.pipeline('before_quality')

    def test_quality_to_security_preserves_true_first_result(self):
        self.pipeline('between_reviews')

    def control_sequence(self, loss):
        c=self.case
        state,runner,agent,github=self.pipeline()
        c.stop_host(runner)
        # A separate actual owning fixture run has its own immutable profile.
        c.git('switch','-qc','codex/control-existing')
        selection={**state.managed_candidate_adoption,'run_id':'control-run','branch':'codex/control-existing'}
        state=replace(state,run_id='control-run',phase='LOCAL_REPOSITORY_VALIDATION',
                      managed_candidate_adoption=selection,branch='codex/control-existing',implementation_branch='codex/control-existing',publication_intent=None,assurance_reviews=())
        c.store.save(state)
        runner.active_lease=acquire(c.root,state.run_id,identity=runner.host_identity,
            instance_id=runner.host_instance_id,process_id=os.getpid(),central_database=c.database)
        runner.lease_heartbeat=LeaseHeartbeat(c.root,runner.active_lease,central_database=c.database);runner.lease_heartbeat.start()
        # Withdrawal is performed inside the already authorized first real child.
        revoke="from engineering_platform.local_repository_binding import unbind_local_repository\nfrom engineering_platform.storage import sqlite_connection\nwith sqlite_connection(Path(%r)) as db: unbind_local_repository(db,project_id='project',repository_id='repo')" % str(c.database)
        if loss=='checkpoint':
            revoke="from engineering_platform.agent_state import StateStore\nfrom dataclasses import replace\ns=StateStore(Path(%r),central_database=Path(%r),emit_local_projection=False)\nt=s.load('control-run');s.save(replace(t,owner_authorized=False))" % (str(c.root/'.engineering/engineering-runs'),str(c.database))
        if loss=='lease':
            revoke="from engineering_platform.storage import sqlite_connection\nwith sqlite_connection(Path(%r)) as db: db.execute(\"UPDATE execution_run_leases SET lease_state='RELEASED' WHERE run_id='control-run'\")" % str(c.database)
        first=("import sys\nsys.path.insert(0,%r)\nfrom pathlib import Path\nPath('first-output.txt').write_text('completed first control')\n" % str(Path(engineering_platform.__file__).parent.parent))+revoke
        if loss=='authority_wait':
            first += "\nimport time\ntime.sleep(17)\nfrom engineering_platform.storage import sqlite_connection\nwith sqlite_connection(Path(%r)) as db: current=db.execute('SELECT expires_at FROM execution_run_leases WHERE run_id=\"control-run\" AND lease_state=\"ACTIVE\"').fetchone()[0]\nassert current>%r, 'control wait must permit actual lease renewal'" % (str(c.database),runner.active_lease.expires_at)
        second="from pathlib import Path\nPath('second-output.txt').write_text('must not start')"
        bindings=tuple({'validation_id':name,'category':'repository','control_identity':name,'command':[sys.executable,'-c',code],'required':True} for name,code in (('first',first),('second',second)))
        record_validation_profile(c.root,run_id=state.run_id,selected_validation_tier='DOCUMENTATION',validation_profile_version='1.0',required_validation_controls=('first','second'),control_bindings=bindings,recorded_at=datetime.now(timezone.utc).isoformat(),central_database=c.database)
        before=target_bytes(c.root)
        result=runner._execute_required_validation_controls(state)
        after=target_bytes(c.root)
        changed={p for p in set(before)|set(after) if before.get(p)!=after.get(p)}
        self.assertEqual(changed,{'first-output.txt'}, 'only the authorized first control may write target bytes')
        self.assertEqual((result.phase,result.next_action),('BLOCKED','managed_candidate_adoption_invalid'))
        with sqlite_connection(c.database) as connection:
            commands=[tuple(r) for r in connection.execute("SELECT validation_id FROM execution_validation_command_invocations WHERE run_id='control-run'")]
            controls=[tuple(r) for r in connection.execute("SELECT validation_id,execution_status,result FROM execution_validation_control_results WHERE run_id='control-run'")]
        self.assertEqual(commands,[('first',)])
        self.assertEqual(controls,[('first','EXECUTED','PASS')])
        self.assertEqual(result.repair_iterations,state.repair_iterations)
        if loss=="checkpoint":
            self.assertFalse(c.store.load(state.run_id).owner_authorized, "blocked audit must preserve canonical withdrawal")

    def test_between_controls_preserves_first_result(self):
        self.control_sequence('authority')

    def test_checkpoint_loss_at_second_control(self):
        self.control_sequence('checkpoint')

    def test_lease_loss_at_second_control(self):
        self.control_sequence('lease')


    def process_resume(self, revoked, *, interrupted=False):
        c=self.case
        state,runner,agent,github=self.pipeline('pause_in_quality' if interrupted else 'pause_after_quality')
        self.assertEqual([r['reviewer'] for r in state.assurance_reviews],[] if interrupted else ['quality'])
        if revoked:c.unbind()
        before=target_bytes(c.root)
        child=r"""
import json,os,sys
from pathlib import Path
from unittest.mock import patch
from engineering_platform.agent_state import StateStore
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_repository import SubprocessRepositoryClient
from tests.engineering import test_managed_adoption as fixture
root,database,remote,prompt=map(Path,sys.argv[1:])
c=fixture.AdoptionLifecycleTests();c.root=root;c.database=database;c.remote=remote
c.store=StateStore(root/'.engineering/engineering-runs',central_database=database,emit_local_projection=False)
c.transport=fixture.LocalGitHubTransport(remote);c.repository=SubprocessRepositoryClient(c.transport)
agent,github=c.lifecycle_adapters();review=agent.review;calls=[];starts=[]
def external_model(*args,**kwargs):
    calls.append(args[1].reviewer);return review(*args,**kwargs)
agent.review=external_model
def audit(event,args):
    if event=='subprocess.Popen' and args[2] is not None and os.fsdecode(args[2])==str(root) and ('--check' in args[1] or 'unittest' in args[1]):starts.append(list(args[1]))
sys.addaudithook(audit)
r=EngineeringRunner(root,c.store,c.repository,github,agent,lambda _:None)
try:
    with patch('engineering_platform.execution_host.provider_readiness_failures',return_value=()):
        result=r.run(prompt,run_id='adopt-run',resume=True)
    print(json.dumps({'pid':os.getpid(),'phase':result.phase,'next_action':result.next_action,'calls':calls,'controls':starts,'creates':github.creates,'repairs':result.repair_iterations,'repair_audit':result.repair_audit,'reviews':[x['reviewer'] for x in result.assurance_reviews]}))
finally:c.stop_host(r)
"""
        outcome=subprocess.run((sys.executable,'-c',child,str(c.root),str(c.database),str(c.remote),str(c.prompt)),text=True,capture_output=True,timeout=40)
        self.assertEqual(outcome.returncode,0,outcome.stdout+outcome.stderr)
        receipt=json.loads(outcome.stdout.splitlines()[-1])
        self.assertNotEqual(receipt['pid'],os.getpid())
        self.assertEqual(receipt['repairs'],state.repair_iterations)
        self.assertEqual(receipt['repair_audit'],list(state.repair_audit))
        self.assertEqual(receipt['controls'],[], 'new process must preserve completed control evidence')
        if revoked or interrupted:
            self.assertEqual(receipt['calls'],[], 'revoked recovery must not replay any reviewer')
            self.assertEqual(receipt['creates'],0)
            self.assertEqual(receipt['phase'],'BLOCKED')
            self.assertEqual(receipt['reviews'],[] if interrupted else ['quality'])
            self.assertEqual(target_bytes(c.root),before)
        else:
            self.assertEqual(receipt['calls'],['security'], 'valid recovery must preserve completed quality result')
            self.assertEqual(receipt['creates'],1)
            self.assertEqual(receipt['phase'],'WAIT_FOR_OPERATOR_MERGE')
            self.assertEqual(receipt['reviews'],['quality','security'])

    def test_real_new_process_revoked_assurance_preserves_results_and_budget(self):
        self.process_resume(True)

    def test_real_new_process_valid_assurance_does_not_replay_quality(self):
        self.process_resume(False)

    def native_model_boundary(self, revoked, *, bounded=False, spawn=False):
        from engineering_platform.capability_review import run_reviews, ReviewerSelection
        from engineering_platform.execution_executor import CodexCliClient
        from engineering_platform.providers import CodexCliProvider, process_effect_scope, model_effect_scope
        from engineering_platform.managed_adoption import effect_authority, AdoptionAuthorityError
        c=self.case
        state,runner,agent,github=self.pipeline()
        runner.active_lease=acquire(c.root,state.run_id,identity=runner.host_identity,
            instance_id=runner.host_instance_id,process_id=os.getpid(),central_database=c.database)
        runner.lease_heartbeat=LeaseHeartbeat(c.root,runner.active_lease,central_database=c.database);runner.lease_heartbeat.start()
        launcher=c.area/'external-model'
        actual=c.data/'actual-model-starts.jsonl'
        launcher.write_text('#!'+sys.executable+'\nimport json\nfrom pathlib import Path\nwith Path('+repr(str(actual))+').open("a") as f:f.write("actual-native-model\\n")\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":json.dumps({"contract_version":"3.0","contribution":"review","recommendations":[],"findings":[],"coverage":{},"finding_dispositions":{}})}}))\n')
        launcher.chmod(0o700)
        withdrawn=[]
        class ExternalNativeModel(CodexCliProvider):
            def _arguments(self,args):
                command=super()._arguments(args)
                if revoked and 'exec' in args and not withdrawn:
                    c.unbind();withdrawn.append(target_bytes(c.root))
                return command
        provider=ExternalNativeModel();provider._executable=str(launcher)
        authority=lambda:effect_authority(state=state,root=c.root,central_database=c.database,lease=runner.active_lease)
        starts=[]
        def call():
            if bounded or spawn:
                with model_effect_scope(lambda:starts.append('started')), process_effect_scope(authority,verify_exit=False):
                    if spawn:
                        child=provider.spawn_invocation(c.root,('codex','exec'),environment=dict(os.environ))
                        child.communicate(timeout=10)
                    else:
                        provider.invoke(c.root,('codex','exec'),input_text='exact source',max_output_bytes=65536,timeout=10)
            else:
                return run_reviews(c.root,(ReviewerSelection('quality','owning',1.0),),'review',CodexCliClient(provider),authority=authority,started=lambda:starts.append('started'))
        if revoked:
            with self.assertRaises(AdoptionAuthorityError):call()
            self.assertEqual(starts,[], 'denied native reviewer must not have an executed dispatch')
            self.assertFalse(actual.exists(), 'denied native model process must not start')
            self.assertEqual(target_bytes(c.root),withdrawn[0])
        else:
            result=call()
            if result is not None:self.assertFalse(result[0].failed)
            self.assertEqual(starts,['started'])
            self.assertEqual(actual.read_text(),'actual-native-model\n')

    def test_native_mandatory_model_actual_start_rechecks_authority(self):
        self.native_model_boundary(True)

    def test_native_mandatory_model_valid_start(self):
        self.native_model_boundary(False)

    def test_bounded_model_actual_start_rechecks_authority(self):
        self.native_model_boundary(True,bounded=True)

    def test_bounded_model_valid_start(self):
        self.native_model_boundary(False,bounded=True)

    def test_spawn_model_actual_start_rechecks_authority(self):
        self.native_model_boundary(True,spawn=True)

    def test_spawn_model_valid_start(self):
        self.native_model_boundary(False,spawn=True)

    def test_control_wait_keeps_real_heartbeat_and_revocation_available(self):
        self.control_sequence('authority_wait')

    def test_review_wait_keeps_real_heartbeat_and_revocation_available(self):
        self.pipeline('between_reviews_wait')

    def test_checkpoint_withdrawal_during_quality_is_not_restored_by_result_audit(self):
        result,*_=self.pipeline('checkpoint_during_quality')
        self.assertFalse(result.owner_authorized)
        self.assertFalse(self.case.store.load(result.run_id).owner_authorized)

    def test_phase_change_during_quality_prevents_security_start(self):
        self.pipeline('phase_during_quality')

    def test_stale_phase_transition_cannot_restore_current_authority(self):
        from engineering_platform.execution_models import AgentResult
        c=self.case
        state,runner,agent,github=self.pipeline()
        c.store.save(replace(state,owner_authorized=False))
        before=target_bytes(c.root)
        actual=runner._execute_required_validation_controls(state)
        self.assertEqual(actual.next_action,'managed_candidate_adoption_invalid')
        self.assertFalse(actual.owner_authorized)
        self.assertEqual(target_bytes(c.root),before)
        c.store.save(replace(state,owner_authorized=False))
        actual,_=runner._run_quality_assurance(state,AgentResult('COMPLETE',state.branch,commit_sha=c.sha))
        self.assertEqual(actual.next_action,'managed_candidate_adoption_invalid')
        self.assertFalse(actual.owner_authorized)
        self.assertEqual(target_bytes(c.root),before)

    def test_started_uncertain_review_new_process_has_no_replay(self):
        self.process_resume(False,interrupted=True)

    def test_revoked_started_uncertain_review_new_process_has_no_replay(self):
        self.process_resume(True,interrupted=True)
