/* Passive Chromium observations: actual document and completed transport only. */
import { createHash } from 'node:crypto';
import { expect } from '@playwright/test';

export async function observeConsoleDocument(page) {
  const session = await page.context().newCDPSession(page), requests = new Map();
  await session.send('Network.enable'); await session.send('Page.enable');
  const summary = (payload) => ({ scope: payload.scope, project_id: payload.project_id ?? null,
    version: payload.status?.platform_version, limited: payload.limited, reset_at: payload.reset_at });
  session.on('Network.requestWillBeSent', (event) => {
    const url = new URL(event.request.url);
    if (!['/api/dashboard-snapshot', '/api/events', '/api/github-rate-limit'].includes(url.pathname)) return;
    requests.set(event.requestId, { request_id: event.requestId, document_id: event.loaderId,
      path: url.pathname, project_header: event.request.headers['X-Engineering-Platform-Project'] ?? null,
      status: null, completed: false });
  });
  session.on('Network.responseReceived', (event) => {
    const request = requests.get(event.requestId); if (request) request.status = event.response.status;
  });
  session.on('Network.loadingFailed', (event) => {
    const request = requests.get(event.requestId); if (request) request.failure = event.errorText;
  });
  session.on('Network.loadingFinished', async (event) => {
    const request = requests.get(event.requestId);
    if (!request || request.path === '/api/events') return;
    try {
      const response = await session.send('Network.getResponseBody', { requestId: event.requestId });
      const body = response.base64Encoded ? Buffer.from(response.body, 'base64').toString('utf8') : response.body;
      request.response = summary(JSON.parse(body)); request.response_sha256 = createHash('sha256').update(body).digest('hex');
      request.completed = true;
    } catch (error) { request.failure = String(error); }
  });
  session.on('Network.eventSourceMessageReceived', (event) => {
    const request = requests.get(event.requestId); if (!request || event.eventName !== 'dashboard') return;
    try {
      request.response = summary(JSON.parse(event.data));
      request.response_sha256 = createHash('sha256').update(event.data).digest('hex'); request.completed = true;
    } catch (error) { request.failure = String(error); }
  });
  const documentId = async () => (await session.send('Page.getFrameTree')).frameTree.frame.loaderId;
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
  return { scope, quota, select, documentId, records: () => [...requests.values()] };
}
