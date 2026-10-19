"""Portable controller regressions, not native/macOS qualification.
Actual controller pipe/identity/log behavior with a protocol-only subprocess.
OS selection is modeled; real installed native proof remains separate.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tests.engineering import test_genesis_bootstrap as fixture
from engineering_platform import genesis_authority as authority,genesis_contract as contract,genesis_service as service

class PortableControllerPinRegressions(unittest.TestCase):
    setUp=fixture.GenesisOwnerFixture.setUp
    def prepared(self):
        service.admit(self.data,token=self.grant['credential'],headers=self.headers,value=self.original_request)
        with authority.store(self.data) as db:
            _,scope=authority.authenticate(db,self.grant['credential'],self.headers)
        return {'format':1,'mode':service.genesis_git_runtime.POLICY,'operation_id':scope['operation_id'],'host_id':scope['host_id'],'selection':{'fixture':'MODELED_OS_SELECTION'},'executable':{'path':'/MODELED/NEVER_EXECUTED/git'},'ancestors':[]}
    def evidence(self):
        with authority.store(self.data) as db:return service._row(db,'synthetic-operation',self.grant['credential'])['evidence']
    def run_readiness(self,pin,receipt=None,verify=None):
        marker=self.root/'protocol-worker-ready'
        result={'business_effects':0,'git_runtime':pin,'git_version_sha256':'0'*64} if receipt is None else receipt
        code=(f'import sys,json,hashlib;from pathlib import Path;Path({str(marker)!r}).write_text("ready"); '
              'j=json.loads(sys.stdin.buffer.readline()); '
              'raw=(json.dumps(j,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\\n").encode(); '
              'print(json.dumps({"state":"STEP_READY","job_digest":hashlib.sha256(raw).hexdigest()}),flush=True); '
              'assert sys.stdin.buffer.readline()==b"EXECUTE\\n"; '
              f'print(json.dumps({{"state":"STEP_COMPLETED","step":j["step"],"result":{result!r}}}),flush=True)')
        original_popen=subprocess.Popen
        self.starts=[]
        def initialized(*args,**kwargs):
            self.starts.append(1)
            self.assertEqual(self.evidence()["attempts"][-1]["git_runtime"],pin)
            self.assertNotIn("git_runtime",self.evidence()["known"])
            p=original_popen(*args,**kwargs)
            until=time.monotonic()+5
            while not marker.exists():
                if p.poll() is not None or time.monotonic()>=until:
                    p.kill();p.wait(timeout=5);self.fail('Own protocol child did not reach exec marker')
                time.sleep(.01)
            return p
        modeled_sys=SimpleNamespace(**{k:getattr(sys,k) for k in dir(sys) if k!='platform'},platform='darwin')
        with service._lock(self.data,'synthetic-operation') as fd, \
             patch.object(service,'sys',modeled_sys), \
             patch.object(service.genesis_native,'runtime',return_value=Path(sys.executable)), \
             patch.object(service.genesis_native,'command',return_value=(sys.executable,'-I','-c',code)), \
             patch.object(service.genesis_git_runtime,'capture',return_value=pin) as capture, \
             patch.object(service.genesis_git_runtime,'verify',side_effect=verify,return_value=pin['executable']['path']) as checked, \
             patch.object(service.subprocess,'Popen',side_effect=initialized):
            result=service._execute_step(self.data,operation_id='synthetic-operation',step='PREFLIGHT',token=self.grant['credential'],headers=self.headers,source_identity=None,lock_fd=fd)
        return result,capture.call_count,checked.call_args_list
    def test_modeled_macos_readiness_persists_same_original_pin_after_real_handshake(self):
        pin=self.prepared(); result,captured,checks=self.run_readiness(pin)
        self.assertEqual(captured,1);self.assertEqual(len(checks),2)
        self.assertTrue(all(c.kwargs['selection'] for c in checks))
        e=self.evidence();self.assertEqual(e['known']['git_runtime'],pin)
        self.assertEqual(e['attempts'][0]['git_runtime'],pin)
        self.assertEqual(e['completed_steps'],['PREFLIGHT'])
        self.assertEqual(e['attempts'][0]['state'],'STEP_COMPLETED')
        self.assertFalse((self.workspace/'new-repository').exists())
    def test_foreign_worker_pin_is_red_and_never_becomes_known(self):
        pin=self.prepared();foreign=deepcopy(pin);foreign['host_id']='foreign'
        receipt={'business_effects':0,'git_runtime':foreign,'git_version_sha256':'0'*64}
        with self.assertRaisesRegex(contract.BootstrapError,'RECEIPT_INVALID'):self.run_readiness(pin,receipt)
        e=self.evidence();self.assertNotIn('git_runtime',e['known']);self.assertEqual(e['completed_steps'],[])
        self.assertEqual(e['attempts'][0]['git_runtime'],pin)
        self.assertFalse((self.workspace/'new-repository').exists())
    def test_selection_drift_after_worker_exit_cannot_complete_readiness(self):
        pin=self.prepared()
        with self.assertRaisesRegex(contract.BootstrapError,'GIT_RUNTIME_CHANGED'):
            self.run_readiness(pin,verify=[pin['executable']['path'],contract.BootstrapError('BOOTSTRAP_GIT_RUNTIME_CHANGED',409)])
        e=self.evidence();self.assertNotIn('git_runtime',e['known']);self.assertEqual(e['completed_steps'],[])
        self.assertEqual(e['attempts'][0]['git_runtime'],pin)
        self.assertFalse((self.workspace/'new-repository').exists())
    def test_failed_attempt_pin_is_reused_without_capture(self):
        pin=self.prepared()
        with authority.store(self.data,write=True) as db:
            row=service._row(db,'synthetic-operation',self.grant['credential'])
            row['evidence']['attempts']=[{'attempt_id':'a'*32,'step':'PREFLIGHT','state':'INTENT_RECORDED','git_runtime':pin}]
            service._save(db,row,self.grant['credential'])
        result,captured,checks=self.run_readiness(pin)
        self.assertEqual(captured,0);self.assertEqual(len(checks),2)
        e=self.evidence();self.assertEqual(e['attempts'][0]['git_runtime'],pin)
        self.assertEqual(e['known']['git_runtime'],pin);self.assertEqual(e['completed_steps'],['PREFLIGHT'])

    def modeled_platform(self,name):
        return SimpleNamespace(**{k:getattr(sys,k) for k in dir(sys) if k!='platform'},platform=name)
    def test_legacy_readiness_is_selected_and_history_is_not_rewritten(self):
        pin=self.prepared()
        historical={'attempt_id':'a'*32,'step':'PREFLIGHT','state':'STEP_COMPLETED','exit_code':0}
        with authority.store(self.data,write=True) as db:
            row=service._row(db,'synthetic-operation',self.grant['credential'])
            row['evidence'].pop('git_runtime_policy',None)
            row['evidence'].update(completed_steps=['PREFLIGHT'],attempts=[historical],known={'native_profile_started':True,'business_effects':0})
            service._save(db,row,self.grant['credential'])
        with patch.object(service,'sys',self.modeled_platform('darwin')),patch.object(service,'_execute_step',side_effect=contract.BootstrapError('OWN_STOP',409)) as step:
            with self.assertRaisesRegex(contract.BootstrapError,'OWN_STOP'):
                service.execute(self.data,operation_id='synthetic-operation',token=self.grant['credential'],headers=self.headers)
            self.assertEqual(step.call_args.kwargs['step'],'PREFLIGHT')
        self.assertEqual(self.evidence()['attempts'],[historical])
        self.run_readiness(pin)
        e=self.evidence();self.assertEqual(e['completed_steps'],['PREFLIGHT'])
        self.assertEqual(e['attempts'][0],historical);self.assertEqual(len(e['attempts']),2)
        self.assertEqual(e['git_runtime_policy'],service.genesis_git_runtime.POLICY)
    def test_broken_current_pin_cannot_be_treated_as_legacy(self):
        pin=self.prepared()
        with authority.store(self.data,write=True) as db:
            row=service._row(db,'synthetic-operation',self.grant['credential'])
            row['evidence'].update(git_runtime_policy=service.genesis_git_runtime.POLICY,completed_steps=['PREFLIGHT'],known={'native_profile_started':True,'business_effects':0})
            service._save(db,row,self.grant['credential'])
        with self.assertRaisesRegex(contract.BootstrapError,'PIN_INVALID'):self.run_readiness(pin)
        self.assertEqual(self.starts,[]);self.assertNotIn('git_runtime',self.evidence()['known'])
        self.assertFalse((self.workspace/'new-repository').exists())
    def test_macos_pin_on_non_darwin_refuses_before_runtime_or_process_start(self):
        pin=self.prepared()
        with authority.store(self.data,write=True) as db:
            row=service._row(db,'synthetic-operation',self.grant['credential'])
            row['evidence']['known']['git_runtime']=pin
            service._save(db,row,self.grant['credential'])
        before=self.evidence()
        with patch.object(service,'sys',self.modeled_platform('linux')),patch.object(service.genesis_native,'runtime') as native,patch.object(service.subprocess,'Popen') as start:
            with self.assertRaisesRegex(contract.BootstrapError,'MODE_UNSUPPORTED'):
                service.execute(self.data,operation_id='synthetic-operation',token=self.grant['credential'],headers=self.headers)
            native.assert_not_called();start.assert_not_called()
        self.assertEqual(before,self.evidence());self.assertFalse((self.workspace/'new-repository').exists())
    def test_withdrawal_after_selection_prevents_pin_publication_and_start(self):
        pin=self.prepared()
        def withdraw(*args,**kwargs):
            with authority.store(self.data,write=True) as db:
                authority.revoke(db,grant_id='synthetic-grant',operation_id='owner-withdraw')
            return pin['executable']['path']
        with self.assertRaises(contract.BootstrapError):self.run_readiness(pin,verify=withdraw)
        self.assertEqual(self.starts,[])
        self.assertFalse((self.workspace/'new-repository').exists())

if __name__=='__main__':unittest.main()
