import { spawn } from 'node:child_process';
import { mkdtempSync,writeFileSync,mkdirSync,readFileSync,existsSync,rmSync,statSync } from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { test,expect } from './dashboard_coverage_fixture.mjs';
import { retainWorkerLogs,finishWorkerLogs } from './dashboard_worker_logs.mjs';

for (const selected of ['', 'project', 'none']) for (const auto of [true,false]) {
 test(`actual installed terminal scope ${selected || 'PLATFORM'} auto=${auto}`,async({page},info)=>{
  test.setTimeout(30000);
  const python=process.env.EP_RUN_EVIDENCE_INSTALLED_PYTHON;
  expect(python).toBeTruthy();
  const privateRoot=process.env.EP_BROWSER_PRIVATE_LOG_ROOT || tmpdir();
  mkdirSync(privateRoot,{recursive:true,mode:0o700});
  const evidence=mkdtempSync(path.join(privateRoot,'scope-proof-'));
  const env={...process.env};delete env.PYTHONPATH;
  const child=spawn(python,[path.resolve('tests/engineering/console_scope_fixture.py'),evidence,...(process.env.EP_DATABASE_EVIDENCE_MODE?[process.env.EP_DATABASE_EVIDENCE_MODE]:process.env.EP_SCOPE_REFAULT==='1'?['refault']:[])],{cwd:process.cwd(),env,stdio:['pipe','pipe','pipe']});
  retainWorkerLogs(child,info,'scope-worker',{privateRoot});
  const queue=[];let buffer='',pending;
  child.stdout.on('data',chunk=>{buffer+=chunk;while(buffer.includes('\n')){const i=buffer.indexOf('\n'),line=buffer.slice(0,i);buffer=buffer.slice(i+1);if(line.startsWith('{')){const message=JSON.parse(line);if(pending){const resolve=pending;pending=null;resolve(message);}else queue.push(message);}}});
  const next=()=>queue.length?Promise.resolve(queue.shift()):new Promise((resolve,reject)=>{const t=setTimeout(()=>reject(Error('Owned scope worker timeout; logs retained')),10000);pending=x=>{clearTimeout(t);resolve(x);};});
  const closed=new Promise(resolve=>child.once('close',resolve));
  let facts;
  try {
   const ready=await next();expect(ready.module).toContain('site-packages/engineering_platform');
   await page.addInitScript(()=>{
    const Native=window.EventSource;
    window.scopeStreams=[];
    window.EventSource=class extends Native {
     constructor(...args){super(...args);const fact={frames:[],controls:[],closes:0};window.scopeStreams.push({source:this,fact});
      this.addEventListener('dashboard',e=>fact.frames.push(JSON.parse(e.data)));
      this.addEventListener('console-read-error',e=>fact.controls.push(JSON.parse(e.data)));this.fact=fact;}
     close(){this.fact.closes++;return super.close();}
    };
   });
   const url=ready.url+(selected?'?project='+selected:'');
   await page.goto(url,{waitUntil:'domcontentloaded'});
   await expect.poll(()=>page.evaluate(()=>scopeStreams[0]?.fact.frames.length||0)).toBeGreaterThan(0);
   // Capture healthy actual response/scope before loss; no mocked success.
   const response=await page.request.get(ready.url+'api/dashboard-snapshot'+(selected?'?project='+selected:''));
   expect(response.ok()).toBeTruthy();const snapshot=await response.json();expect(snapshot.scope).toBe(selected?'PROJECT':'PLATFORM');
   await page.locator('#autoRefresh').setChecked(auto);
   // Invalid, absent and foreign controls may not invalidate this document.
   if (process.env.EP_SCOPE_BASELINE !== "1") await page.evaluate(selected=>{
    const source=scopeStreams[0].source;
    for(const value of [null,[],{}, {scope:'PROJECT',project_id:'foreign',outcome:'UNAVAILABLE',error:'CENTRAL_UNAVAILABLE'},
      {scope:'PLATFORM',project_id:'',outcome:'UNAVAILABLE',error:'CENTRAL_UNAVAILABLE'},
      {scope:'PROJECT',project_id:null,outcome:'UNAVAILABLE',error:'CENTRAL_UNAVAILABLE'},
      {scope:selected?'PLATFORM':'PROJECT',project_id:selected?null:'none',outcome:'UNAVAILABLE',error:'CENTRAL_UNAVAILABLE'}])
     source.dispatchEvent(new MessageEvent('console-read-error',{data:JSON.stringify(value)}));
    source.dispatchEvent(new MessageEvent('console-read-error',{data:'invalid JSON'}));
    source.fact.controls=[];
   },selected);
   expect(await page.locator('body').getAttribute('data-console-read-outcome')).toBeNull();
   child.stdin.write('lose\n');await next();
   await expect.poll(()=>page.evaluate(()=>scopeStreams[0].fact.controls.length)).toBeGreaterThan(0);
   await expect(page.locator('body')).toHaveAttribute('data-console-read-outcome','UNAVAILABLE',{timeout:2500});
   await expect.poll(()=>page.evaluate(()=>scopeStreams[0].source.readyState)).toBe(2);
   await expect(page.locator('#updateMode')).toHaveText(/unavailable|niet beschikbaar/i);
   child.stdin.write('status\n');const stored=await next();if (!process.env.EP_DATABASE_EVIDENCE_NEGATIVE) expect(stored).toEqual({restored:true,replacement:false,errors:[]});
   facts=await page.evaluate(()=>({projectId:document.body.dataset.projectId,scope:window.ENGINEERING_PLATFORM_NO_PROJECT?'PLATFORM':'PROJECT',stream:scopeStreams[0].fact,readyState:scopeStreams[0].source.readyState,outcome:document.body.dataset.consoleReadOutcome,status:document.querySelector('#updateMode').textContent}));
   expect(facts.stream.controls.at(-1)).toEqual({scope:selected?'PROJECT':'PLATFORM',project_id:selected||null,error:'CENTRAL_UNAVAILABLE',outcome:'UNAVAILABLE'});
   // The closed stream's late dashboard and error cannot restore old data.
   await page.evaluate(()=>{const source=scopeStreams[0].source;source.dispatchEvent(new MessageEvent('dashboard',{data:JSON.stringify(scopeStreams[0].fact.frames[0])}));source.dispatchEvent(new Event('error'));});
   await expect(page.locator('body')).toHaveAttribute('data-console-read-outcome','UNAVAILABLE');
   expect(await page.evaluate(()=>scopeStreams[0].source.readyState)).toBe(2);
   await page.screenshot({path:info.outputPath('actual-unavailable.png')});
   writeFileSync(info.outputPath('scope-wire-browser.json'),JSON.stringify({ready,snapshot,facts,stored,auto,source:process.env.EP_RUN_EVIDENCE_SOURCE_SHA,tree:process.env.EP_RUN_EVIDENCE_SOURCE_TREE,wheel:process.env.EP_RUN_EVIDENCE_WHEEL_SHA256},null,2));
   // Only a new authorised document may establish scope after source restore.
   if (!process.env.EP_DATABASE_EVIDENCE_NEGATIVE) {
    await page.goto(ready.url+'?project='+(selected==='project'?'none':'project'),{waitUntil:'domcontentloaded'});
    await expect.poll(()=>page.evaluate(()=>scopeStreams[0]?.fact.frames.length||0)).toBeGreaterThan(0);
    expect(await page.locator('body').getAttribute('data-console-read-outcome')).toBeNull();
   }
  } finally {
   await page.goto('about:blank');child.stdin.write('stop\n');await closed;
   await finishWorkerLogs(child,info);
   const verdict=JSON.parse(readFileSync(path.join(evidence,'verdict.json'),'utf8'));
   const closure=JSON.parse(readFileSync(path.join(evidence,'closure.json'),'utf8'));
   // Kernel-observed child close precedes this closed-store capture/cleanup.
   // No server handler or SQLite connection can still write this own process.
   expect(child.exitCode).not.toBeNull();
   const owner=JSON.parse(readFileSync(path.join(closure.own_store,'scope-owner.json'),'utf8'));
   expect(owner).toEqual({proof:evidence,pid:child.pid});
   expect(path.basename(closure.own_store)).toMatch(/^ep-dashboard-stored-canary-/);
   expect(path.resolve(closure.database)).toContain(path.resolve(closure.own_store)+path.sep);
   if(process.env.EP_DATABASE_EVIDENCE_MODE==='closed-store-tamper') {
    // Deterministic negative: last consistent original is already retained.
    // A physical fault after true process stop must remain red before cleanup.
    writeFileSync(closure.database,Buffer.concat([readFileSync(closure.database),Buffer.from('controlled post-close byte fault')]));
   }
   const finalFiles={};
   for(const suffix of ['', '.unavailable','-wal','-shm','-journal']) {
    const file=suffix==='.unavailable'?closure.database.replace(/\.[^/.]+$/,'.unavailable'):closure.database+suffix;
    if(existsSync(file)) {
     const bytes=readFileSync(file),identity=statSync(file);
     writeFileSync(path.join(evidence,'after-process-stop'+suffix+'.sqlite'),bytes,{flag:'wx',mode:0o600});
     finalFiles[suffix]={sha256:createHash('sha256').update(bytes).digest('hex'),bytes:bytes.length,dev:identity.dev,ino:identity.ino};
    }
   }
   const finalSnapshot=verdict.phases['T-final-after-handlers'];
   const finalViolations=[];
   if(finalSnapshot) {
    for(const suffix of ['', '-wal','-shm','-journal']) {
     if(['sha256','bytes','dev','ino'].some(key=>(finalFiles[suffix]?.[key]??null)!==(finalSnapshot[suffix]?.[key]??null)))
      finalViolations.push('closed_store_changed:'+suffix);
    }
   } else if(verdict.qualified) finalViolations.push('final_consistent_snapshot_missing');
   const postClose={process_stopped:true,finalFiles,violations:finalViolations,fixture_removed:false};
   // Incomplete/failed evidence preserves the original failure own-store.
   if(verdict.qualified && finalViolations.length===0 && closure.ordinary_handlers_completed && closure.transport.unexpected.length===0 && closure.transport.processes_stopped) {
    rmSync(closure.own_store,{recursive:true});postClose.fixture_removed=!existsSync(closure.own_store);
   }
   writeFileSync(path.join(evidence,'post-close.json'),JSON.stringify(postClose),{flag:'wx',mode:0o600});
   writeFileSync(info.outputPath('database-evidence-receipt.json'),JSON.stringify({verdict,closure,postClose},null,2));
   expect(verdict.retention_errors).toEqual([]);
   expect(verdict.violations).toEqual([]);
   expect(verdict.qualified).toBe(true);
   expect(closure.stopped).toBe(true);
   expect(closure.ordinary_handlers_completed).toBe(true);
   expect(closure.request_admission_closed).toBe(true);
   expect(closure.guards_retained_through_exit).toBe(true);
   expect(postClose.process_stopped).toBe(true);
   expect(postClose.violations).toEqual([]);
   expect(closure.transport.unexpected).toEqual([]);
   expect(closure.transport.processes_stopped).toBe(true);
   expect(postClose.fixture_removed).toBe(true);
   expect(child.exitCode).toBe(0);
  }
 });
}
