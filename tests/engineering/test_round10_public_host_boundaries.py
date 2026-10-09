"""R10 real public-host discriminators: metadata, ACK and stale grants."""
from dataclasses import replace
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import engineering_platform
from engineering_platform import capability_review as cr, effect_provider
from engineering_platform.execution_executor import CodexCliClient
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.managed_adoption import profile_digest
from engineering_platform.providers import CodexCliProvider
from engineering_platform.qualification_runtime import QualificationReviewBackend, receive
from engineering_platform.storage import load_validation_context, sqlite_connection
from tests.engineering import test_managed_adoption as adoption
from tests.engineering import test_specialist_selection_disposition as specialist


class Round10PublicHostBoundaries(unittest.TestCase):
    def last_control(self, change):
        c = adoption.AdoptionLifecycleTests(); c.setUp(); self.addCleanup(c.doCleanups)
        marker = c.area / 'committed-control-checkpoint.json'
        child = ('import unittest,sys,json\nfrom pathlib import Path\n'
            + 'sys.path.insert(0,' + repr(str(Path(engineering_platform.__file__).parent.parent)) + ')\n'
            + 'from engineering_platform.agent_state import StateStore\nfrom dataclasses import replace\n'
            + 'class Documentation(unittest.TestCase):\n    def test_heading(self):\n'
            + '        self.assertTrue(Path("README.md").read_text().startswith("# "))\n'
            + '        store=StateStore(Path(' + repr(str(c.root / '.engineering/engineering-runs'))
            + '),central_database=Path(' + repr(str(c.database)) + '),emit_local_projection=False)\n'
            + '        current=store.load("adopt-run")\n'
            + (('        from engineering_platform.storage import sqlite_connection\n        from engineering_platform.execution_lease import Lease,release\n'
                + '        with sqlite_connection(Path(' + repr(str(c.database)) + ')) as db: row=db.execute("SELECT lease_id,run_id,host_identity,host_instance_id,acquired_at,last_heartbeat_at,expires_at,lease_state FROM execution_run_leases WHERE run_id=\'adopt-run\' AND lease_state=\'ACTIVE\'").fetchone()\n'
                + '        release(Path(' + repr(str(c.root)) + '),Lease(*row),central_database=Path(' + repr(str(c.database)) + '))\n') if change=='LEASE_RELEASED' else '        store.save(replace(current,' + change + '))\n' if change else '')
            + '        observed=store.load("adopt-run")\n'
            + '        Path(' + repr(str(marker)) + ').write_text(json.dumps({"owner":observed.owner_authorized,"phase":observed.phase,"repair":observed.repair_iterations}))\n')
        c.git('switch', 'main'); (c.root / 'tests/test_docs.py').write_text(child)
        c.git('add', 'tests/test_docs.py'); c.git('commit', '-qm', 'real final control checkpoint')
        c.base=c.git('rev-parse','HEAD');c.transport.command(c.root,'git','push','origin','main')
        c.git('branch','-f','codex/existing','main');c.git('switch','codex/existing')
        (c.root/'README.md').write_text('# Candidate\n\nApproved documentation.\n')
        c.git('add','README.md');c.git('commit','-qm','selected documentation');c.sha=c.git('rev-parse','HEAD')
        c.selection={**c.selection,'candidate_sha':c.sha,'base_sha':c.base,'validation_profile_digest':profile_digest(c.root,c.sha,0)}
        agent,github=c.lifecycle_adapters();actual=agent.review;reviews=[]
        def external(*args,**kwargs):
            reviews.append(args[1].reviewer);return actual(*args,**kwargs)
        agent.review=external
        runner=EngineeringRunner(c.root,c.store,c.repository,github,agent,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        state=runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        observed=c.store.load('adopt-run');committed=json.loads(marker.read_text())
        context=load_validation_context(c.root,'adopt-run',central_database=c.database)
        self.assertTrue(context['controls']);self.assertTrue(all(x['result']=='PASS' for x in context['controls'].values()),'actual authorized control results must remain')
        if change:
            self.assertEqual(reviews,[], 'last control checkpoint change must prevent every subsequent reviewer')
            self.assertEqual(github.creates,0)
            self.assertEqual(state.next_action,'managed_candidate_adoption_invalid')
            self.assertEqual(observed.owner_authorized,committed['owner'],'post-control write must preserve committed owner withdrawal')
            self.assertEqual(observed.repair_iterations,committed['repair'])
            with sqlite_connection(c.database) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_invocations WHERE run_id='adopt-run'").fetchone()[0],0)
        else:
            self.assertEqual(state.phase,'WAIT_FOR_OPERATOR_MERGE');self.assertEqual(reviews,['quality','security']);self.assertEqual(github.creates,1)
        return state

    def test_last_native_control_committed_withdrawal_blocks_reviews(self):
        self.last_control('owner_authorized=False')

    def test_last_native_control_changed_phase_is_not_rebound(self):
        self.last_control('phase="QUALITY_CONTROL_AGENT"')

    def test_last_native_control_changed_checkpoint_preserves_consumption(self):
        self.last_control('repair_iterations=current.repair_iterations+1')

    def test_last_native_control_lost_lease_blocks_reviews(self):
        self.last_control('LEASE_RELEASED')

    def test_valid_last_native_control_continues(self):
        self.last_control(None)

    def test_fragmented_ack_total_deadline_releases_public_host_lock(self):
        c=adoption.AdoptionLifecycleTests();c.setUp();self.addCleanup(c.doCleanups)
        base,github=c.lifecycle_adapters();accepted=threading.Event();withdrawn=threading.Event();outcomes={};workers=[]
        class Adapter(QualificationReviewBackend):
            def __getattr__(self,name):return getattr(base,name)
            def review(self,*args):return base.review(*args)
            def prepare_review(self,*args):
                request=super().prepare_review(*args);front,proxy=socket.socketpair();front.settimeout(300);upstream=request.connection
                def transport():
                    try:
                        header=receive(proxy,4);upstream.sendall(header+receive(proxy,struct.unpack('!I',header)[0]))
                        ack=receive(upstream,64);accepted.set()
                        for offset in range(0,64,4):
                            proxy.sendall(ack[offset:offset+4]);time.sleep(.5)
                    except OSError:pass
                    finally:upstream.close();proxy.close()
                worker=threading.Thread(target=transport,daemon=True);worker.start();workers.append(worker)
                return cr.SocketReviewRequest(front,request.payload)
        adapter=Adapter();runner=EngineeringRunner(c.root,c.store,c.repository,github,adapter,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        def withdraw():
            if accepted.wait(15):
                started=time.monotonic()
                try:c.unbind();outcomes['committed']=True
                except Exception as error:outcomes['error']=str(error)
                outcomes['elapsed']=time.monotonic()-started;withdrawn.set()
        worker=threading.Thread(target=withdraw,daemon=True);worker.start();workers.append(worker)
        started=time.monotonic();state=runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        self.assertTrue(withdrawn.wait(2),outcomes)
        self.assertTrue(outcomes.get('committed'), 'total ACK deadline must release authority lock for canonical withdrawal')
        self.assertLess(outcomes['elapsed'],6,'handoff must use one <=5s total deadline')
        self.assertEqual(state.next_action,'assurance_start_uncertain')
        self.assertEqual([a['reviewer'] for a in adapter.review_acceptances],['quality'])
        self.assertEqual([e['status'] for e in state.assurance_launch_events],['INTENT','UNKNOWN'])
        self.assertEqual(github.creates,0);self.assertEqual(base.prompts,[])
        for worker in workers:worker.join(timeout=2)
        c.stop_host(runner)
        again=EngineeringRunner(c.root,c.store,c.repository,github,adapter,lambda _:None);self.addCleanup(c.stop_host,again)
        after=again.run(c.prompt,run_id='adopt-run',resume=True)
        self.assertEqual(len(adapter.review_acceptances),1,'UNKNOWN must never blindly replay')
        self.assertEqual(after.assurance_launch_events,state.assurance_launch_events)

    def test_fragmented_ack_real_heartbeat_and_withdrawal_remain_available(self):
        from engineering_platform.execution_lease import acquire, LeaseHeartbeat
        from engineering_platform.managed_adoption import effect_authority
        c=adoption.AdoptionLifecycleTests();c.setUp();self.addCleanup(c.doCleanups)
        base,github=c.lifecycle_adapters();runner=EngineeringRunner(c.root,c.store,c.repository,github,base,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        state=runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        runner.active_lease=acquire(c.root,state.run_id,identity=runner.host_identity,instance_id=runner.host_instance_id,process_id=os.getpid(),central_database=c.database)
        original_expiry=runner.active_lease.expires_at
        runner.lease_heartbeat=LeaseHeartbeat(c.root,runner.active_lease,central_database=c.database);runner.lease_heartbeat.start()
        accepted=threading.Event();outcomes={};workers=[]
        class Adapter(QualificationReviewBackend):
            def review(self,*args):return base.review(*args)
            def prepare_review(self,*args):
                request=super().prepare_review(*args);front,proxy=socket.socketpair();front.settimeout(300);upstream=request.connection
                def transport():
                    try:
                        header=receive(proxy,4);upstream.sendall(header+receive(proxy,struct.unpack('!I',header)[0]))
                        ack=receive(upstream,64);accepted.set()
                        for offset in range(0,64,4):proxy.sendall(ack[offset:offset+4]);time.sleep(.5)
                    except OSError:pass
                    finally:upstream.close();proxy.close()
                worker=threading.Thread(target=transport,daemon=True);worker.start();workers.append(worker)
                return cr.SocketReviewRequest(front,request.payload)
        def withdraw():
            if accepted.wait(20):
                started=time.monotonic()
                try:c.unbind();outcomes['committed']=True
                except Exception as error:outcomes['error']=str(error)
                outcomes['elapsed']=time.monotonic()-started
        worker=threading.Thread(target=withdraw,daemon=True);worker.start();workers.append(worker)
        # Cross the actual unchanged 15s heartbeat tick while handoff is locked.
        time.sleep(12)
        adapter=Adapter();started=time.monotonic()
        with self.assertRaises(cr.ReviewStartUncertain):
            cr.run_reviews(c.root,(cr.ReviewerSelection('quality','bounded',1,transport_invocation_id='actual-ack-heartbeat'),),'bounded',adapter,
                authority=lambda:effect_authority(state=state,root=c.root,central_database=c.database,lease=runner.active_lease))
        self.assertLess(time.monotonic()-started,6)
        for worker in workers:worker.join(timeout=2)
        self.assertTrue(outcomes.get('committed'),outcomes)
        self.assertLess(outcomes['elapsed'],6)
        observation_deadline=time.monotonic()+5
        while True:
            with sqlite_connection(c.database) as db:
                actual_expiry=db.execute('SELECT expires_at FROM execution_run_leases WHERE lease_id=?',(runner.active_lease.lease_id,)).fetchone()[0]
            if actual_expiry>original_expiry or time.monotonic()>=observation_deadline or runner.lease_heartbeat.error:
                break
            time.sleep(.1)
        self.assertGreater(actual_expiry,original_expiry,'actual unchanged heartbeat must renew across bounded handoff; '+str(runner.lease_heartbeat.error))
        self.assertIsNone(runner.lease_heartbeat.error)
        self.assertEqual(len(adapter.review_acceptances),1)

    def native_public_host(self, *, deny_audit=False):
        c=specialist.SpecialistPipelineTests();c.setUp();self.addCleanup(c.doCleanups)
        calls=[];starts=[];active=[True]
        def audit(event,args):
            if active[0] and event=='subprocess.Popen' and args[1] and 'codex' in str(args[1][0]):
                starts.append('exec' if 'exec' in args[1] else 'version' if '--version' in args[1] else 'mcp' if 'mcp' in args[1] else 'other')
        sys.addaudithook(audit);self.addCleanup(lambda:active.__setitem__(0,False))
        class Model(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                state=c.store.load('specialist-run')
                pending=[r for r in state.specialist_records if r['kind']=='DISPATCH' and not any(x['kind'] in {'RESULT','UNCERTAIN'} and x['invocation_id']==r['invocation_id'] for x in state.specialist_records)]
                if pending:
                    record=pending[-1];role=record['reviewer'];binding={k:v for k,v in record.items() if k not in {'kind','payload'}}
                    path='README.md' if role=='documentation' else 'tests/test_docs.py'
                    selection=next(x for x in c.plan().selections if x.reviewer==role)
                    output={'contract_version':'2.0','contribution':'Bounded real native advice','recommendations':[],
                        'findings':[{'id':'missing-detail','summary':'Add the missing acceptance sentence.','path':path,'evidence_ref':'git-blob:'+dict(selection.specialist_source_blobs)[path],'proposed_disposition':'ACCEPTED'}], 'specialist_binding':binding}
                else:
                    role=state.assurance_launch_events[-1]['reviewer']
                    output={'contract_version':'3.0','contribution':'Reviewed current fixture candidate','recommendations':[],'findings':[],
                        'coverage':{s:{'status':'REVIEWED','evidence_ref':'git:'+state.implementation_head_sha} for s in cr.MANDATORY_COVERAGE_SURFACES['IMPLEMENTATION'][role]},'finding_dispositions':{}}
                calls.append(role)
                item={'id':'message','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':json.dumps(output)}]}
                events=[{'type':'response.created','response':{'id':'fixture','status':'in_progress','output':[]}}, {'type':'response.output_item.added','output_index':0,'item':item},{'type':'response.output_item.done','output_index':0,'item':item},{'type':'response.completed','response':{'id':'fixture','status':'completed','output':[item],'usage':{'input_tokens':20,'output_tokens':20,'total_tokens':40}}}]
                data=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Model);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(thread.join);self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix='ep-r10-native-') as raw:
            area=Path(raw);prefix=area/'provider';(prefix/'bin').mkdir(parents=True);(prefix/'bin/codex').symlink_to(shutil.which('codex'));home=area/'cli-home';home.mkdir()
            (home/'config.toml').write_text('model = "fixture"\nmodel_provider = "fixture"\n[model_providers.fixture]\nname = "fixture"\nbase_url = "http://127.0.0.1:'+str(server.server_port)+'/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n')
            real_policy=effect_provider.policy
            def local_policy(client,root):
                return real_policy(client,root)+('-c','model="fixture"','-c','model_provider="fixture"','-c',f'model_providers.fixture={{name="fixture",base_url="http://127.0.0.1:{server.server_port}/v1",wire_api="responses",requires_openai_auth=false}}')
            with patch.dict(os.environ,{'EP_MANAGED_CODEX_CLI_PREFIX':str(prefix),'CODEX_HOME':str(home)}),patch.object(effect_provider,'policy',side_effect=local_policy):
                client=CodexCliClient(CodexCliProvider());primary=c.transport();client.invoke=primary.invoke
                if deny_audit:
                    with sqlite_connection(c.fixture.database) as db:
                        db.execute("CREATE TRIGGER deny_specialist_start BEFORE INSERT ON provider_invocations WHEN NEW.phase='CAPABILITY_REVIEW_DISPATCH' BEGIN SELECT RAISE(ABORT,'actual audit denied'); END")
                runner=c.runner(client);state=runner.run(c.fixture.prompt,run_id='specialist-run',owner_authorized=True)
                if deny_audit:
                    observed=c.store.load('specialist-run')
                    self.assertEqual(state.next_action,'specialist_start_unavailable')
                    self.assertEqual(primary.implementations,0)
                    self.assertLessEqual(len(calls),1);self.assertTrue(all(role=='validation' for role in calls))
                    self.assertEqual(starts.count('exec'),1)
                    self.assertEqual([r['kind'] for r in observed.specialist_records if r['kind']!='SELECTION'],['DISPATCH','UNCERTAIN'],'actual typed uncertainty must survive snapshot and persist its reservation')
                    with sqlite_connection(c.fixture.database) as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_invocations WHERE run_id='specialist-run'").fetchone()[0],0)
                    return
                self.assertEqual(state.phase,'WAIT_FOR_OPERATOR_MERGE','native public host must execute both actual specialist model requests; '+str(state.diagnostic))
                self.assertEqual(calls,['validation','documentation','quality','security'],'native public host must execute both actual specialist model requests')
                self.assertEqual(starts.count('exec'),4);self.assertGreaterEqual(starts.count('version'),3);self.assertGreaterEqual(starts.count('mcp'),3)
                data=cr.specialist_readback(c.store.load('specialist-run').specialist_records)
                self.assertEqual((data['reserved_invocation_count'],data['completed_invocation_count'],primary.implementations),(2,2,1))
                self.assertEqual({f['disposition'] for f in data['findings']},{'VERIFIED','DEFERRED'})
                with sqlite_connection(c.fixture.database) as db:
                    rows=[tuple(r) for r in db.execute("SELECT phase,role FROM provider_invocations WHERE run_id='specialist-run' ORDER BY ordinal")]
                self.assertEqual(sum(p=='CAPABILITY_REVIEW_DISPATCH' for p,r in rows),2,'metadata must not create model dispatch rows')
                self.assertEqual([r['status'] for r in state.assurance_reviews],['PASS','PASS'])

    def test_native_public_host_metadata_not_modelstarts_full_delivery(self):
        self.native_public_host()

    def test_native_public_host_actual_audit_failure_stays_typed(self):
        self.native_public_host(deny_audit=True)

    def test_real_process_crash_before_ack_preserves_intent_after_natural_lease(self):
        import subprocess
        outcome=subprocess.run((sys.executable,'-B',str(Path(__file__).with_name('round10_process_worker.py'))),text=True,capture_output=True,timeout=140)
        self.assertEqual(outcome.returncode,0,outcome.stdout+outcome.stderr)
        self.assertIn('CRASHED_BEFORE_ACK',outcome.stdout)

    def test_real_partial_send_deadline_is_uncertain_and_bounded(self):
        front,back=socket.socketpair();front.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024);front.settimeout(300)
        request=cr.SocketReviewRequest(front,b'x'*262144)
        observed=[]
        def receive_partial():
            time.sleep(5.5)
            while True:
                part=back.recv(65536)
                if not part:break
                observed.append(part)
            back.close()
        worker=threading.Thread(target=receive_partial,daemon=True);worker.start()
        started=time.monotonic()
        try:
            with self.assertRaises(cr.ReviewStartUncertain):request.start()
            self.assertLess(time.monotonic()-started,6,'partial send must use total handoff deadline')
        finally:request.close();worker.join(timeout=2)
        self.assertGreater(sum(map(len,observed)),0)
        self.assertLess(sum(map(len,observed)),len(request.payload)+4,'actual partial send cannot be called accepted')
