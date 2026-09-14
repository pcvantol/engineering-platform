import assert from "node:assert/strict";
import test from "node:test";
import { createDynamicEvidenceLocalizer, DYNAMIC_TRANSLATION_LIMITS as limits } from "../../src/engineering_platform/assets/dashboard_translation.mjs";

function row(source, format) {
  return { source, format, element: {
    dataset: {}, textContent: source, isConnected: true, hiddenAncestor: false,
    closest(selector) { return selector === "dialog" ? this.dialog || null : this.hiddenAncestor ? {} : null; },
    removeAttribute(name) {
      if (name === "data-translation-state") delete this.dataset.translationState;
      else delete this[name];
    },
  } };
}
const count = (text) => [...text].length;
function harness({ responder, now = () => 0, requestTimeoutMs } = {}) {
  let locale = "nl", active = 0, maxActive = 0;
  const requests = [];
  const localizer = createDynamicEvidenceLocalizer({
    getLocale: () => locale, sourceFallbackTitle: () => "Original evidence", now, requestTimeoutMs,
    fetch: async (url, options) => {
      const request = { url, ...options, payload: JSON.parse(options.body) };
      requests.push(request); active += 1; maxActive = Math.max(maxActive, active);
      try {
        if (responder) return await responder(request, requests.length);
        return { ok: true, json: async () => ({ translations: request.payload.texts.map((source) => `nl:${source}`) }) };
      } finally { active -= 1; }
    },
  });
  return { localizer, requests, setLocale: (value) => { locale = value; }, maxActive: () => maxActive };
}
function validRequests(requests) {
  for (const request of requests) {
    assert.ok(request.payload.texts.length <= 8);
    assert.ok(request.payload.texts.every((source) => count(source) <= 4096));
    assert.ok(request.payload.texts.reduce((sum, source) => sum + count(source), 0) <= 24000);
    assert.ok(Buffer.byteLength(request.body, "utf8") <= 32768, `body has ${Buffer.byteLength(request.body, "utf8")} bytes`);
  }
}

test("batches seven distinct 4000-codepoint texts within the real total limit", async () => {
  const { localizer, requests } = harness();
  const rows = Array.from({ length: 7 }, (_, index) => row(String(index) + "a".repeat(3999)));
  await localizer.localize(rows);
  validRequests(requests);
  assert.deepEqual(requests.map((request) => request.payload.texts.length), [6, 1]);
  assert.deepEqual(rows.map(({ element }) => element.textContent), rows.map(({ source }) => `nl:${source}`));
});

test("counts serialized UTF-8 bytes for four distinct 3000-character CJK texts", async () => {
  const { localizer, requests } = harness();
  const rows = Array.from({ length: 4 }, (_, index) => row(String(index) + "漢".repeat(2999)));
  await localizer.localize(rows);
  validRequests(requests);
  assert.equal(requests.length, 2);
});

test("uses Python codepoint lengths, retaining exact input and invalid-source fallback", async () => {
  const { localizer, requests } = harness({ responder: async ({ payload }) => ({ ok: true, json: async () => ({ translations: payload.texts.map((text) => `translated:${text.slice(0, 10)}`) }) }) });
  const sources = ["a".repeat(4096), "é".repeat(4096), "漢".repeat(4096), "😀".repeat(4096), "a".repeat(4097), "😀".repeat(4097), "literal \"quotes\" and \\\\ path\n".repeat(100)];
  const rows = sources.map((source) => row(source));
  await localizer.localize(rows);
  validRequests(requests);
  assert.deepEqual(requests.flatMap((request) => request.payload.texts), sources.filter((source) => count(source) <= 4096));
  for (const item of rows.filter(({ source }) => count(source) > 4096)) {
    assert.equal(item.element.textContent, item.source);
    assert.equal(item.element.dataset.translationState, "source");
  }
});

test("Python Unicode blanks fall back independently while BOM and valid evidence retain their bytes", async () => {
  const { localizer, requests } = harness();
  const blanks = ["", "\u0085", "\u001c", "\u001d", "\u001e", "\u001f", " \t\n\u00a0\u2000"];
  const valid = ["Usable scratch.", "\ufeff", "\u0085Evidence\u001c"];
  const rows = [...blanks, ...valid].map((source) => row(source));
  await localizer.localize(rows);
  assert.deepEqual(requests.flatMap((request) => request.payload.texts), valid);
  for (const item of rows.slice(0, blanks.length)) {
    assert.equal(item.element.textContent, item.source);
    assert.equal(item.element.dataset.translationState, "source");
  }
});

