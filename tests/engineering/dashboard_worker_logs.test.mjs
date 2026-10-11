import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { retainWorkerLogs, finishWorkerLogs } from './dashboard_worker_logs.mjs';

function context(t) {
  const root = mkdtempSync(path.join(tmpdir(), 'ep-worker-log-regression-'));
  const children = [];
  t.after(async () => {
    for (const child of children) {
      if (child.exitCode === null && child.signalCode === null && child.pid) {
        const closed = once(child, 'close');
        child.kill('SIGTERM');
        await closed;
      }
    }
    rmSync(root, { recursive: true, force: true });
  });
  return { root, outputPath(name) { mkdirSync(root, { recursive: true }); return path.join(root, name); },
    own(child) { children.push(child); return child; },
    attachments: [], async attach(name, data) { this.attachments.push({ name, ...data }); } };
}

test('retains actual bytes before stop and rejects a handler error after the ordinary receipt', async t => {
  const info = context(t);
  const child = spawn(process.execPath, ['-e', `
    process.stderr.write('before receipt\\n');
    process.stdout.write('{"ordinary_handlers_completed":true}\\n');
    process.stdin.once('data', () => {
      process.stderr.write(JSON.stringify({event:'ep-canary-handler-error',path:'/api/events',exception_type:'RuntimeError',client_disconnect:false})+'\\n');
      process.stderr.end('RuntimeError: late SSE failure\\n'); process.stdin.destroy();
    });`], { stdio: ['pipe', 'pipe', 'pipe'] });
  info.own(child);
  const logs = retainWorkerLogs(child, info, 'late-handler');
  await Promise.all([once(child.stdout, 'data'), once(child.stderr, 'data')]);
  assert.equal(readFileSync(logs.stderrPath, 'utf8'), 'before receipt\n');
  child.stdin.end('stop\n');
  await assert.rejects(finishWorkerLogs(child, info), /RuntimeError/);
  const receipt = JSON.parse(readFileSync(logs.receiptPath));
  assert.equal(receipt.closed, true);
  assert.equal(receipt.code, 0);
  assert.equal(receipt.unexpected_errors.length, 2);
  assert.match(readFileSync(logs.stderrPath, 'utf8'), /late SSE failure/);
  assert.equal(info.attachments.length, 1);
  assert.equal(JSON.parse(readFileSync(logs.publicReceiptPath)).unexpected_errors, 2);
});

test('waits past leader exit for inherited stderr to close and preserves its final bytes', async t => {
  const info = context(t);
  const child = spawn(process.execPath, ['-e', `
    require('node:child_process').spawn(process.execPath, ['-e', "setTimeout(()=>process.stderr.write('after leader exit\\\\n'),150)"], {stdio:['ignore','ignore',2]}).unref();
    process.exit(0);`], { stdio: ['ignore', 'pipe', 'pipe'] });
  info.own(child);
  const logs = retainWorkerLogs(child, info, 'inherited-stream');
  await once(child, 'exit');
  const receipt = await finishWorkerLogs(child, info);
  assert.equal(readFileSync(logs.stderrPath, 'utf8'), 'after leader exit\n');
  assert.ok(receipt.closed_at >= receipt.exited_at);
  assert.equal(receipt.stderr.bytes, Buffer.byteLength('after leader exit\n'));
});

for (const unexpected of [false, true]) {
  test(`expected disconnect stays distinct from an additional ordinary error: ${unexpected}`, async t => {
    const info = context(t);
    const child = spawn(process.execPath, ['-e', `
      process.stderr.write(JSON.stringify({event:'ep-canary-handler-error',path:'/api/events',exception_type:'BrokenPipeError',client_disconnect:true})+'\\n');
      process.stderr.write('Traceback (most recent call last):\\n  File "owned.py", line 1\\nBrokenPipeError: disconnected\\n');
      ${unexpected ? "process.stderr.write(JSON.stringify({event:'ep-canary-handler-error',path:'/api/status',exception_type:'ValueError',client_disconnect:true})+'\\n');" : ''}`], { stdio: ['ignore', 'pipe', 'pipe'] });
    info.own(child);
    const logs = retainWorkerLogs(child, info, 'disconnect');
    if (unexpected) await assert.rejects(finishWorkerLogs(child, info), /ValueError/);
    else assert.equal((await finishWorkerLogs(child, info)).expected_disconnects, 2);
    assert.match(readFileSync(logs.stderrPath, 'utf8'), /BrokenPipeError/);
  });
}

test('unstructured Python traceback and malformed handler records cannot silently qualify', async t => {
  const info = context(t);
  const child = spawn(process.execPath, ['-e', `process.stderr.write('Traceback (most recent call last):\\n  File "owned.py", line 1\\nLookupError: ordinary failure\\n{"event":"ep-canary-handler-error",bad}\\n');`], { stdio: ['ignore', 'pipe', 'pipe'] });
  info.own(child);
  retainWorkerLogs(child, info, 'unstructured');
  await assert.rejects(finishWorkerLogs(child, info), /LookupError.*malformed/s);
});

test('spawn failure retains evidence and rejects instead of issuing a successful close', async t => {
  const info = context(t);
  const child = spawn(path.join(info.root, 'absent-worker'), [], { stdio: ['ignore', 'pipe', 'pipe'] });
  info.own(child);
  const logs = retainWorkerLogs(child, info, 'spawn-failure');
  await assert.rejects(finishWorkerLogs(child, info), /1 retention errors/);
  assert.match(JSON.parse(readFileSync(logs.receiptPath)).retention_errors[0], /ENOENT/);
  assert.equal(JSON.parse(readFileSync(logs.receiptPath)).closed, true);
});

test('CI result tree contains only sanitized summaries, never raw payloads or private paths', async t => {
  const info = context(t), privateRoot = path.join(info.root, 'private'), publicRoot = path.join(info.root, 'test-results');
  mkdirSync(publicRoot);
  info.outputPath = name => path.join(publicRoot, name);
  const child = info.own(spawn(process.execPath, ['-e', "process.stdout.write('PRIVATE_STORED_PAYLOAD'); process.stderr.write('PRIVATE_DIAGNOSTIC_PAYLOAD');"],
    { stdio: ['ignore', 'pipe', 'pipe'] }));
  const logs = retainWorkerLogs(child, info, 'private', { privateRoot });
  const summary = await finishWorkerLogs(child, info);
  assert.match(readFileSync(logs.stdoutPath, 'utf8'), /PRIVATE_STORED_PAYLOAD/);
  assert.match(readFileSync(logs.stderrPath, 'utf8'), /PRIVATE_DIAGNOSTIC_PAYLOAD/);
  assert.equal(info.attachments.length, 1);
  assert.equal(info.attachments[0].path, logs.publicReceiptPath);
  assert.deepEqual(readdirSync(publicRoot), [path.basename(logs.publicReceiptPath)]);
  const uploaded = readFileSync(logs.publicReceiptPath, 'utf8');
  assert.ok(!uploaded.includes('PRIVATE_') && !uploaded.includes(privateRoot));
  assert.equal(summary.stdout.path, undefined);
  assert.equal(summary.stderr.path, undefined);
});

test('a private-log override inside the published result tree is refused', async t => {
  const info = context(t);
  const child = info.own(spawn(process.execPath, ['-e', 'process.stdin.resume()'], { stdio: ['pipe', 'pipe', 'pipe'] }));
  assert.throws(() => retainWorkerLogs(child, info, 'unsafe-root', { privateRoot: path.join(info.root, 'raw') }), /outside the published/);
});
