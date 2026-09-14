import { SUPPORTED_LOCALES } from "./dashboard_locales.mjs";

// Keep these presentation bounds aligned with dashboard_translation.py and the
// selected-project HTTP route. Python len(str) counts Unicode code points;
// String.length counts UTF-16 code units and rejects valid non-BMP evidence.
export const DYNAMIC_TRANSLATION_LIMITS = Object.freeze({
  texts: 8, textCodepoints: 4096, totalCodepoints: 24000, bodyBytes: 32768,
  translationCodepoints: 6144, cacheEntries: 256, concurrentRequests: 2,
  pendingTexts: 640, targetsPerText: 16, failureEntries: 1024,
  automaticAttempts: 2, retryDelayMs: 15000, requestTimeoutMs: 80000,
});
const limits = DYNAMIC_TRANSLATION_LIMITS;
const supportedLocales = new Set(SUPPORTED_LOCALES);
const encoder = new TextEncoder();
const codepoints = (text) => [...text].length;
// Python str.strip() includes these four information separators and NEL,
// unlike JS trim(); BOM is nonblank in Python. Do not normalize the source.
const blank = (text) => /^[\p{White_Space}\u001c-\u001f]*$/u.test(text);
const bodyFor = (locale, texts) => JSON.stringify({ locale, texts });
const fitsBody = (body) => encoder.encode(body).byteLength <= limits.bodyBytes;

