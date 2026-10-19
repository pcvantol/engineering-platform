#!/usr/bin/env python3
"""Read-only native comparison of Apple's Git shim and its selected real Git.

No Git repository, grant, submission, commit or B8R record is created. Neither
the sandbox policy nor the owning process-group detector is changed. Original
private process images/fd types and output are encrypted before publication.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import time

_spec = importlib.util.spec_from_file_location('own_group_observer', Path(__file__).with_name('genesis_process_group_observer.py'))
_observer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_observer)
append_private, group_facts = _observer.append_private, _observer.group_facts


WORKER = '''import json,os,subprocess,sys
from engineering_platform.effect_workspace import child_environment
fd=os.open(sys.argv[2],os.O_RDONLY|os.O_DIRECTORY)
try:
 try:
  p=subprocess.run((sys.argv[1],'--version'),preexec_fn=lambda:os.fchdir(fd),pass_fds=(fd,),env=child_environment(),input=b'',stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5,check=False)
  result={'returncode':p.returncode,'stdout_hex':p.stdout.hex(),'stderr_hex':p.stderr.hex()}
 except subprocess.TimeoutExpired as error:result={'error_type':type(error).__name__,'stdout_hex':(error.stdout or b'').hex(),'stderr_hex':(error.stderr or b'').hex()}
 except subprocess.SubprocessError as error:result={'error_type':type(error).__name__}
 print(json.dumps(result),flush=True)
finally:os.close(fd)
'''


def private_images(group: int, path: Path) -> None:
    """Birth guarded image paths/fd types, encrypted only; never public argv."""
    before = group_facts(group)
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    lib.proc_pidpath.restype = ctypes.c_int
    lib.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    lib.proc_pidinfo.restype = ctypes.c_int
    rows = []
    class FdInfo(ctypes.Structure):
        _fields_ = [('fd', ctypes.c_int32), ('kind', ctypes.c_uint32)]
    for member in before['members']:
        if not member['identity_stable']:
            continue
        pid = member['pid']
        buffer = ctypes.create_string_buffer(4096)
        count = lib.proc_pidpath(pid, buffer, len(buffer))
        fds = (FdInfo * 256)()
        ctypes.set_errno(0)
        size = lib.proc_pidinfo(pid, 1, 0, fds, ctypes.sizeof(fds))  # PROC_PIDLISTFDS
        fd_errno = ctypes.get_errno()
        rows.append({'pid': pid, 'start': member['samples'][0]['start'],
            'image': os.fsdecode(buffer.value) if count > 0 else None,
            'fd_bytes': size, 'fd_errno': fd_errno,
            'fd_inventory_complete':fd_errno == 0 and 0 < size < ctypes.sizeof(fds) and size % ctypes.sizeof(FdInfo) == 0,
            'fds': [{'fd': item.fd, 'kind': item.kind} for item in fds[:max(0, min(size, ctypes.sizeof(fds))) // ctypes.sizeof(FdInfo)]]})
    after = group_facts(group)
    after_births = {m['pid']: m['samples'][0]['start'] for m in after['members'] if m['identity_stable']}
    for row in rows:
        row['birth_guard_after'] = after_births.get(row['pid']) == row['start']
    append_private(path, {'at_ns': time.monotonic_ns(), 'group': group, 'before': before, 'private_images': rows, 'after': after})


def one(args, root: Path, label: str, executable: str) -> dict:
    from engineering_platform import genesis_native, genesis_service
    from engineering_platform.genesis_contract import BootstrapError
    from engineering_platform.provider_process_identity import capture_process_identity, verify_process_identity
    native = genesis_native.runtime()
    command = genesis_native.command(native=native, anchor=root/'a', target=root/'r', writable=False,
        readable=(Path(genesis_service.__file__).resolve().parent.parent,),
        child=(str(args.python), '-I', '-c', WORKER, executable, str(root/'r')), profile_id='ep-genesis-git-'+label)
    deadline = time.monotonic()+30
    stderr = bytearray()
    records = bytearray()
    record = {'label': label}
    process = subprocess.Popen(command, cwd=root/'a', env=genesis_native.environment(native),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    original_identity = capture_process_identity(process.pid, process.pid)
    def drain():
        os.set_blocking(process.stderr.fileno(), False)
        while True:
            try: chunk = os.read(process.stderr.fileno(), 65536)
            except BlockingIOError: return
            if not chunk: return
            room = 262144-len(stderr)
            stderr.extend(chunk[:room])
            if len(chunk) > room: raise RuntimeError('Bounded diagnostic stderr exceeded')
    def drain_tail():
        os.set_blocking(process.stdout.fileno(), False)
        while True:
            try: chunk = os.read(process.stdout.fileno(), 65536)
            except BlockingIOError: return
            if not chunk: return
            room = 262144-len(records)
            records.extend(chunk[:room])
            if len(chunk) > room: raise RuntimeError('Bounded diagnostic stdout exceeded')
    try:
        raw = genesis_service._line(process.stdout, deadline, guard=drain, records=records)
        record['worker'] = json.loads(raw)
        protocol_size = len(records)
        while process.poll() is None:
            drain()
            drain_tail()
            if time.monotonic() >= deadline: raise TimeoutError('Original30s native deadline')
            time.sleep(.01)
        process.wait(timeout=.01)
        drain()
        drain_tail()
        if len(records) != protocol_size: raise RuntimeError('Unexpected diagnostic protocol output')
        record['native_returncode'] = process.returncode
        try:
            genesis_service._require_group_stopped(process.pid)
        except BootstrapError as error:
            record['original_group_check'] = error.code
            record['failure_facts'] = group_facts(process.pid)
            private_images(process.pid, args.output/'private'/('images-'+label+'.jsonl'))
        else:
            record['original_group_check'] = 'ABSENT_ESRCH'
        # Observation within the SAME original deadline; no effect/step is
        # advanced and an earlier original refusal is never relabelled PASS.
        while True:
            drain()
            drain_tail()
            if len(records) != protocol_size: raise RuntimeError('Unexpected diagnostic protocol output')
            try: os.killpg(process.pid, 0)
            except ProcessLookupError:
                record['finally_absent'] = True
                break
            if time.monotonic() >= deadline:
                record['finally_absent'] = False
                break
            time.sleep(.02)
    except Exception as error:
        record['probe_error_type'] = type(error).__name__
        record['primary_error'] = {'type':type(error).__name__,'detail':str(error)}
    finally:
        cleanup_errors = []
        def retained(error):
            cleanup_errors.append({'type':type(error).__name__,'detail':str(error)})
        try:
            if process.poll() is None:
                if original_identity is not None and verify_process_identity(original_identity) == 'MATCH':
                    process.terminate()  # Only this positively identified direct child.
                    try: process.wait(timeout=2)
                    except subprocess.TimeoutExpired: record['direct_child_stop_uncertain'] = True
                else: record['direct_child_stop_uncertain'] = True
        except Exception as error:
            retained(error)
        try: drain()
        except Exception as error: retained(error)
        try: drain_tail()
        except Exception as error: retained(error)
        if 'protocol_size' in locals() and len(records) != protocol_size:
            retained(RuntimeError('Unexpected diagnostic protocol output'))
        for stream in (process.stdout, process.stderr):
            try: stream.close()
            except Exception as error: retained(error)
        for suffix,data in (('stdout',records),('stderr',stderr)):
            try:
                path=args.output/'private'/(label+'.'+suffix)
                path.write_bytes(data)
                path.chmod(0o600)
            except Exception as error:
                retained(error)
        record['cleanup_errors'] = cleanup_errors
        record['control_integrity'] = ('FAIL' if record.get('primary_error') or cleanup_errors
            or record.get('direct_child_stop_uncertain') or not record.get('finally_absent') else 'PASS')
        append_private(args.output/'private'/'original-controls.jsonl', record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('python', 'native', 'output', 'recipient'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    for name in ('python', 'native', 'output', 'recipient'):
        setattr(args, name, getattr(args, name).absolute())
    if sys.platform != 'darwin': raise RuntimeError('Owning Darwin host required')
    os.environ['EP_MANAGED_CODEX_CLI_PREFIX'] = str(args.native)
    args.output.mkdir(mode=0o700)
    (args.output/'private').mkdir(mode=0o700)
    (args.output/'public').mkdir(mode=0o700)
    root = Path('/private/tmp/pgd1')
    root.mkdir(mode=0o700)
    (root/'a').mkdir(mode=0o700)
    (root/'r').mkdir(mode=0o700)
    append_private(args.output/'private'/'root-identity.jsonl', {'device':root.stat().st_dev,'inode':root.stat().st_ino})
    controls = []
    lookup_error = None
    try:
        controls.append(one(args, root, 'system-shim', 'git'))
        if controls[0].get('control_integrity') == 'PASS':
            # Read the existing root-owned OS choice without starting xcrun,
            # xcodebuild or any other selector/cache-warming helper process.
            selection=Path('/var/db/xcode_select_link')
            selection_info=selection.lstat()
            if selection_info.st_uid != 0 or not stat.S_ISLNK(selection_info.st_mode):
                raise RuntimeError('Supported root-owned OS toolchain selection required')
            selection_text=str(selection.readlink())
            selected = (selection/'usr/bin/git').resolve(strict=True)
            if not any(selected.is_relative_to(prefix) for prefix in (Path('/Applications'), Path('/Library/Developer'))):
                raise RuntimeError('Unexpected selected toolchain placement')
            for node in selected.parents:
                info = node.stat()
                if info.st_uid != 0 or info.st_mode & 0o002: raise RuntimeError('Untrusted selected toolchain parent')
            info = selected.stat()
            if (info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode)
                    or not os.access(selected, os.X_OK) or selected.name != 'git'):
                raise RuntimeError('Actual root-owned non-shim Git required')
            selected_digest=hashlib.sha256(selected.read_bytes()).hexdigest()
            selected_identity=tuple(getattr(info,key) for key in ('st_dev','st_ino','st_uid','st_gid','st_mode','st_size','st_mtime_ns','st_ctime_ns'))
            append_private(args.output/'private'/'selected-git.jsonl', {'path':str(selected),'device':info.st_dev,'inode':info.st_ino,
                'sha256':selected_digest, 'mode':stat.S_IMODE(info.st_mode),
                'selection_device':selection_info.st_dev,'selection_inode':selection_info.st_ino,'selection_text':selection_text})
            controls.append(one(args, root, 'system-shim-after-selection-read', 'git'))
            if controls[1].get('control_integrity') == 'PASS':
                controls.append(one(args, root, 'selected-direct', str(selected)))
            fresh=selection.lstat()
            selected_after=selected.stat()
            after_identity=tuple(getattr(selected_after,key) for key in ('st_dev','st_ino','st_uid','st_gid','st_mode','st_size','st_mtime_ns','st_ctime_ns'))
            if ((fresh.st_dev,fresh.st_ino)!=(selection_info.st_dev,selection_info.st_ino)
                    or str(selection.readlink())!=selection_text or after_identity!=selected_identity
                    or hashlib.sha256(selected.read_bytes()).hexdigest()!=selected_digest):
                raise RuntimeError('Selected OS toolchain changed during comparison')
    except Exception as error:
        lookup_error = type(error).__name__
        append_private(args.output/'private'/'control-errors.jsonl', {'type':lookup_error,'detail':str(error)})
    public_controls = []
    for row in controls:
        public_controls.append({key:row[key] for key in ('label','native_returncode','original_group_check','failure_facts',
            'finally_absent','probe_error_type','direct_child_stop_uncertain','control_integrity') if key in row})
        public_controls[-1]['git_returncode'] = row.get('worker',{}).get('returncode')
        public_controls[-1]['git_error_type'] = row.get('worker',{}).get('error_type')
    diagnosis = (len(controls)==3 and not lookup_error
        and all(row.get('original_group_check')=='BOOTSTRAP_HOST_GROUP_ACTIVE_OR_UNCERTAIN' for row in controls[:2])
        and controls[2].get('original_group_check')=='ABSENT_ESRCH' and controls[2].get('native_returncode')==0
        and controls[2].get('worker',{}).get('returncode')==0
        and all(row.get('control_integrity')=='PASS' for row in controls))
    summary = {'result':'SHIM_FAILURE_PERSISTS_SELECTED_DIRECT_SUCCEEDS' if diagnosis else 'NO_CLOSING_DIAGNOSIS',
        'controls':public_controls,'driver_error_type':lookup_error,
        'meaning':'Read-only native toolchain diagnosis; no productrepair/Genesis qualification PASS'}
    archive = args.output/'private'/'original.tar.gz'
    with tarfile.open(archive,'w:gz') as saved:
        for path in sorted((args.output/'private').iterdir()):
            if path != archive: saved.add(path, arcname=path.name)
    encrypted = args.output/'public'/'primary-evidence.cms'
    subprocess.run(('openssl','cms','-encrypt','-binary','-aes-256-cbc','-in',str(archive),'-outform','DER','-out',str(encrypted),str(args.recipient)),check=True,capture_output=True)
    summary['private_archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    summary['encrypted_archive_sha256'] = hashlib.sha256(encrypted.read_bytes()).hexdigest()
    (args.output/'public'/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({'result':summary['result'],'controls':len(controls)}))
    return 0 if diagnosis else 1


if __name__ == '__main__': raise SystemExit(main())
