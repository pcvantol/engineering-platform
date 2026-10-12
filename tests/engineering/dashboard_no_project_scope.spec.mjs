import { spawn } from 'node:child_process';
import { mkdtempSync,writeFileSync,mkdirSync } from 'node:fs';
import path from 'node:path';
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
  const child=spawn(python,[path.resolve('tests/engineering/console_scope_fixture.py'),evidence,...(process.env.EP_SCOPE_REFAULT==='1'?['refault']:[])],{cwd:process.cwd(),env,stdio:['pipe','pipe','pipe']});
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
   child.stdin.write('status\n');const stored=await next();expect(stored).toEqual({restored:true,replacement:false,errors:[]});
   facts=await page.evaluate(()=>({projectId:document.body.dataset.projectId,scope:window.ENGINEERING_PLATFORM_NO_PROJECT?'PLATFORM':'PROJECT',stream:scopeStreams[0].fact,readyState:scopeStreams[0].source.readyState,outcome:document.body.dataset.consoleReadOutcome,status:document.querySelector('#updateMode').textContent}));
   expect(facts.stream.controls.at(-1)).toEqual({scope:selected?'PROJECT':'PLATFORM',project_id:selected||null,error:'CENTRAL_UNAVAILABLE',outcome:'UNAVAILABLE'});
   // The closed stream's late dashboard and error cannot restore old data.
   await page.evaluate(()=>{const source=scopeStreams[0].source;source.dispatchEvent(new MessageEvent('dashboard',{data:JSON.stringify(scopeStreams[0].fact.frames[0])}));source.dispatchEvent(new Event('error'));});
   await expect(page.locator('body')).toHaveAttribute('data-console-read-outcome','UNAVAILABLE');
   expect(await page.evaluate(()=>scopeStreams[0].source.readyState)).toBe(2);
   await page.screenshot({path:info.outputPath('actual-unavailable.png')});
   writeFileSync(info.outputPath('scope-wire-browser.json'),JSON.stringify({ready,snapshot,facts,stored,auto,source:process.env.EP_RUN_EVIDENCE_SOURCE_SHA,tree:process.env.EP_RUN_EVIDENCE_SOURCE_TREE,wheel:process.env.EP_RUN_EVIDENCE_WHEEL_SHA256},null,2));
   // Only a new authorised document may establish scope after source restore.
   await page.goto(ready.url+'?project='+(selected==='project'?'none':'project'),{waitUntil:'domcontentloaded'});
   await expect.poll(()=>page.evaluate(()=>scopeStreams[0]?.fact.frames.length||0)).toBeGreaterThan(0);
   expect(await page.locator('body').getAttribute('data-console-read-outcome')).toBeNull();
  } finally {
   await page.close();child.stdin.write('stop\n');await closed;
   expect(child.exitCode).toBe(0);
   await finishWorkerLogs(child,info);
  }
 });
}
