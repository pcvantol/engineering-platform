"""Owned installed HTTP Console; loss injection preserves the original store."""
import hashlib,io,json,sys
from contextlib import redirect_stdout
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tests.engineering.test_console_stream_access import ConsoleStreamAccessTests
from engineering_platform import server

proof=Path(sys.argv[1]);proof.mkdir(mode=0o700,parents=True,exist_ok=True)
case=ConsoleStreamAccessTests();case.setUp()
original_audit=server._audit_console_read_loss
original_control=json.dumps
moved=case.fixture.database.with_suffix('.unavailable')
try:
    with redirect_stdout(io.StringIO()):
        assert server.main(['bootstrap-topology','--data-root',str(case.fixture.data),'--project-id','none','--repository-id','repo-none'])==0
    before=case.fixture.database.read_bytes()
    (proof/'original.sqlite').write_bytes(before);(proof/'original.sqlite').chmod(0o600)
    def audit(error):
        original_audit(error)
        if moved.exists():
            assert not case.fixture.database.exists()
            assert moved.read_bytes()==before
            moved.rename(case.fixture.database)
    server._audit_console_read_loss=audit
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
            assert not moved.exists();case.fixture.database.rename(moved);emit({'lost':True})
        elif command=='status':
            emit({'restored':case.fixture.database.exists(),'replacement':case.fixture.database.exists() and case.fixture.database.read_bytes()!=before,'errors':[type(e).__name__ for e in case.errors]})
        elif command=='stop':break
finally:
    server._audit_console_read_loss=original_audit;server.json.dumps=original_control
    if moved.exists():moved.rename(case.fixture.database)
    case.doCleanups()
    print(original_control({'stopped':not case.thread.is_alive(),'fixture_removed':not case.fixture.area.exists(),'errors':[type(e).__name__ for e in case.errors]}),flush=True)
