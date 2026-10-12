"""Owned installed HTTP Console; loss injection preserves the original store."""
import hashlib,io,json,sys,inspect,sqlite3,threading,socket,time,os
from contextlib import redirect_stdout,closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tests.engineering.dashboard_run_evidence_fixture import RealTopologyFixture,OwnedRequestServer,TrackedRequestHandler,IsolatedProviderTransport
from types import SimpleNamespace
from engineering_platform import server
from tests.engineering.console_database_evidence import DatabaseEvidence

proof=Path(sys.argv[1]);proof.mkdir(mode=0o700,parents=True,exist_ok=True)
class ScopeRequestHandler(TrackedRequestHandler):
    # End each completed ordinary request instead of leaving an idle
    # keep-alive socket classified as an executing writer at teardown.
    def do_GET(self):
        try:return super().do_GET()
        finally:self.close_connection=True
    def do_POST(self):
        try:return super().do_POST()
        finally:self.close_connection=True

fixture=RealTopologyFixture()
httpd=OwnedRequestServer(('127.0.0.1',0),ScopeRequestHandler);httpd.data_root=fixture.data
thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
case=SimpleNamespace(fixture=fixture,httpd=httpd,thread=thread)
transport=IsolatedProviderTransport();transport.start()
def errors():
    return [item['exception_type'] for item in httpd.handler_errors if not item['client_disconnect']]
original_audit=server._audit_console_read_loss
original_control=json.dumps
original_platform=server._no_project_console_snapshot
original_project=server._central_console_project_snapshot
mode=sys.argv[2] if len(sys.argv)>2 else "normal"
armed=False
moved=case.fixture.database.with_suffix('.unavailable')
evidence=DatabaseEvidence(case.fixture.database,proof)
try:
    with redirect_stdout(io.StringIO()):
        assert server.main(['bootstrap-topology','--data-root',str(case.fixture.data),'--project-id','none','--repository-id','repo-none'])==0
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
            emit({'restored':evidence.restored,'replacement':evidence.replacement,'errors':errors()})
        elif command=='stop':break
finally:
    # Existing admission/handler and external-transport lifecycle, unchanged
    # five-second metadata deadline. No in-flight ordinary writer is dropped.
    deadline=time.monotonic()+5
    close={'ordinary_quiescent':False}
    all_requests_stopped=False
    try:
        # Stop admission before ending accepted sockets that have not
        # entered any route (browser preconnect/idle transport, no writer).
        # Their actual handler-thread exit is still required below.
        with httpd.request_condition:
            httpd.admission_closed=True
            initial_requests=list(httpd.requests.values())
            for request,route in list(httpd.requests.items()):
                if route is None:
                    try:request.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
        close=httpd.quiesce();thread.join(max(0,deadline-time.monotonic()))
        if close['ordinary_quiescent']:
            with httpd.request_condition:
                for request in list(httpd.requests):
                    try:request.shutdown(socket.SHUT_RDWR)
                    except OSError:pass  # Already closed own stream socket.
                while httpd.requests and time.monotonic()<deadline:
                    httpd.request_condition.wait(max(0,deadline-time.monotonic()))
                all_requests_stopped=not httpd.requests
    except Exception as error:
        evidence.retention_errors.append({'stage':'handler_closure','type':type(error).__name__})
    # Streams may still be inside their unchanged product refresh interval.
    # Retain every guard until process exit, as the existing OwnedReadWorker
    # does. End metadata children without restoring their transport adapters.
    try:
        from engineering_platform.providers import CodexCliProvider
        for process in transport.processes:
            if process.poll() is None:CodexCliProvider().close_app_server(process)
    except Exception as error:
        evidence.retention_errors.append({'stage':'transport_closure','type':type(error).__name__})
    if not close['ordinary_quiescent']:
        evidence.retention_errors.append({'stage':'handler_closure','type':'ordinary_handlers_incomplete'})
    if moved.exists() and not evidence.failed_loss_recorded:evidence.restore()
    if case.fixture.database.exists() and not evidence.retention_errors:
        try:
            evidence.snapshot('T-final-after-handlers',case.fixture.database)
        except Exception as error:
            evidence.retention_errors.append({'stage':'final_snapshot','type':type(error).__name__})
    try:
        verdict=evidence.verdict()
    except Exception as error:
        evidence.retention_errors.append({'type':type(error).__name__,'stage':'final_verdict'})
        verdict={'qualified':False,'violations':evidence.violations,'retention_errors':evidence.retention_errors}
    # The browser owner retains final database bytes and cleans this exact
    # synthetic own-store only AFTER observing actual child close.
    case.fixture.temp._finalizer.detach()
    owner=original_control({'proof':str(proof),'pid':os.getpid()})
    (case.fixture.area/'scope-owner.json').write_text(owner)
    os.chmod(case.fixture.area/'scope-owner.json',0o600)
    closure={'stopped':not case.thread.is_alive(),'fixture_removed':not case.fixture.area.exists(),
             'errors':errors(),'ordinary_handlers_completed':close['ordinary_quiescent'],
             'request_admission_closed':httpd.admission_closed,'all_requests_stopped':all_requests_stopped,
             'requests_at_shutdown':initial_requests,'guards_retained_through_exit':True,
             'own_store':str(case.fixture.area),'database':str(case.fixture.database),'pid':os.getpid(),
             'transport':{'unexpected':transport.unexpected,'processes_stopped':all(p.poll() is not None for p in transport.processes)},'evidence':verdict}
    evidence.write(proof/'closure.json',original_control(closure).encode())
    print(original_control({k:v for k,v in closure.items() if k!='evidence'}),flush=True)

# No atexit hook may remove evidence or restore adapters with live streams.
os._exit(1 if not verdict['qualified'] or errors() else 0)