test("Python whitespace-only translated output is invalid, but BOM is not stripped", async () => {
  for (const output of ["\u0085", "\u001c", "\ufeff"]) {
    const { localizer } = harness({ responder: async () => ({ ok: true, json: async () => ({ translations: [output] }) }) });
    const item = row("Original evidence.");
    await localizer.localize([item]);
    assert.equal(item.element.textContent, output === "\ufeff" ? output : item.source);
  }
});

test("600 distinct visible rows remain translated with only two concurrent requests and a bounded cache", async () => {
  const { localizer, requests, maxActive } = harness({ responder: async ({ payload }) => {
    await new Promise((resolve) => setImmediate(resolve));
    return { ok: true, json: async () => ({ translations: payload.texts.map((source) => `nl:${source}`) }) };
  } });
  const rows = Array.from({ length: 600 }, (_, index) => row(`Evidence ${index}`));
  await localizer.localize(rows);
  assert.ok(maxActive() <= 2, `observed ${maxActive()} simultaneous fetches`);
  validRequests(requests);
  assert.equal(requests.length, 75);
  assert.deepEqual(rows.map(({ element }) => element.textContent), rows.map(({ source }) => `nl:${source}`));
  await localizer.localize([row("Evidence 0")]);
  assert.equal(requests.length, 76, "the oldest value was evicted from the bounded cache");
  await localizer.localize([row("Evidence 599")]);
  assert.equal(requests.length, 76, "a recent value remains cached");
});

test("accepts exactly 32768 serialized bytes and splits a one-byte overflow", async () => {
  const prefix = ["漢".repeat(4096), "字".repeat(4096)];
  const remaining = 32768 - Buffer.byteLength(JSON.stringify({ locale: "nl", texts: [...prefix, ""] }));
  const last = "界".repeat(Math.floor(remaining / 3)) + "a".repeat(remaining % 3);
  for (const extra of ["", "b"]) {
    const { localizer, requests } = harness();
    await localizer.localize([...prefix, last + extra].map((source) => row(source)));
    validRequests(requests);
    assert.equal(requests.length, extra ? 2 : 1);
    if (!extra) assert.equal(Buffer.byteLength(requests[0].body), 32768);
  }
});

test("preserves mapping with JSON escaping, total boundary and more than eight texts", async () => {
  const { localizer, requests } = harness();
  const sources = Array.from({ length: 6 }, (_, index) => String(index) + "a".repeat(3999));
  sources.push("b", ...Array.from({ length: 10 }, (_, index) => `${index}:\"\\\n\t\u0001`));
  const rows = sources.map((source) => row(source));
  await localizer.localize(rows);
  validRequests(requests);
  assert.equal(requests[0].payload.texts.reduce((total, text) => total + count(text), 0), 24000);
  assert.equal(requests[1].payload.texts.length, 8);
  assert.equal(requests[2].payload.texts.length, 3);
  assert.deepEqual(requests.flatMap((request) => request.payload.texts), sources);
  assert.deepEqual(rows.map(({ element }) => element.textContent), sources.map((source) => `nl:${source}`));
});

test("deduplicates simultaneous views and releases inflight after success", async () => {
  let release;
  const { localizer, requests } = harness({ responder: ({ payload }) => new Promise((resolve) => {
    release = () => resolve({ ok: true, json: async () => ({ translations: payload.texts.map((text) => `nl:${text}`) }) });
  }) });
  const first = row("Shared evidence"), second = row("Shared evidence");
  const one = localizer.localize([first]), two = localizer.localize([second]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 1);
  release(); await Promise.all([one, two]);
  assert.equal(first.element.textContent, "nl:Shared evidence");
  assert.equal(second.element.textContent, "nl:Shared evidence");
  await localizer.localize([row("Shared evidence")]);
  assert.equal(requests.length, 1);
});

test("keeps subscribers built before insertion and bounds pending work explicitly", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { localizer, requests, maxActive } = harness({ responder: async ({ payload }) => {
    await gate;
    return { ok: true, json: async () => ({ translations: payload.texts.map((source) => `nl:${source}`) }) };
  } });
  const detached = [row("Shared detached"), row("Shared detached")];
  detached.forEach(({ element }) => { element.isConnected = false; });
  const detachedWork = localizer.localize(detached);
  detached.forEach(({ element }) => { element.isConnected = true; });
  const rows = Array.from({ length: limits.pendingTexts + 20 }, (_, index) => row(`Pending ${index}`));
  const work = localizer.localize(rows);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 2);
  assert.equal(rows.filter(({ element }) => element.dataset.translationState === "source").length, 21);
  release(); await Promise.all([work, detachedWork]);
  assert.ok(maxActive() <= 2);
  assert.equal(requests.flatMap((request) => request.payload.texts).length, limits.pendingTexts);
  assert.deepEqual(detached.map(({ element }) => element.textContent), ["nl:Shared detached", "nl:Shared detached"]);
});

