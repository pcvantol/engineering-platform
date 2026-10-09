import os,sys,json,time,socket,struct,threading,subprocess,hashlib
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,os.getcwd())
from engineering_platform.capability_review import SocketReviewRequest,review_request_bytes
from engineering_platform.qualification_runtime import receive,QualificationReviewBackend
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_repository import SubprocessRepositoryClient
from engineering_platform.agent_state import StateStore
from engineering_platform.storage import sqlite_connection
from tests.engineering import test_managed_adoption as fixture
from tests.engineering.test_git_effect_authority_regressions import target_bytes
from unittest.mock import patch

def child(specfile,mode):
 spec=json.loads(Path(specfile).read_text());c=fixture.AdoptionLifecycleTests()
 for field in ('root','database','remote','prompt','data'):setattr(c,field,Path(spec[field]))
 c.store=StateStore(c.root/'.engineering/engineering-runs',central_database=c.database,emit_local_projection=False)
 c.transport=fixture.LocalGitHubTransport(c.remote);c.repository=SubprocessRepositoryClient(c.transport)
 base,github=c.lifecycle_adapters()
 class ExternalBackend(QualificationReviewBackend):
  def __getattr__(self,key):return getattr(base,key)
  def prepare_review(self,root,selection,objective,evidence=None):
   if mode=='resume':return super().prepare_review(root,selection,objective,evidence)
   payload=review_request_bytes(root,selection,objective,evidence);front,back=socket.socketpair();front.settimeout(300)
   def accept_and_crash():
    length=struct.unpack('!I',receive(back,4))[0];request=receive(back,length);assert request==payload
    with (c.data/'r10-actual-acceptances.jsonl').open('a') as f:
     f.write(json.dumps({'pid':os.getpid(),'id':selection.transport_invocation_id,'digest':hashlib.sha256(request).hexdigest()})+'\n');f.flush();os.fsync(f.fileno())
    os._exit(73)
   threading.Thread(target=accept_and_crash,daemon=True).start();return SocketReviewRequest(front,payload)
 adapter=ExternalBackend();runner=EngineeringRunner(c.root,c.store,c.repository,github,adapter,lambda _:None)
 try:
  with patch('engineering_platform.execution_host.provider_readiness_failures',return_value=()):
   state=runner.run(c.prompt,run_id='adopt-run',owner_authorized=mode=='start',managed_candidate=spec['selection'] if mode=='start' else None,resume=mode=='resume')
  print(json.dumps({'pid':os.getpid(),'phase':state.phase,'next_action':state.next_action,'acceptances':getattr(adapter,'review_acceptances',[]),'creates':github.creates,'primary_calls':len(base.prompts),'repair_iterations':state.repair_iterations,'launch_events':state.assurance_launch_events}),flush=True)
 finally:c.stop_host(runner)

if len(sys.argv)>1:child(sys.argv[1],sys.argv[2]);raise SystemExit()
c=fixture.AdoptionLifecycleTests();c.setUp()
try:
 specfile=c.area/'r10-actual-process.json';specfile.write_text(json.dumps({**{key:str(getattr(c,key)) for key in ('root','database','remote','prompt','data')},'selection':c.selection}))
 command=(sys.executable,'-B',str(Path(__file__).resolve()),str(specfile))
 first=subprocess.run((*command,'start'),text=True,capture_output=True,timeout=45);assert first.returncode==73,(first.returncode,first.stdout,first.stderr)
 interrupted=c.store.load('adopt-run');before=target_bytes(c.root)
 with sqlite_connection(c.database) as db:
  expiry=db.execute("SELECT expires_at FROM execution_run_leases WHERE run_id='adopt-run' AND lease_state='ACTIVE'").fetchone()[0]
  before_rows=db.execute("SELECT COUNT(*) FROM provider_invocations WHERE run_id='adopt-run'").fetchone()[0]
 delay=max(0,(datetime.fromisoformat(expiry)-datetime.now(timezone.utc)).total_seconds())
 print(json.dumps({'stage':'CRASHED_BEFORE_ACK','exit':first.returncode,'launch_events':interrupted.assurance_launch_events,'provider_rows':before_rows,'natural_wait_seconds':delay}),flush=True)
 time.sleep(delay+.1)
 resumed=subprocess.run((*command,'resume'),text=True,capture_output=True,timeout=45);assert resumed.returncode==0,(resumed.stdout,resumed.stderr)
 receipt=json.loads(resumed.stdout.splitlines()[-1]);after=c.store.load('adopt-run')
 with sqlite_connection(c.database) as db:after_rows=db.execute("SELECT COUNT(*) FROM provider_invocations WHERE run_id='adopt-run'").fetchone()[0]
 accepted=[json.loads(line) for line in (c.data/'r10-actual-acceptances.jsonl').read_text().splitlines()]
 assert receipt['phase']=='BLOCKED' and receipt['next_action']=='assurance_start_uncertain',receipt
 assert receipt['acceptances']==[] and receipt['creates']==0 and receipt['primary_calls']==0
 assert after.repair_iterations==interrupted.repair_iterations==0
 assert after.assurance_launch_events==interrupted.assurance_launch_events
 assert before_rows==after_rows==0
 assert target_bytes(c.root)==before
 assert len(accepted)==1 and accepted[0]['pid']!=receipt['pid']
 print(json.dumps({'stage':'NEW_PROCESS_RESUME','status':'PASS','receipt':receipt,'accepted_before_crash':accepted,'same_launch_history':True,'same_provider_rows':True,'target_bytes_unchanged':True,'waited_actual_lease_seconds':delay}),flush=True)
finally:c.doCleanups()
