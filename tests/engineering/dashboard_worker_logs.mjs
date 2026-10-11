/* Raw child output is retained independently of JSON receipts and UI assertions. */
import { openSync, writeSync, fsyncSync, closeSync, readFileSync, writeFileSync, mkdtempSync, mkdirSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import path from 'node:path';

const ownedLogs = new WeakMap();
const disconnectTypes = new Set(['BrokenPipeError', 'ConnectionResetError']);

function classify(stderr) {
  const unexpected = [], disconnects = [];
  for (const line of stderr.split('\n')) {
    if (line.includes('ep-canary-handler-error')) {
      try {
        const event = JSON.parse(line);
        if (event.event !== 'ep-canary-handler-error' || typeof event.exception_type !== 'string'
            || !/^[A-Za-z_][\w.]*$/.test(event.exception_type)
            || typeof event.client_disconnect !== 'boolean') throw new Error('invalid handler record');
        if (event.client_disconnect && disconnectTypes.has(event.exception_type)) disconnects.push(event);
        else unexpected.push(event);
      } catch { unexpected.push({ exception_type: 'malformed handler error record' }); }
    }
    // Standard Python tracebacks are emitted after the structured record.
    // Also detect unstructured errors from the legacy server independently.
    const terminal = /^([A-Za-z_][\w.]*(?:Error|Exception|Interrupt))(?::|$)/.exec(line);
    if (terminal) {
      const event = { exception_type: terminal[1], raw_terminal: line };
      if (disconnectTypes.has(terminal[1])) disconnects.push(event);
      else unexpected.push(event);
    }
  }
  const tracebacks = (stderr.match(/^Traceback \(most recent call last\):/gm) || []).length;
  const terminals = unexpected.filter(e => e.raw_terminal).length + disconnects.filter(e => e.raw_terminal).length;
  if (tracebacks > terminals) unexpected.push({ exception_type: 'incomplete or unclassified Python traceback' });
  return { unexpected, disconnects };
}

export function retainWorkerLogs(child, testInfo, label, { privateRoot = process.env.EP_BROWSER_PRIVATE_LOG_ROOT || tmpdir() } = {}) {
  if (ownedLogs.has(child)) throw new Error('Worker log collector already attached');
  const stem = `${label}-${child.pid ?? 'spawn-failed'}`;
  const publicReceiptPath = testInfo.outputPath(`${stem}-log-close.json`);
  mkdirSync(privateRoot, { recursive: true, mode: 0o700 });
  const publishedRoot = realpathSync(testInfo.project?.outputDir || path.dirname(publicReceiptPath));
  const relative = path.relative(publishedRoot, realpathSync(privateRoot));
  if (!relative || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))) {
    throw new Error('Private worker logs must be outside the published test result tree');
  }
  const privateDirectory = mkdtempSync(path.join(privateRoot, 'ep-browser-worker-'));
  // CI uploads test-results/**. Raw payloads and private paths must never be
  // written there or attached to the public Playwright report.
  const stderrPath = path.join(privateDirectory, 'stderr.log');
  const stdoutPath = path.join(privateDirectory, 'stdout.log');
  const receiptPath = path.join(privateDirectory, 'log-close.json');
  const handles = { stderr: openSync(stderrPath, 'wx', 0o600), stdout: openSync(stdoutPath, 'wx', 0o600) };
  const state = { stderrPath, stdoutPath, receiptPath, publicReceiptPath, label, pid: child.pid ?? null, started_at: Date.now(),
    errors: [], exited_at: null, closed_at: null, code: null, signal: null };
  for (const stream of ['stdout', 'stderr']) {
    child[stream].on('data', chunk => {
      try {
        // Write all bytes before allowing this observer to report progress.
        for (let offset = 0; offset < chunk.length;) offset += writeSync(handles[stream], chunk, offset);
        fsyncSync(handles[stream]);
      } catch (error) { state.errors.push(`${stream} retention: ${error.message}`); }
    });
    child[stream].on('error', error => state.errors.push(`${stream}: ${error.message}`));
  }
  child.on('error', error => state.errors.push(`worker: ${error.message}`));
  child.once('exit', (code, signal) => { state.exited_at = Date.now(); state.code = code; state.signal = signal; });
  state.closed = new Promise(resolve => child.once('close', (code, signal) => {
    state.closed_at = Date.now(); state.code = code; state.signal = signal;
    for (const handle of Object.values(handles)) {
      try { fsyncSync(handle); } catch (error) { state.errors.push(`log flush: ${error.message}`); }
      try { closeSync(handle); } catch (error) { state.errors.push(`log close: ${error.message}`); }
    }
    resolve();
  }));
  ownedLogs.set(child, state);
  return state;
}

export async function finishWorkerLogs(child, testInfo) {
  const state = ownedLogs.get(child);
  if (!state) throw new Error('Worker has no retained raw log collector');
  await state.closed; // Child close includes draining inherited stdout/stderr, unlike exit.
  const stderr = readFileSync(state.stderrPath), stdout = readFileSync(state.stdoutPath);
  const { unexpected, disconnects } = classify(stderr.toString('utf8'));
  const digest = (bytes, path) => ({ path, bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') });
  const receipt = { contract: 'ep-browser-worker-log-close/v1', label: state.label, pid: state.pid,
    closed: true, started_at: state.started_at, exited_at: state.exited_at, closed_at: state.closed_at,
    code: state.code, signal: state.signal, source_sha: process.env.EP_RUN_EVIDENCE_SOURCE_SHA || null,
    source_tree: process.env.EP_RUN_EVIDENCE_SOURCE_TREE || null, wheel_sha256: process.env.EP_RUN_EVIDENCE_WHEEL_SHA256 || null,
    stderr: digest(stderr, state.stderrPath), stdout: digest(stdout, state.stdoutPath),
    retention_errors: state.errors, unexpected_errors: unexpected, expected_disconnects: disconnects };
  writeFileSync(state.receiptPath, JSON.stringify(receipt, null, 2), { mode: 0o600 });
  const summary = { ...receipt,
    stderr: { bytes: receipt.stderr.bytes, sha256: receipt.stderr.sha256 },
    stdout: { bytes: receipt.stdout.bytes, sha256: receipt.stdout.sha256 },
    retention_errors: state.errors.length,
    unexpected_errors: unexpected.length, expected_disconnects: disconnects.length };
  writeFileSync(state.publicReceiptPath, JSON.stringify(summary, null, 2), { mode: 0o600 });
  await testInfo.attach(`${state.label}-log-close`, { path: state.publicReceiptPath, contentType: 'application/json' });
  if (state.errors.length || unexpected.length) throw new Error(`Worker log qualification failed: ${state.errors.length} retention errors; ${unexpected.length} unexpected errors: ${unexpected.map(e => e.exception_type).join('; ')}`);
  return summary;
}