test("bounds subscribers without losing the first visible views", async () => {
  let release;
  const { localizer, requests } = harness({ responder: () => new Promise((resolve) => {
    release = () => resolve({ ok: true, json: async () => ({ translations: ["Translated shared"] }) });
  }) });
  const rows = Array.from({ length: limits.targetsPerText + 1 }, () => row("Shared"));
  const work = localizer.localize(rows);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 1); release(); await work;
  assert.ok(rows.slice(0, limits.targetsPerText).every(({ element }) => element.textContent === "Translated shared"));
  assert.equal(rows.at(-1).element.textContent, "Shared");
  assert.equal(rows.at(-1).element.dataset.translationState, "source");
});

for (const mode of ["http", "invalid_json", "cardinality", "blank", "type", "too_long", "abort", "timeout"]) {
  test(`releases ${mode} failures, limits rerender retries and permits an explicit retry`, async () => {
    let time = 0, healthy = false;
    const source = " Validation blocked. Expected: usable scratch. Observed: missing directory.\n";
    const { localizer, requests } = harness({ now: () => time, responder: async ({ payload }) => {
      if (healthy) return { ok: true, json: async () => ({ translations: payload.texts.map(() => "Translated. Expected: scratch.") }) };
      if (mode === "abort") throw new DOMException("Aborted", "AbortError");
      if (mode === "timeout") return { ok: false, status: 503, json: async () => ({ error: "DASHBOARD_TRANSLATION_TIMEOUT" }) };
      if (mode === "http") return { ok: false, status: 500 };
      if (mode === "invalid_json") return { ok: true, json: async () => { throw new SyntaxError("Invalid JSON"); } };
      const translations = { cardinality: [], blank: ["  "], type: [23], too_long: ["😀".repeat(6145)] }[mode];
      return { ok: true, json: async () => ({ translations }) };
    } });
    const formatting = (text) => text.replace(". Expected:", ".\nExpected:");
    const first = row(source, formatting);
    await localizer.localize([first]);
    assert.equal(first.element.textContent, source);
    assert.equal(first.element.dataset.translationState, "source");
    await localizer.localize([row(source)]);
    assert.equal(requests.length, 1);
    time = limits.retryDelayMs;
    await localizer.localize([row(source)]);
    assert.equal(requests.length, 2, "one automatic retry after the delay");
    time += limits.retryDelayMs * 10;
    await localizer.localize([row(source)]);
    assert.equal(requests.length, 2, "rerenders do not restart retries indefinitely");
    healthy = true; localizer.retry();
    const retry = row(source, formatting);
    await localizer.localize([retry]);
    assert.equal(requests.length, 3);
    assert.equal(retry.element.textContent, "Translated.\nExpected: scratch.");
    assert.equal(retry.element.dataset.translationState, undefined);
  });
}

test("caches only valid output and accepts exactly 6144 Python codepoints", async () => {
  const { localizer, requests } = harness({ responder: async () => ({ ok: true, json: async () => ({ translations: ["😀".repeat(6144)] }) }) });
  const target = row("Astral translation");
  await localizer.localize([target]);
  assert.equal(count(target.element.textContent), 6144);
  await localizer.localize([row("Astral translation")]);
  assert.equal(requests.length, 1);
});

test("old locale, changed evidence, disconnected or closed-modal responses cannot overwrite the view", async () => {
  const releases = [];
  const { localizer, requests, setLocale } = harness({ responder: (request) => new Promise((resolve) => {
    releases.push(() => resolve({ ok: true, json: async () => ({ translations: request.payload.texts.map((source) => `${request.payload.locale}:${source}`) }) }));
  }) });
  const live = row("Old source"), removed = row("Removed source"), closed = row("Closed source");
  const dutch = localizer.localize([live, removed, closed]);
  await new Promise((resolve) => setImmediate(resolve));
  setLocale("de"); localizer.retry();
  const german = localizer.localize([live]);
  await new Promise((resolve) => setImmediate(resolve));
  releases[1](); await german;
  assert.equal(live.element.textContent, "de:Old source");
  setLocale("en");
  live.source = "New source";
  await localizer.localize([live]);
  removed.element.isConnected = false; removed.element.textContent = "Removed view";
  closed.element.hiddenAncestor = true; closed.element.textContent = "Closed view";
  releases[0](); await dutch;
  assert.equal(live.element.textContent, "New source");
  assert.equal(removed.element.textContent, "Removed view");
  assert.equal(closed.element.textContent, "Closed view");
  assert.equal(requests.length, 2, "English never invokes the provider");
});

