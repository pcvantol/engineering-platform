"""Test-only, durable physical and logical evidence at an owned SQLite barrier."""
from __future__ import annotations
import hashlib,json,os,sqlite3,threading,inspect
from contextlib import contextmanager
from pathlib import Path


class DatabaseEvidence:
    def __init__(self, database: Path, root: Path):
        self.database,self.root=database,root
        root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.lock=threading.RLock();self.event_lock=threading.RLock()
        self.phases={};self.events=[];self.violations=[];self.retention_errors=[]
        self.original_connect=sqlite3.connect
        self.moved=database.with_suffix('.unavailable')
        self.loss_started=False;self.restored=False;self.replacement=False;self.failed_loss_recorded=False;self.claim_a_checked=False;self.pre_loss_changes=[]

    def write(self,path: Path,data: bytes):
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
        except Exception as error:
            self.retention_errors.append({'path':str(path),'type':type(error).__name__})
            raise

    def event(self,kind,**facts):
        with self.event_lock:
            record={'ordinal':len(self.events),'kind':kind,**facts}
            self.events.append(record)
            try:
                with (self.root/'events.jsonl').open('ab') as f:
                    os.chmod(f.name,0o600);f.write((json.dumps(record,sort_keys=True)+'\n').encode());f.flush();os.fsync(f.fileno())
            except Exception as error:
                self.retention_errors.append({'type':type(error).__name__,'stage':'event'})

    def traced_connect(self,*args,**kwargs):
        connection=self.original_connect(*args,**kwargs)
        def observe(statement):
            verb=statement.strip().split(None,1)[0].upper() if statement.strip() else ''
            if verb in {'INSERT','UPDATE','DELETE','REPLACE','CREATE','ALTER','DROP'}:
                frames=[{'function':f.function,'file':f.filename,'line':f.lineno} for f in inspect.stack()
                        if 'engineering_platform' in f.filename or 'console_scope_fixture' in f.filename]
                self.event('actual_sql_write',database=str(args[0]) if args else str(kwargs.get('database')),statement=statement,owners=frames)
        connection.set_trace_callback(observe)
        return connection

    @contextmanager
    def stable(self,path):
        # rw cannot create a missing file. A narrow own-store RESERVED lock
        # blocks writers while copying physical files and their logical view.
        connection=self.original_connect(path.resolve().as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=5)
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
        finally:
            if connection.in_transaction:connection.rollback()
            connection.close()

    def snapshot(self,name,path,connection=None):
        if connection is None:
            with self.stable(path) as current:return self.snapshot(name,path,current)
        directory=self.root/name;directory.mkdir(mode=0o700)
        physical={}
        for suffix in ['', '-wal','-shm','-journal']:
            source=Path(str(path)+suffix)
            if source.exists():
                data=source.read_bytes();stat=source.stat();self.write(directory/(name+suffix+'.sqlite'),data)
                physical[suffix]={'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'dev':stat.st_dev,'ino':stat.st_ino}
        logical={}
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            columns=[row[1] for row in connection.execute('PRAGMA table_info("'+table.replace('"','""')+'")')]
            rows=list(connection.execute('SELECT * FROM "'+table.replace('"','""')+'"'))
            logical[table]={'columns':columns,'rows':sorted([list(row) for row in rows],key=repr)}
        schema=list(connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))
        value={'physical':physical,'logical':logical,'schema':schema,'writer_event_count':len(self.events)}
        self.write(directory/'snapshot.json',(json.dumps(value,sort_keys=True,default=repr)+'\n').encode())
        self.phases[name]=value;self.event('snapshot_retained',phase=name,physical=physical)
        return value

    def loss(self):
        try:
            with self.lock,self.stable(self.database) as connection:
                current=self.snapshot('T1',self.database,connection)
                self.claim_a()
                self.database.rename(self.moved);self.loss_started=True
                # The original open connection remains bound to the moved inode.
                self.snapshot('T2-at-loss',self.moved,connection)
                self.event('actual_source_renamed',identity=current['physical'][''])
        except Exception as error:
            self.retention_errors.append({'type':type(error).__name__,'stage':'loss_boundary'})
            self.violations.append('loss_boundary_evidence_failed')
            raise

    def restore(self):
        with self.lock:
            if not self.moved.exists() or self.failed_loss_recorded:return
            try:
                with self.stable(self.moved) as connection:
                    moved=self.snapshot('T2-before-restore',self.moved,connection)
                    self.replacement=self.database.exists()
                    if self.replacement:
                        # Never overwrite a replacement; preserve even invalid bytes.
                        path=self.root/'replacement.sqlite';self.write(path,self.database.read_bytes())
                        self.violations.append('replacement_file_created')
                        self.database.rename(self.root/'replacement-retained.sqlite')
                    base=self.phases['T1']
                    if moved['physical']['']['sha256']!=base['physical']['']['sha256']:
                        self.violations.append('moved_source_bytes_changed')
                    if moved['physical']['']['ino']!=base['physical']['']['ino']:
                        self.violations.append('moved_source_identity_changed')
                    if moved['logical']!=base['logical'] or moved['schema']!=base['schema']:
                        self.violations.append('moved_source_domain_changed')
                    for suffix in ['-wal','-journal']:
                        if moved['physical'].get(suffix)!=base['physical'].get(suffix):self.violations.append('source_journal_changed:'+suffix)
                    if any(v.startswith(('moved_source_','source_journal_','unexplained_pre_loss_','unapproved_pre_loss_')) for v in self.violations):
                        self.failed_loss_recorded=True
                        return
                    # link is a non-overwriting publication boundary. A creator
                    # racing the check cannot have its evidence silently replaced.
                    try:
                        os.link(self.moved,self.database)
                    except FileExistsError:
                        self.replacement=True
                        self.write(self.root/'late-replacement.sqlite',self.database.read_bytes())
                        self.violations.append('late_replacement_file_created')
                        self.database.rename(self.root/'late-replacement-retained.sqlite')
                        os.link(self.moved,self.database)
                    self.moved.unlink();self.restored=True
                    self.snapshot('T-restored',self.database,connection)
            except Exception as error:
                self.retention_errors.append({'type':type(error).__name__,'stage':'restore'})
                # Preserve the primary server outcome, but keep the verdict red.
                # Failure bytes remain in the own store until manual recovery.
                self.violations.append('evidence_retention_or_restore_failed')

    def claim_a(self):
        if self.claim_a_checked:return self.pre_loss_changes
        self.claim_a_checked=True
        t0,t1=self.phases['T0'],self.phases['T1'];changes=[]
        for table in sorted(set(t0['logical'])|set(t1['logical'])):
            a,b=t0['logical'].get(table),t1['logical'].get(table)
            if a!=b:changes.append({'table':table,'before':a,'after':b})
        self.pre_loss_changes=changes
        if (t0['physical']['']['dev'],t0['physical']['']['ino'])!=(t1['physical']['']['dev'],t1['physical']['']['ino']):
            self.violations.append('unexplained_pre_loss_identity_change')
        self.write(self.root/'T0-T1-domain-diff.json',(json.dumps(changes,sort_keys=True,default=repr)+'\n').encode())
        if t0['schema']!=t1['schema']:self.violations.append('unexplained_pre_loss_schema_change')
        # Replay only specifically owned observations/setup into the retained
        # T0 image. Every row, schema object and required physical byte remains
        # compared; no domain/table/field is erased from the evidence.
        if changes or t0['physical']['']['sha256']!=t1['physical']['']['sha256']:
            replay=self.root/'claim-a-replay.sqlite'
            self.write(replay,(self.root/'T0/T0.sqlite').read_bytes())
            for suffix in ['-wal','-journal']:
                saved=self.root/'T0'/('T0'+suffix+'.sqlite')
                if saved.exists():self.write(Path(str(replay)+suffix),saved.read_bytes())
            count=0
            connection=self.original_connect(replay,isolation_level=None)
            try:
                for event in self.events[:t1['writer_event_count']]:
                    if event['kind']!='actual_sql_write':continue
                    if str(self.database) not in event['database']:continue
                    sql=event['statement'];owners={f['function'] for f in event['owners']}
                    compact=' '.join(sql.split()).upper()
                    capacity=(compact.startswith('INSERT INTO ENGINEERING_METADATA(')
                              and 'ep.provider_capacity_history.v1' in sql
                              and 'record_provider_capacity' in owners
                              and ('_provider_capacity_projection' in owners or '_augment_dashboard_snapshot' in owners))
                    audit=(compact.startswith('INSERT INTO ENGINEERING_COMPONENT_LOGS(')
                           and '_audit_dashboard_action' in owners and 'emit' in owners)
                    setup=(compact.startswith('INSERT INTO ENGINEERING_METADATA(')
                           and "'console.log_level'" in sql and "'\"DEBUG\"'" in sql
                           and 'update_console_interval_configuration' in owners
                           and any(e['kind']=='declared_fixture_setup' for e in self.events[:event['ordinal']]))
                    if not (capacity or audit or setup):
                        self.violations.append('unapproved_pre_loss_writer');continue
                    connection.execute(sql);count+=1
                self.snapshot('T0-replayed-owned-writes',replay)
            finally:connection.close()
            calculated=self.phases['T0-replayed-owned-writes']
            if calculated['logical']!=t1['logical'] or calculated['schema']!=t1['schema']:
                self.violations.append('unexplained_pre_loss_domain_change')
            if calculated['physical']['']['sha256']!=t1['physical']['']['sha256']:
                self.violations.append('unexplained_pre_loss_physical_change')
            self.event('claim_a_replay_checked',approved_actual_statements=count,
                       equivalent=calculated['logical']==t1['logical'],physical_equal=calculated['physical']['']['sha256']==t1['physical']['']['sha256'])
        return changes

    def verdict(self):
        if 'T1' in self.phases:self.claim_a()
        else:self.violations.append('loss_boundary_not_observed')
        result={'phases':{k:v['physical'] for k,v in self.phases.items()},'restored':self.restored,
                'replacement':self.replacement,'violations':self.violations,'retention_errors':self.retention_errors,
                'qualified':not self.violations and not self.retention_errors}
        self.write(self.root/'verdict.json',(json.dumps(result,sort_keys=True)+'\n').encode())
        return result