export function createDynamicEvidenceLocalizer({
  getLocale, sourceFallbackTitle, fetch: transport = globalThis.fetch,
  now = () => Date.now(), requestTimeoutMs = limits.requestTimeoutMs,
}) {
  // LRU successes and retry metadata have independent bounds. Failure entries
  // are never evicted automatically: eviction must not restart a failed request
  // indefinitely as status updates rerender the same evidence. Saturation falls
  // back to source until an explicit locale change or modal reopening retries.
  const cache = new Map(), failures = new Map(), inflight = new Map();
  const targets = new WeakMap(), pending = [];
  let active = 0, pumpScheduled = false, retryEpoch = 0;

  function current(target) {
    const dialog = target.element.closest?.("dialog");
    return getLocale() === target.locale && target.element.isConnected
      && targets.get(target.element) === target
      && target.element.dataset.dynamicEvidenceSource === target.source
      && (!dialog || dialog.open)
      && !target.element.closest?.('[hidden], [aria-hidden="true"]');
  }
  function project(target, translation, initial = false) {
    if (!initial && !current(target)) return;
    if (translation !== undefined) {
      target.element.textContent = target.format ? target.format(translation) : translation;
      target.element.removeAttribute("data-translation-state");
      target.element.removeAttribute("title");
    } else {
      // Failed diagnostics retain every original character, rather than only a
      // previously formatted or translated projection of the source evidence.
      target.element.textContent = target.source;
      target.element.dataset.translationState = "source";
      target.element.title = sourceFallbackTitle();
    }
  }
  function cached(key) {
    const value = cache.get(key);
    if (value !== undefined) { cache.delete(key); cache.set(key, value); }
    return value;
  }
  function remember(key, value) {
    cache.delete(key); cache.set(key, value);
    while (cache.size > limits.cacheEntries) cache.delete(cache.keys().next().value);
  }
  function schedulePump() {
    if (pumpScheduled) return;
    pumpScheduled = true;
    queueMicrotask(() => { pumpScheduled = false; pump(); });
  }
  function takeBatch() {
    const batch = [], texts = [];
    const locale = pending[0].locale;
    let total = 0;
    while (pending.length && batch.length < limits.texts) {
      const entry = pending[0];
      if (entry.locale !== locale || total + entry.length > limits.totalCodepoints
          || !fitsBody(bodyFor(locale, [...texts, entry.source]))) break;
      pending.shift(); entry.active = true;
      batch.push(entry); texts.push(entry.source); total += entry.length;
    }
    return { batch, body: bodyFor(locale, texts) };
  }
  async function request({ batch, body }) {
    let translations;
    const controller = new AbortController();
    // The server separately enforces its provider deadline. This extra browser
    // bound releases presentation work if a connection/body read stops making
    // progress after the server invocation has already finished.
    const timeout = setTimeout(() => controller.abort(), Math.min(limits.requestTimeoutMs, requestTimeoutMs));
    try {
      const response = await transport("/api/dashboard-translate", {
        method: "POST", headers: { "Content-Type": "application/json" }, body, signal: controller.signal,
      });
      const payload = response.ok ? await response.json() : null;
      if (!Array.isArray(payload?.translations) || payload.translations.length !== batch.length
          || payload.translations.some((value) => typeof value !== "string" || blank(value)
            || codepoints(value) > limits.translationCodepoints)) throw Error("translation_unavailable");
      translations = payload.translations;
    } catch {
      // HTTP failures, malformed responses, deadline responses and browser aborts
      // preserve source. Only validated provider output enters the cache.
    } finally {
      clearTimeout(timeout);
      batch.forEach((entry, index) => {
        const value = translations?.[index];
        if (value !== undefined) {
          remember(entry.key, value);
          if (failures.get(entry.key) === entry.failure) failures.delete(entry.key);
        } else if (entry.epoch === retryEpoch && failures.get(entry.key) === entry.failure) {
          entry.failure.retryAfter = now() + limits.retryDelayMs;
        }
        // Publish from this response, before later batches can evict the cache
        // entry. An inventory larger than the cache still updates every row.
        for (const target of entry.targets.values()) project(target, value);
        if (inflight.get(entry.key) === entry) inflight.delete(entry.key);
        entry.resolve();
      });
      active -= 1;
      schedulePump();
    }
  }
  function pump() {
    while (active < limits.concurrentRequests && pending.length) {
      if (pending[0].locale !== getLocale()) {
        const entry = pending.shift();
        if (inflight.get(entry.key) === entry) inflight.delete(entry.key);
        if (failures.get(entry.key) === entry.failure) failures.delete(entry.key);
        entry.resolve();
        continue;
      }
      const batch = takeBatch();
      active += 1;
      void request(batch);
    }
  }
  function subscribe(entry, target) {
    // Rerenders replace their old DOM targets. Prune them before counting the
    // fixed subscriber bound, so normal status updates do not accumulate work.
    for (const [element, previous] of entry.targets) {
      if (targets.get(element) !== previous || (entry.active && !current(previous))) entry.targets.delete(element);
    }
    if (!entry.targets.has(target.element) && entry.targets.size >= limits.targetsPerText) {
      project(target, undefined, true);
      return;
    }
    entry.targets.set(target.element, target);
  }
  async function localize(rows) {
    const locale = getLocale(), completions = new Set();
    for (const row of rows) {
      const source = String(row.source ?? "");
      const target = { ...row, source, locale };
      targets.set(target.element, target);
      target.element.dataset.dynamicEvidenceSource = source;
      // Source is immediately available, including detached rows the caller
      // inserts after building a modal. This also restores English locally.
      project(target, source, true);
      if (locale === "en") continue;
      const key = `${locale}\u0000${source}`, length = codepoints(source);
      if (!supportedLocales.has(locale) || blank(source) || length > limits.textCodepoints
          || !fitsBody(bodyFor(locale, [source]))) {
        project(target, undefined, true); continue;
      }
      const hit = cached(key);
      if (hit !== undefined) { project(target, hit, true); continue; }
      const existing = inflight.get(key);
      if (existing) { subscribe(existing, target); completions.add(existing.completion); continue; }
      const previous = failures.get(key);
      if (pending.length >= limits.pendingTexts
          || (previous && (previous.attempts >= limits.automaticAttempts || now() < previous.retryAfter))
          || (!previous && failures.size >= limits.failureEntries)) {
        project(target, undefined, true); continue;
      }
      const failure = previous || { attempts: 0, retryAfter: 0 };
      failure.attempts += 1; failures.set(key, failure);
      let resolve;
      const completion = new Promise((done) => { resolve = done; });
      const entry = { key, source, locale, length, failure, epoch: retryEpoch,
        targets: new Map([[target.element, target]]), completion, resolve };
      inflight.set(key, entry); pending.push(entry); completions.add(completion);
    }
    schedulePump();
    await Promise.all(completions);
  }
  function retry() {
    retryEpoch += 1;
    failures.clear();
    // Existing work keeps its identity and is still deduplicated. Its eventual
    // cleanup cannot remove state belonging to a newer attempt.
  }
  return { localize, retry };
}
