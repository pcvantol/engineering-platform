/* Passive Chromium observations: actual document and completed transport only. */
import { createHash } from 'node:crypto';
import { expect } from '@playwright/test';
import { appendFileSync, mkdirSync, mkdtempSync, writeFileSync, readFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { tmpdir } from 'node:os';
import path from 'node:path';

const observers = new WeakMap();
const header = (headers, name) => Object.entries(headers).find(([key]) => key.toLowerCase() === name.toLowerCase())?.[1] ?? null;

export function observeConsoleDocument(page) {
  if (!observers.has(page)) observers.set(page, createObservation(page));
  return observers.get(page);
}

async function createObservation(page) {
  const session = await page.context().newCDPSession(page), requests = new Map();
  const privateRoot = process.env.EP_BROWSER_PRIVATE_LOG_ROOT || tmpdir();
  mkdirSync(privateRoot, { recursive: true, mode: 0o700 });
  const retained = mkdtempSync(path.join(privateRoot, 'ep-document-observation-'));
  const raw = path.join(retained, 'transport.jsonl.gz');
  const pending = new Set();
  // Public Playwright request objects bind a held read to its actual lifecycle.
  // A completed first read cannot hide a different, still pending predecessor.
  const pendingSnapshotReads = new Set();
  const started = request => {
    if (new URL(request.url()).pathname === '/api/dashboard-snapshot') pendingSnapshotReads.add(request);
  };
  const finished = request => pendingSnapshotReads.delete(request);
  page.on('request', started);
  page.on('requestfinished', finished);
  page.on('requestfailed', finished);
  let resolveActualClose, rejectActualClose, sealing = null, sealed = false;
  const actualClose = new Promise((resolve, reject) => { resolveActualClose = resolve; rejectActualClose = reject; });
  // Closing the real page stops listeners. The fixture explicitly seals the
  // evidence only after its failure observations and other owned work end.
  actualClose.catch(() => {});
  const extras = new Map();
  const retain = (kind, fact) => {
    if (sealed) throw Error('Console observation write after evidence seal');
    appendFileSync(raw, gzipSync(Buffer.from(JSON.stringify({ at: Date.now(), kind, fact }) + '\n')), { mode: 0o600 });
  };
  const own = work => {
    if (sealing || sealed) return Promise.reject(Error('Console observation started after evidence seal request'));
    const task = work(); pending.add(task);
    task.then(() => pending.delete(task), () => pending.delete(task));
    return task;
  };
  retain('binding', { pid: process.pid, source_sha: process.env.EP_RUN_EVIDENCE_SOURCE_SHA || null,
    source_tree: process.env.EP_RUN_EVIDENCE_SOURCE_TREE || null, wheel_sha256: process.env.EP_RUN_EVIDENCE_WHEEL_SHA256 || null });
  await session.send('Network.enable'); await session.send('Page.enable');
  await session.send('Performance.enable');
  retain('initial-performance', await session.send('Performance.getMetrics'));
  const summary = (payload) => ({ scope: payload.scope, project_id: payload.project_id ?? null,
    version: payload.status?.platform_version, limited: payload.limited, reset_at: payload.reset_at,
    run_id: payload.history?.run_id ?? null, run_ids: Array.isArray(payload.runs) ? payload.runs.map(run => run.run_id) : undefined });
  session.on('Network.requestWillBeSent', (event) => {
    const url = new URL(event.request.url);
    if (!['/api/dashboard-snapshot', '/api/events', '/api/github-rate-limit', '/api/prompt-history', '/api/configuration', '/health'].includes(url.pathname)
        && !/^\/api\/prompt-history\/[^/]+\/details$/.test(url.pathname)) return;
    retain('request', event);
    for (const extra of extras.get(event.requestId) || []) retain('transport-extra-info', extra);
    extras.delete(event.requestId);
    requests.set(event.requestId, { request_id: event.requestId, document_id: event.loaderId,
      path: url.pathname, project_header: header(event.request.headers, 'X-Engineering-Platform-Project'),
      status: null, completed: false });
  });
  for (const name of ['Network.requestWillBeSentExtraInfo', 'Network.responseReceivedExtraInfo']) {
    session.on(name, event => {
      if (requests.has(event.requestId)) retain(name, event);
      else {
        if (!extras.has(event.requestId)) extras.set(event.requestId, []);
        extras.get(event.requestId).push({ name, event });
      }
    });
  }
  session.on('Network.responseReceived', (event) => {
    const request = requests.get(event.requestId); if (request) { request.status = event.response.status; retain('response', event); }
  });
  session.on('Network.loadingFailed', (event) => {
    const request = requests.get(event.requestId); if (request) { request.failure = event.errorText; retain('loading-failed', event); }
  });
  session.on('Network.loadingFinished', (event) => {
    const request = requests.get(event.requestId);
    if (!request || request.path === '/api/events') return;
    const work = (async () => {
    try {
      const response = await session.send('Network.getResponseBody', { requestId: event.requestId });
      const body = response.base64Encoded ? Buffer.from(response.body, 'base64').toString('utf8') : response.body;
      retain('body', { request_id: event.requestId, document_id: request.document_id,
        status: request.status, body, sha256: createHash('sha256').update(body).digest('hex') });
      request.response = summary(JSON.parse(body)); request.response_sha256 = createHash('sha256').update(body).digest('hex');
      request.completed = true;
    } catch (error) { request.failure = String(error); retain('body-failure', { request, error: String(error) }); }
    })();
    pending.add(work); work.then(() => pending.delete(work), () => pending.delete(work));
  });
  session.on('Network.eventSourceMessageReceived', (event) => {
    const request = requests.get(event.requestId);
    if (!request || !['dashboard', 'console-read-error'].includes(event.eventName)) return;
    retain('sse-frame', event);
    if (event.eventName !== 'dashboard') return;
    try {
      request.response = summary(JSON.parse(event.data));
      request.response_sha256 = createHash('sha256').update(event.data).digest('hex'); request.completed = true;
    } catch (error) { request.failure = String(error); }
  });
  const pageError = error => retain('page-error', { message: error.message, stack: error.stack });
  page.on('pageerror', pageError);
  page.on('close', () => {
    try {
      session.removeAllListeners();
      page.off('pageerror', pageError);
      page.off('request', started);
      page.off('requestfinished', finished);
      page.off('requestfailed', finished);
      retain('page-close', { actual_page_closed: true });
      resolveActualClose();
    } catch (error) { rejectActualClose(error); }
  });
  const closed = () => {
    if (!sealing) sealing = (async () => {
      await actualClose;
      while (pending.size) await Promise.all([...pending]);
      retain('observation-sealed', { actual_page_closed: true, owned_operations_ended: true });
      sealed = true;
      const bytes = readFileSync(raw);
      const receipt = { pid: process.pid, actual_page_closed: true,
        bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
      writeFileSync(path.join(retained, 'closed.json'), JSON.stringify(receipt), { mode: 0o600 });
      return receipt;
    })();
    return sealing;
  };
  const documentId = () => own(async () => {
    const frame = (await session.send('Page.getFrameTree')).frameTree.frame;
    retain('current-document', { frame_id: frame.id, document_id: frame.loaderId, url: frame.url });
    return frame.loaderId;
  });
  const completed = async (path) => {
    const id = await documentId(); return [...requests.values()].filter(r => r.document_id === id && r.path === path && r.completed && r.status === 200);
  };
  const scopeMatches = (response, project) => project === null
    ? response.scope === 'PLATFORM' && response.project_id === null
    : response.scope === 'PROJECT' && response.project_id === project;
  const scope = async (ingress, project) => {
    let applied;
    await expect.poll(async () => {
      const values = await completed(ingress === 'sse' ? '/api/events' : '/api/dashboard-snapshot');
      applied = values.find(r => scopeMatches(r.response, project)); return Boolean(applied);
    }).toBe(true);
    expect(await page.evaluate(() => window.ENGINEERING_PLATFORM_NO_PROJECT === true)).toBe(project === null);
    await expect(page.locator('body')).toHaveAttribute('data-project-id', project ?? 'none');
    await expect(page.locator('#dashboardSplash')).toBeHidden();
    await expect(page.locator('#platformVersion')).toHaveText(applied.response.version);
    await expect(page.locator('[data-run-evidence]')).toHaveCount(project === 'project' ? 3 : 0);
    expect(await documentId()).toBe(applied.document_id);
    return { ...applied, visible_version: await page.locator('#platformVersion').textContent(), visible_cards: await page.locator('[data-run-evidence]').count() };
  };
  const quota = async (expected) => {
    let response;
    await expect.poll(async () => { response = (await completed('/api/github-rate-limit')).at(-1); return Boolean(response); }).toBe(true);
    expect(response.response.limited).toBe(expected.limited);
    if (expected.reset_at) expect(response.response.reset_at).toBe(expected.reset_at);
    // This button is re-enabled in the actual parser/render finally block.
    await expect(page.locator('#githubRateLimitRefresh')).toBeEnabled();
    if (expected.limited) await expect(page.locator('#githubRateLimitBanner')).toBeVisible();
    else await expect(page.locator('#githubRateLimitBanner')).toBeHidden();
    expect(await documentId()).toBe(response.document_id);
    return { ...response, parser_and_visible_state_completed: true };
  };
  const select = async (project, ingress) => {
    const previous = await documentId();
    await Promise.all([page.waitForEvent('domcontentloaded'), page.locator('#dashboardProject').selectOption(project ?? '')]);
    expect(await documentId()).not.toBe(previous);
    const actual = new URL(page.url()).searchParams.get('project') || null; expect(actual).toBe(project);
    return scope(ingress, project);
  };
  const failure = (testInfo, caughtError = null) => own(async () => {
    // This synchronous marker precedes screenshot/CDP waits and canary cleanup.
    // A later screenshot never replaces the actual assertion timing boundary.
    retain('assertion-caught', { title: testInfo.title, url: page.url(),
      error: caughtError ? { message: String(caughtError), stack: caughtError.stack } : null,
      requests: [...requests.values()] });
    const results = await Promise.allSettled([
      page.screenshot({ path: path.join(retained, 'first-failure.png'), fullPage: true, animations: 'disabled' }),
      page.content(), documentId(), session.send('Performance.getMetrics'),
    ]);
    const facts = results.map((result, index) => result.status === 'fulfilled'
      ? { observation: index, status: 'retained', value: index === 0 ? createHash('sha256').update(result.value).digest('hex') : result.value }
      : { observation: index, status: 'UNAVAILABLE', reason: String(result.reason) });
    retain('first-failure', { title: testInfo.title, status: caughtError ? 'caught_failure' : testInfo.status,
      errors: caughtError ? [{ message: String(caughtError), stack: caughtError.stack }] : testInfo.errors,
      observations: facts, requests: [...requests.values()] });
    if (results.some(result => result.status === 'rejected'))
      throw Error('First browser failure retention incomplete; original failure remains in runner report');
  });
  return { scope, quota, select, documentId, failure, closed,
    privateFiles: () => ({ raw, receipt: path.join(retained, 'closed.json') }),
    records: () => [...requests.values()], pendingSnapshotReads: () => [...pendingSnapshotReads] };
}
