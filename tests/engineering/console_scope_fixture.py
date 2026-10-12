"""Owned installed HTTP Console; loss injection preserves the original store."""
import hashlib,io,json,sys,inspect,sqlite3,threading
from contextlib import redirect_stdout,closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tests.engineering.test_console_stream_access import ConsoleStreamAccessTests
from engineering_platform import server
from tests.engineering.console_database_evidence import DatabaseEvidence

proof=Path(sys.argv[1]);proof.mkdir(mode=0o700,parents=True,exist_ok=True)
case=ConsoleStreamAccessTests();case.setUp()
original_audit=server._audit_console_read_loss
original_control=json.dumps
original_platform=server._no_project_console_snapshot
original_project=server._central_console_project_snapshot
mode=sys.argv[2] if len(sys.argv)>2 else "normal"
armed=False
moved=case.fixture.database.with_suffix('.unavailable')
try:
    with redirect_stdout(io.StringIO()):
        assert server.main(['bootstrap-topology','--data-root',str(case.fixture.data),'--project-id','none','--repository-id','repo-none'])==0
    evidence=DatabaseEvidence(case.fixture.database,proof)
    evidence.snapshot('T0',case.fixture.database)
    before=case.fixture.database.read_bytes()
    sqlite3.connect=evidence.traced_connect
    if mode=='retention-failure':
        retained_write=evidence.write
        def failing_write(path,data):
            if path.parent.name=='T2-before-restore':
                raise OSError('controlled T2 evidence retention failure')
            return retained_write(path,data)
        evidence.write=failing_write
    if mode=='pre-setup':
        evidence.event('declared_fixture_setup',action='console.log_level=DEBUG')
        server.central_database.update_console_interval_configuration(case.fixture.data,'log_level','DEBUG')
    def audit(error):
        original_audit(error)
        if moved.exists():
            if mode=='tamper-loss':
                with closing(evidence.original_connect(moved)) as fault:
                    fault.execute("UPDATE engineering_metadata SET value='unapproved' WHERE key='installation.instance_id'");fault.commit()
            elif mode=='replacement-loss' and not case.fixture.database.exists():
                case.fixture.database.write_bytes(b'controlled replacement invariant failure')
            evidence.restore()
    server._audit_console_read_loss=audit
    def loss_boundary(reader, owner):
        def read(*args,**kwargs):
            global armed
            if armed and any(frame.function==owner for frame in inspect.stack()):
                armed=False
                evidence.loss()
            return reader(*args,**kwargs)
        return read
    server._no_project_console_snapshot=loss_boundary(original_platform,'_stream_no_project_console_events')
    server._central_console_project_snapshot=loss_boundary(original_project,'_stream_project_console_events')
    # Refault changes only the emitted PLATFORM identity; the real reader,
    # response, browser asset and client remain the selected installed bytes.
    if len(sys.argv)>2 and sys.argv[2]=='refault':
        def encode(value,*args,**kwargs):
            if isinstance(value,dict) and value.get('scope')=='PLATFORM' and value.get('outcome')=='UNAVAILABLE':
                value={**value,'project_id':''}
            return original_control(value,*args,**kwargs)
        server.json.dumps=encode
    def emit(value):print(original_control(value),flush=True)
    emit({'url':f'http://127.0.0.1:{case.httpd.server_port}/','module':server.__file__,'original_sha256':hashlib.sha256(before).hexdigest()})
    for line in sys.stdin:
        command=line.strip()
        if command=='lose':
            assert not moved.exists();armed=True;emit({'loss_armed':True})
        elif command=='status':
            emit({'restored':evidence.restored,'replacement':evidence.replacement,'errors':[type(e).__name__ for e in case.errors]})
        elif command=='stop':break
finally:
    server._audit_console_read_loss=original_audit;server.json.dumps=original_control
    server._no_project_console_snapshot=original_platform;server._central_console_project_snapshot=original_project
    sqlite3.connect=evidence.original_connect
    if moved.exists() and not evidence.failed_loss_recorded:evidence.restore()
    try:
        verdict=evidence.verdict()
    except Exception as error:
        evidence.retention_errors.append({'type':type(error).__name__,'stage':'final_verdict'})
        verdict={'qualified':False,'violations':evidence.violations,'retention_errors':evidence.retention_errors}
    if not evidence.retention_errors:
        case.doCleanups()
    else:
        case.stop()
        case.fixture.temp._finalizer.detach()
    evidence.write(proof/'closure.json',original_control({'stopped':not case.thread.is_alive(),'fixture_removed':not case.fixture.area.exists(),'errors':[type(e).__name__ for e in case.errors],'evidence':verdict}).encode())
    print(original_control({'stopped':not case.thread.is_alive(),'fixture_removed':not case.fixture.area.exists(),'errors':[type(e).__name__ for e in case.errors]}),flush=True)

if not verdict['qualified'] or case.errors:sys.exit(1)
