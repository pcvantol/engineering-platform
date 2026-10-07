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
        if mode=='start' and boundary=='consumer' and any(r['kind']=='CONSUMER_RESULT' for r in state.specialist_records):
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
        return super().review(root,selected,objective,evidence)
    def invoke(self,root,prompt):
        self.log('implementation')
        if mode=='resume' and boundary!='dispatch':
            raise AssertionError('Durable primary consumer result was replayed through model')
        return super().invoke(root,prompt)
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
runner=EngineeringRunner(root,store,repository,GitHub(),Model(),lambda _:None)
try:
    with patch('engineering_platform.codex_capacity.read_remaining_percent',return_value=100),patch('engineering_platform.execution_host.provider_readiness_failures',return_value=()):
        result=runner.run(prompt,run_id='specialist-run',resume=True,owner_authorized=True)
    assert result.phase=='WAIT_FOR_OPERATOR_MERGE',result
    assert result.repair_iterations==2,result
    (data/'specialist-process-result.json').write_text(json.dumps(result.to_dict(),sort_keys=True)+'\n')
finally:
    if runner.lease_heartbeat:
        from engineering_platform.execution_lease import release
        release(root,runner.lease_heartbeat.stop(),central_database=database)
