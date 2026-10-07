"""Real installed host/store process; only external transports are deterministic."""
from pathlib import Path
import json,os,sys
from dataclasses import replace
from unittest.mock import patch
from types import SimpleNamespace
from engineering_platform.agent_state import StateStore
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_models import PullRequestEvidence
from engineering_platform.execution_repository import SubprocessRepositoryClient
from engineering_platform.managed_publication import PublicationCandidate
from tests.engineering.test_managed_adoption import LocalGitHubTransport
from tests.engineering import test_specialist_selection_disposition as qualified

spec=json.loads(Path(sys.argv[1]).read_text());mode,boundary=sys.argv[2:4]
root,data,prompt,remote=map(Path,(spec['root'],spec['data'],spec['prompt'],spec['remote']))
database=Path(spec['database']);repository=SubprocessRepositoryClient(LocalGitHubTransport(remote))
class Store(StateStore):
    def save(self,state,**kwargs):
        path=super().save(state,**kwargs)
        if mode=='start' and boundary in {'consumer','noop-consumer'} and any(r['kind']=='CONSUMER_RESULT' for r in state.specialist_records):
            os._exit(73)
        return path
store=Store(root/'.engineering/engineering-runs',central_database=database,emit_local_projection=False)
def git(*args):
    import subprocess
    return subprocess.check_output(('git',*args),cwd=root,text=True).strip()
fixture=SimpleNamespace(root=root,data=data,prompt=prompt,remote=remote,database=database,store=store,git=git)
case=qualified.SpecialistPipelineTests();case.fixture=fixture
Base=type(case.transport(optional_fail=boundary=='assurance-empty'))
class Model(Base):
    def log(self,role):
        with (data/'specialist-model-calls.jsonl').open('a') as file:
            file.write(json.dumps({'role':role,'mode':mode})+'\n');file.flush();os.fsync(file.fileno())
    def review(self,root,selected,objective,evidence=None):
        self.log(selected.reviewer)
        if mode=='start' and boundary=='dispatch' and selected.reviewer not in {'quality','security'}:
            os._exit(73)
        if mode=='start' and boundary in {'assurance','assurance-empty'} and selected.reviewer=='quality':
            os._exit(73)
        if mode=='resume' and selected.reviewer not in {'quality','security'}:
            raise AssertionError('Uncertain optional invocation was retried')
        result=super().review(root,selected,objective,evidence)
        return replace(result,findings=()) if boundary=='noop-consumer' and selected.reviewer not in {'quality','security'} else result
    def invoke(self,root,prompt):
        self.log('implementation')
        if mode=='resume' and boundary!='dispatch':
            raise AssertionError('Durable primary consumer result was replayed through model')
        if boundary=='noop-consumer':
            import subprocess
            completed=subprocess.run((sys.executable,'-m','unittest','discover','-s','tests'),cwd=root,capture_output=True,text=True)
            assert completed.returncode==0,completed.stderr
            from engineering_platform.execution_models import AgentResult
            return AgentResult('COMPLETE',terminal_condition='repository_reconciled',commit_sha=git('rev-parse','HEAD'),
                validation_evidence=({'command':'python3 -m unittest discover -s tests','result':'PASS: actual isolated repository tests'},))
        if boundary=='recovered':
            import subprocess
            child=subprocess.Popen((sys.executable,'-c','import time;time.sleep(30)'),start_new_session=True)
            try:
                self.process_callback({'pid':child.pid,'process_group':child.pid})
                return super().invoke(root,prompt)
            finally:
                child.terminate();child.wait(timeout=10);self.process_callback(None)
        return super().invoke(root,prompt)
    def set_process_callback(self,callback):
        self.process_callback=callback