test("changed source in the same locale and closed-modal guards apply independently", async () => {
  const releases = [];
  const { localizer } = harness({ responder: ({ payload }) => new Promise((resolve) => {
    releases.push(() => resolve({ ok: true, json: async () => ({ translations: payload.texts.map((source) => `nl:${source}`) }) }));
  }) });
  const live = row("Old source"), removed = row("Removed source"), closed = row("Closed source");
  const old = localizer.localize([live, removed, closed]);
  await new Promise((resolve) => setImmediate(resolve));
  live.source = "New source";
  const newer = localizer.localize([live]);
  await new Promise((resolve) => setImmediate(resolve));
  removed.element.isConnected = false; removed.element.textContent = "Removed view";
  closed.element.hiddenAncestor = true; closed.element.textContent = "Closed view";
  releases[0](); await old;
  assert.equal(live.element.textContent, "New source");
  assert.equal(removed.element.textContent, "Removed view");
  assert.equal(closed.element.textContent, "Closed view");
  releases[1](); await newer;
  assert.equal(live.element.textContent, "nl:New source");
});

test("failure bookkeeping saturates safely and explicit retry frees it", async () => {
  const { localizer, requests } = harness({ responder: async () => ({ ok: false, status: 503 }) });
  for (let index = 0; index < limits.failureEntries; index += 128) {
    await localizer.localize(Array.from({ length: Math.min(128, limits.failureEntries - index) }, (_, offset) => row(`Failure ${index + offset}`)));
  }
  const countBefore = requests.length;
  const extra = row("New evidence at capacity");
  await localizer.localize([extra]);
  assert.equal(requests.length, countBefore);
  assert.equal(extra.element.textContent, extra.source);
  assert.equal(extra.element.dataset.translationState, "source");
  localizer.retry(); await localizer.localize([extra]);
  assert.equal(requests.length, countBefore + 1);
});

test("a native modal closed during the request retains its source projection", async () => {
  let release;
  const { localizer } = harness({ responder: () => new Promise((resolve) => {
    release = () => resolve({ ok: true, json: async () => ({ translations: ["Late translation"] }) });
  }) });
  const target = row("Modal evidence"); target.element.dialog = { open: true };
  const work = localizer.localize([target]);
  await new Promise((resolve) => setImmediate(resolve));
  target.element.dialog.open = false;
  release(); await work;
  assert.equal(target.element.textContent, "Modal evidence");
  target.element.dialog.open = true; localizer.retry();
  await localizer.localize([target]);
  assert.equal(target.element.textContent, "Late translation");
});

test("an enforced browser transport deadline releases hung requests for later explicit retry", async () => {
  let healthy = false, aborted = 0;
  const { localizer, requests } = harness({ requestTimeoutMs: 5, responder: ({ signal }) => {
    if (healthy) return { ok: true, json: async () => ({ translations: ["Later translation"] }) };
    return new Promise((resolve, reject) => signal.addEventListener("abort", () => {
      aborted += 1; reject(new DOMException("Deadline reached", "AbortError"));
    }, { once: true }));
  } });
  const target = row("Full original evidence");
  await localizer.localize([target]);
  assert.equal(aborted, 1);
  assert.equal(target.element.textContent, target.source);
  assert.equal(target.element.dataset.translationState, "source");
  healthy = true; localizer.retry();
  await localizer.localize([target]);
  assert.equal(requests.length, 2);
  assert.equal(target.element.textContent, "Later translation");
});

test("switching to English releases queued stale work without starting more provider requests", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { localizer, requests, setLocale } = harness({ responder: async ({ payload }) => {
    await gate;
    return { ok: true, json: async () => ({ translations: payload.texts.map((source) => `nl:${source}`) }) };
  } });
  const rows = Array.from({ length: 24 }, (_, index) => row(`Evidence ${index}`));
  const pending = localizer.localize(rows);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 2);
  setLocale("en"); await localizer.localize(rows);
  release(); await pending;
  assert.equal(requests.length, 2);
  assert.ok(rows.every(({ element, source }) => element.textContent === source));
});

test("cache eviction is exactly 256 successful entries and follows access recency", async () => {
  const { localizer, requests } = harness();
  await localizer.localize(Array.from({ length: limits.cacheEntries }, (_, index) => row(`LRU ${index}`)));
  const originalCount = requests.length;
  await localizer.localize([row("LRU 0")]);
  assert.equal(requests.length, originalCount);
  await localizer.localize([row("Additional evidence")]);
  await localizer.localize([row("LRU 0")]);
  assert.equal(requests.length, originalCount + 1, "recently read oldest entry is retained");
  await localizer.localize([row("LRU 1")]);
  assert.equal(requests.length, originalCount + 2, "least recently used entry is evicted at 257");
});
