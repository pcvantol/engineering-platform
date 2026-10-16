#!/usr/bin/env python3
"""Owning installed HTTP recovery qualification, never a consumer API.

Only exclusive new synthetic roots are accepted. Fault cases stop an actual
Server after a real persisted boundary, then kill that exact child and restart
the ordinary installed Server. No SQL states or effect receipts are fabricated.
Credentials remain in memory/private owner configs, never arguments or reports.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import subprocess
import time
import traceback
import zipfile


FAULT_LAUNCHER = '''import json,os,signal,sys
from pathlib import Path
from engineering_platform import genesis_service as service,server
point,marker,root,instance=sys.argv[1:]
def barrier():
 Path(marker).write_text(json.dumps({"pid":os.getpid(),"point":point}))
 os.kill(os.getpid(),signal.SIGSTOP)
if point=="ACCEPT":
 original=service.admit
 def wrapped(*a,**k):
  result=original(*a,**k)
  barrier()
  return result
 service.admit=wrapped
elif point=="REGISTER":
 original=service.execute
 def wrapped(*a,**k):
  result=original(*a,**k)
  barrier()
  return result
 service.execute=wrapped
else:
 original=service._execute_step
 def wrapped(*a,**k):
  result=original(*a,**k)
  if k["step"]==point:barrier()
  return result
 service._execute_step=wrapped
raise SystemExit(server.main(["serve","--data-root",root,"--expected-instance-id",instance]))
'''


def write_private(path, value):
    with path.open('x', encoding='utf-8') as f:
        os.chmod(path, 0o600)
        f.write(value if isinstance(value, str) else json.dumps(value, indent=2)+'\n')


class Case:
    def __init__(self, args, name, index, schema):
        self.args, self.name, self.schema = args, name, schema
        self.root = Path('/private/tmp') / (args.root_prefix+str(index))
        self.root.mkdir(mode=0o700)  # Never adopt an existing scratch resource.
        self.data, self.workspace = self.root/'s', self.root/'w'
        self.workspace.mkdir(mode=0o700)
        self.target = self.workspace/'r'
        self.record = {'name': name, 'root': str(self.root), 'http': [], 'processes': [], 'boundaries': []}
        self.logs, self.children = [], []
        self.evidence = args.output/name
        self.evidence.mkdir(mode=0o700)
        write_private(self.root/'.qualification-owner.json', {
            'assignment': 'L2-GENESIS-LOCAL-BOOTSTRAP-V1-20261016', 'source': args.source,
            'uid': os.getuid(), 'identity': [self.root.stat().st_dev, self.root.stat().st_ino]})
        self.env = {k: v for k, v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}}
        self.env.update(PATH=str(args.python.parent)+':'+str(args.native/'bin')+':/usr/bin:/bin',
                        EP_MANAGED_CODEX_CLI_PREFIX=str(args.native), PYTHONDONTWRITEBYTECODE='1')
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0)); self.port = s.getsockname()[1]
        self.instance = self.cli('server', 'init', '--bind-host', '127.0.0.1', '--bind-port', self.port)['instance_id']
        self.cli('genesis_cli', 'init')
        self.request = {'contract_version': '1.0', 'operation_id': 'qualification-'+name,
            'target_ref': 'owner-target', 'effect_plan_binding': {
                'plan_id': 'synthetic-r50-plan', 'plan_revision': 1, 'plan_digest': '1'*64,
                'decision_id': 'independent-EP-owner-qualification', 'decision_digest': '2'*64,
                'proposal_id': 'original-r50', 'proposal_digest': hashlib.sha256(args.bundle.read_bytes()).hexdigest(),
                'source_instance_id': 'synthetic-forge', 'source_actor_id': 'synthetic-operator',
                'context_id': 'synthetic-genesis'}, 'bundle': json.loads(args.bundle.read_bytes())}
        self.validator = self.root/'v.py'
        write_private(self.validator, args.validator.read_text())
        self.issue(30 if name == 'expiry' else 900)
        self.headers = {'EP-Bootstrap-Contract': '1.0', 'EP-Instance-ID': self.instance,
            'EP-Host-ID': hashlib.sha256((os.uname().nodename+':'+str(os.geteuid())).encode()).hexdigest(),
            'EP-Consumer-ID': 'forge', 'EP-Principal': 'synthetic-service', 'EP-Audience': 'genesis-bootstrap'}
        self.path = '/v1/bootstrap/genesis/operations'
        self.item = self.path+'/'+self.request['operation_id']

    def cli(self, module, *args):
        command = [str(self.args.python), '-I', '-m', 'engineering_platform.'+module,
                   *map(str, args), '--data-root', str(self.data)]
        if hasattr(self, 'instance'):
            command += ['--expected-instance-id', self.instance]
        r = subprocess.run(command, cwd=self.root, env=self.env, capture_output=True, text=True, timeout=40)
        body = json.loads(r.stdout)
        self.record.setdefault('owner_commands', []).append({'module': module, 'args': list(map(str, args)),
            'exit': r.returncode, 'result': {k: v for k, v in body.items() if k != 'credential'}, 'stderr': r.stderr})
        assert r.returncode == 0, (module, body, r.stderr)
        return body

    def issue(self, ttl):
        cfg = self.root/'grant.json'
        write_private(cfg, {'grant_id': 'owner-bootstrap', 'issuance_id': 'owner-issue',
            'consumer_id': 'forge', 'principal': 'synthetic-service', 'target_name': 'r',
            'workspace_root': str(self.workspace), 'validator_path': str(self.validator),
            'request': self.request, 'ttl_seconds': ttl})
        self.grant = self.cli('genesis_cli', 'issue', '--config', cfg)

    def start(self, point=None):
        marker = self.root/('barrier-'+str(len(self.children))+'.json')
        if point:
            launcher = self.root/'fault-launcher.py'
            if not launcher.exists(): write_private(launcher, FAULT_LAUNCHER)
            cmd = [str(self.args.python), '-I', str(launcher), point, str(marker), str(self.data), self.instance]
        else:
            cmd = [str(self.args.python), '-I', '-m', 'engineering_platform.server', 'serve',
                   '--data-root', str(self.data), '--expected-instance-id', self.instance]
        log = (self.evidence/('server-'+str(len(self.children))+'.log')).open('xb')
        self.logs.append(log)
        p = subprocess.Popen(cmd, cwd=self.root, env=self.env, stdout=log, stderr=log, start_new_session=True)
        self.children.append(p)
        self.record['processes'].append({'pid': p.pid, 'fault_boundary': point, 'started_ns': time.time_ns()})
        until = time.monotonic()+20
        while time.monotonic() < until:
            assert p.poll() is None, ('Server failed to start', p.returncode)
            with socket.socket() as s:
                if s.connect_ex(('127.0.0.1', self.port)) == 0: return p, marker
            time.sleep(.05)
        raise TimeoutError('Own Server did not listen')

    def exchange(self, method, path, expected=None, body=None):
        raw = None if body is None else (json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':'))+'\n').encode()
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=180)
        try:
            c.request(method, path, body=raw, headers={**self.headers,
                'Authorization': 'Bearer '+self.grant['credential'], 'Content-Type': 'application/json'})
            r = c.getresponse(); response = r.read(); value = json.loads(response)
            self.record['http'].append({'method': method, 'path': path, 'status': r.status, 'body': value,
                'request_sha256': None if raw is None else hashlib.sha256(raw).hexdigest(),
                'response_sha256': hashlib.sha256(response).hexdigest(), 'headers': dict(r.getheaders())})
            self.schema.validate(value)
            if expected is not None: assert r.status == expected, (r.status, value)
            return value
        except (OSError, http.client.HTTPException) as e:
            self.record['http'].append({'method': method, 'path': path, 'transport_failure': type(e).__name__})
            if expected is not None: raise
        finally: c.close()

    def snapshot(self):
        files = {} if not self.target.exists() else {str(p.relative_to(self.target)): {
            'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'mode': stat.S_IMODE(p.stat().st_mode),
            'inode': p.stat().st_ino} for p in self.target.rglob('*') if p.is_file()}
        with sqlite3.connect((self.data/'epdata.sqlite').as_uri()+'?mode=ro', uri=True) as db:
            rows = {t: db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in (
                'ep_genesis_operations', 'ep_project_registrations', 'ep_repository_registrations',
                'ep_local_repository_bindings', 'ep_submissions', 'ep_execution_runs', 'engineering_transactions')}
            operations = [{k: value[k] for k in ('attempts', 'completed_steps', 'known')}
                for raw, in db.execute('SELECT evidence FROM ep_genesis_operations')
                for value in (json.loads(raw),)]
        return {'files': files, 'rows': rows, 'effect_ledger': operations}

    def barrier(self, p, marker):
        until = time.monotonic()+160
        while time.monotonic() < until:
            assert p.poll() is None, ('Server stopped before selected boundary', p.returncode)
            if marker.exists():
                value = json.loads(marker.read_bytes())
                assert value['pid'] == p.pid
                state = subprocess.check_output(['ps', '-o', 'stat=', '-p', str(p.pid)], text=True).strip()
                if 'T' in state:
                    self.record['boundaries'].append({**value, 'actual_process_state': state, 'at_ns': time.time_ns()})
                    return
            time.sleep(.05)
        raise TimeoutError('Real boundary not reached')

    def stop(self, p, crash=False):
        if p.poll() is None:
            # Stop the exact unreaped child before capturing descendants. No
            # PID-only helper kill and no new children racing the inventory.
            p.send_signal(signal.SIGSTOP)
            from engineering_platform.provider_process_identity import capture_process_identity, verify_process_identity
            parent = capture_process_identity(p.pid, p.pid)
            assert parent is not None, 'Unidentifiable own Server: stop unsafe'
            listing = subprocess.check_output(['ps', '-axo', 'pid=,ppid='], text=True)
            edges = [tuple(map(int, line.split())) for line in listing.splitlines() if line.strip()]
            owned = {p.pid}
            while True:
                found = {pid for pid, ppid in edges if ppid in owned}
                if found <= owned: break
                owned.update(found)
            descendants = [capture_process_identity(pid) for pid in owned-{p.pid}]
            assert all(descendants), 'Unidentifiable own descendant: retain checkpoint'
            for child in descendants:
                if verify_process_identity(child) == 'MATCH': os.kill(child.pid, signal.SIGKILL)
                else: assert verify_process_identity(child) == 'NOT_ACTIVE', 'Descendant identity changed'
            assert verify_process_identity(parent) == 'MATCH'
            p.send_signal(signal.SIGKILL if crash or descendants else signal.SIGTERM)
            if not crash and not descendants: p.send_signal(signal.SIGCONT)
            try: p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                assert verify_process_identity(parent) == 'MATCH'
                p.send_signal(signal.SIGKILL); p.wait(timeout=10)
            for child in descendants:
                until = time.monotonic()+5
                while verify_process_identity(child) == 'MATCH' and time.monotonic() < until: time.sleep(.05)
                assert verify_process_identity(child) == 'NOT_ACTIVE', 'Own descendant remains active'
            self.record['processes'][self.children.index(p)]['descendants_stopped'] = [c.pid for c in descendants]
        self.record['processes'][self.children.index(p)].update(returncode=p.returncode, reaped=True)

    def run(self):
        point = {'lost-accept': 'ACCEPT', 'after-create': 'CREATE_TARGET', 'after-commit': 'GIT_REF',
                 'before-register': 'VERIFY_COMMIT', 'lost-success': 'REGISTER', 'revoke': 'CREATE_TARGET',
                 'expiry': 'CREATE_TARGET', 'source-loss': 'CREATE_TARGET', 'validator-pin': 'WRITE_FILES'}[self.name]
        p, marker = self.start(point)
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(self.exchange, 'POST', self.path, None, self.request)
            self.barrier(p, marker)
            before = self.snapshot()
            if self.name in {'revoke', 'expiry'}:
                if self.name == 'revoke':
                    self.cli('genesis_cli', 'revoke', '--grant-id', self.grant['grant_id'], '--operation-id', 'owner-revoke')
                else:
                    # Natural original lifetime, never a fake clock or extended grant.
                    time.sleep(31)
                p.send_signal(signal.SIGCONT)
                request.result(timeout=45)
                assert self.record['http'][-1]['status'] == 401
                assert before == self.snapshot(), 'Effect after authority withdrawal'
                self.exchange('POST', self.item+'/resume', 401)
                assert before == self.snapshot()
            elif self.name == 'validator-pin':
                original = self.validator.read_bytes()
                write_private(self.evidence/'original-validator.py', original.decode())
                self.validator.write_bytes(b'raise SystemExit(0)\n')
                p.send_signal(signal.SIGCONT); request.result(timeout=45)
                assert self.record['http'][-1]['status'] == 409
                assert before == self.snapshot(), 'Untrusted validator reached an effect'
                self.validator.write_bytes(original)
                complete = self.exchange('POST', self.item+'/resume', 200)
                assert complete['state'] == 'COMPLETE'
            elif self.name == 'source-loss':
                database = self.data/'epdata.sqlite'; held = self.data/'preserved-original.sqlite'
                database.rename(held)
                p.send_signal(signal.SIGCONT); request.result(timeout=45)
                assert self.record['http'][-1]['status'] == 503
                assert not database.exists(), 'Missing initialized source was recreated'
                self.stop(p)
                held.rename(database)
                self.start()
                assert before == self.snapshot()
                self.exchange('GET', self.item, 200)
                assert before == self.snapshot(), 'GET caused recovery effects'
                complete = self.exchange('POST', self.item+'/resume', 200)
                assert complete['state'] == 'COMPLETE'
            else:
                self.stop(p, crash=True)
                request.result(timeout=10)
                self.start()
                original = self.exchange('GET', self.item, 200)
                assert before == self.snapshot(), 'Readback resumed physical work'
                complete = self.exchange('POST', self.item+'/resume', 200)
                assert complete['state'] == 'COMPLETE'
                assert complete['operation_id'] == original['operation_id']
                if original['commit']: assert original['commit'] == complete['commit']
                final = self.snapshot()
                replay = self.exchange('POST', self.path, 200, self.request)
                assert replay['commit'] == complete['commit'] and self.snapshot() == final
        final = self.snapshot()
        for table in ('ep_submissions', 'ep_execution_runs', 'engineering_transactions'):
            assert final['rows'][table] == 0, ('Unexpected business effect', table)
        assert final['rows']['ep_genesis_operations'] == 1
        self.record['final_snapshot'] = final

    def finish(self):
        errors = []
        for p in self.children:
            try: self.stop(p)
            except BaseException: errors.append(traceback.format_exc())
        for log in self.logs:
            try: log.close()
            except BaseException: errors.append(traceback.format_exc())
        try:
            with socket.socket() as s:
                assert s.connect_ex(('127.0.0.1', self.port)) != 0, 'Own listener survived cleanup'
        except BaseException: errors.append(traceback.format_exc())
        self.record['cleanup_errors'] = errors
        if errors: self.record['state'] = 'FAILED'
        write_private(self.evidence/'receipt.json', self.record)
        if errors: raise RuntimeError('Own cleanup incomplete; original failure and cleanup details retained')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('python', 'native', 'bundle', 'validator', 'wheel', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--source', required=True)
    p.add_argument('--root-prefix', required=True)
    p.add_argument('--cases', nargs='+', default=['lost-accept', 'after-create', 'after-commit', 'before-register', 'lost-success', 'revoke', 'expiry', 'source-loss', 'validator-pin'])
    args = p.parse_args()
    assert args.root_prefix.isalnum() and len(args.root_prefix) <= 4
    assert len(args.source) == 40 and all(c in '0123456789abcdef' for c in args.source)
    args.output.mkdir(mode=0o700)
    from jsonschema import Draft202012Validator, FormatChecker
    with zipfile.ZipFile(args.wheel) as z:
        schema = Draft202012Validator(json.loads(z.read('engineering_platform/schemas/genesis-local-bootstrap-v1.schema.json')), format_checker=FormatChecker())
    results = []
    for index, name in enumerate(args.cases):
        case = None
        primary = cleanup = None
        try:
            case = Case(args, name, index, schema)
            case.run(); case.record['state'] = 'PASS'
        except BaseException as error:
            primary = error
            if case: case.record.update(state='FAILED', failure=traceback.format_exc())
        finally:
            if case:
                try: case.finish()
                except BaseException as error: cleanup = error
                results.append({'case': name, 'state': case.record.get('state')})
                print(json.dumps(results[-1]), flush=True)
        if primary and cleanup: raise BaseExceptionGroup('Primary qualification and cleanup failures', [primary, cleanup])
        if primary: raise primary
        if cleanup: raise cleanup
    write_private(args.output/'summary.json', {'source': args.source,
        'wheel_sha256': hashlib.sha256(args.wheel.read_bytes()).hexdigest(), 'results': results,
        'meaning': 'Actual unchanged noneditable installed package; explicitly instrumented fault boundaries; own synthetic targets only'})


if __name__ == '__main__': main()