class GitHub:
    def publication_candidates(self,*args):
        p=data/'specialist-remote.json'
        return [PublicationCandidate(**json.loads(p.read_text()))] if p.exists() else []
    def create_draft_publication(self,repository,branch,base,title,body):
        candidate=PublicationCandidate(71,repository,repository,branch,base,git('rev-parse','HEAD'),'OPEN',True)
        with (data/'specialist-remote.json').open('x') as file:
            from dataclasses import asdict
            json.dump(asdict(candidate),file)
            file.flush();os.fsync(file.fileno())
        if mode=='start' and boundary=='publication':
            os._exit(73)
    def pull_request(self,number):
        candidate=self.publication_candidates()[0]
        return PullRequestEvidence(number,'OPEN',True,True,head_branch=candidate.branch,base_branch='main',head_sha=candidate.head_sha)
    def ready(self,number):pass
    def normalize_markdown_body(self,number):return False
    def find_open_pull_request(self,*args):return None
if mode=='start' and boundary=='recovered':
    from engineering_platform import execution_host
    os.environ['ENGINEERING_PLATFORM_TEST_INTERRUPT_PROVIDER_ONCE']='specialist-run:EXECUTE_AGENT'
    terminal=execution_host.record_replacement_terminal
    def crash_after_recovery(*args,**kwargs):
        recorded=terminal(*args,**kwargs)
        if kwargs.get('outcome')=='SUCCESS' and recorded:os._exit(73)
        return recorded
    execution_host.record_replacement_terminal=crash_after_recovery
if mode=='start' and boundary=='artifact':
    from engineering_platform import execution_host
    persist=execution_host.persist_recovery_agent_result
    def crash_after_catalog(*args,**kwargs):
        reference=persist(*args,**kwargs)
        os._exit(73)
    execution_host.persist_recovery_agent_result=crash_after_catalog
runner=EngineeringRunner(root,store,repository,GitHub(),Model(),lambda _:None)
try:
    with patch('engineering_platform.codex_capacity.read_remaining_percent',return_value=100),patch('engineering_platform.execution_host.provider_readiness_failures',return_value=()):
        result=runner.run(prompt,run_id='specialist-run',resume=True,owner_authorized=True)
    if mode=='start' and boundary=='repair-assurance':
        from engineering_platform.execution_lease import acquire,LeaseHeartbeat
        runner.active_lease=acquire(root,'specialist-run',identity=runner.host_identity,instance_id=runner.host_instance_id,
            process_id=os.getpid(),central_database=database)
        runner.lease_heartbeat=LeaseHeartbeat(root,runner.active_lease,central_database=database)
        runner.lease_heartbeat.start()
        model=runner.agent
        original_review=model.review
        def repaired(root,prompt):
            model.log('repair')
            (root/'README.md').write_text('# Qualified repaired delivery\n\nAcceptance sentence plus bounded correction.\n')
            git('add','README.md');git('commit','-qm','bounded same-PR repair')
            candidate=json.loads((data/'specialist-remote.json').read_text())
            candidate['head_sha']=git('rev-parse','HEAD')
            (data/'specialist-remote.json').write_text(json.dumps(candidate))
            from engineering_platform.execution_models import AgentResult
            return AgentResult('COMPLETE',git('branch','--show-current'),pull_request=71,commit_sha=git('rev-parse','HEAD'))
        def interrupted(root,selection,objective,evidence=None):
            if selection.reviewer=='quality':os._exit(73)
            return original_review(root,selection,objective,evidence)
        model.invoke=repaired;model.review=interrupted
        with patch('engineering_platform.codex_capacity.read_remaining_percent',return_value=100),patch('engineering_platform.execution_host.provider_readiness_failures',return_value=()):
            repaired_result=runner._repair(result,'Correct the bounded README detail after hosted validation failure.')
        raise AssertionError((repaired_result.phase,repaired_result.next_action,repaired_result.diagnostic))
    assert result.phase==('COMPLETE' if boundary=='noop-consumer' else 'WAIT_FOR_OPERATOR_MERGE'),result
    assert result.repair_iterations==2,result
    (data/'specialist-process-result.json').write_text(json.dumps(result.to_dict(),sort_keys=True)+'\n')
finally:
    if runner.lease_heartbeat:
        from engineering_platform.execution_lease import release
        release(root,runner.lease_heartbeat.stop(),central_database=database)
