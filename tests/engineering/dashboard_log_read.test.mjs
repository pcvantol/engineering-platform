import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { parse } from "acorn";

// Execute the actual production read function with declared unit-only boundaries.
// These controls grant no Server authority and do not replace installed proof.
const source = readFileSync(new URL("../../src/engineering_platform/assets/dashboard.js", import.meta.url), "utf8");
const actual = parse(source, { ecmaVersion: "latest", sourceType: "module" }).body.find(node => node.type === "FunctionDeclaration" && node.id.name === "refreshComponentLogs");
assert.ok(actual);
function harness(fetch, { normalize = value => value, render = () => {} } = {}) {
  const state = {
    componentLogsLoaded: false, componentLogVersion: "", componentLogRequestId: 0,
    componentLogEntries: { platform: [] }, componentLogTotals: {}, componentLogAvailableEvents: {}, componentLogServerPaged: false,
    fetch, componentLogRequestUrl: () => "declared-unit-only-log-read",
    normalizedComponentLogEntries: normalize, structuredLogEntries: value => [JSON.parse(value)],
    t: key => key, $: () => ({ hidden: true }), renderComponentLogs: render,
  };
  const read = runInNewContext(`${source.slice(actual.start, actual.end)}; refreshComponentLogs`, state);
  return { state, read };
}
const success = payload => ({ ok: true, json: async () => payload });

test("log availability contains fetch, nonOK and JSON decode failures only", async () => {
  for (const fetch of [async () => { throw Error("declared read rejection"); }, async () => ({ ok: false }), async () => ({ ok: true, json: async () => { throw SyntaxError("declared malformed JSON"); } })]) {
    const h = harness(fetch); await h.read();
    assert.equal(h.state.componentLogServerPaged, false);
    assert.equal(h.state.componentLogEntries.platform[0].event, "platform_log_unavailable");
  }
});

test("healthy log normalization and render faults retain the original reason", async () => {
  for (const boundary of ["normalize", "render"]) {
    const original = Error(`Declared ${boundary} sentinel`);
    const h = harness(async () => success({ entries: [{ event: "lifecycle_admission_decided" }], total: 1, events: ["lifecycle_admission_decided"] }), {
      [boundary]: () => { throw original; },
    });
    await assert.rejects(h.read(), error => error === original);
    assert.equal(h.state.componentLogServerPaged, boundary === "render");
    assert.ok(!h.state.componentLogEntries.platform.some(row => row.event === "platform_log_unavailable"));
  }
  const malformed = harness(async () => success(null));
  await assert.rejects(malformed.read(), /Cannot read properties of null/);
});

test("late healthy and failed log reads cannot replace the newer projection", async () => {
  for (const staleFails of [false, true]) {
    let settle, calls = 0;
    const pending = new Promise(resolve => { settle = resolve; });
    const h = harness(() => ++calls === 1 ? pending : Promise.resolve(success({ entries: [{ event: "newer" }], total: 1, events: ["newer"] })));
    const stale = h.read({ revision: 1 }); await h.read({ revision: 2 });
    settle(staleFails ? { ok: false } : success({ entries: [{ event: "older" }], total: 1, events: ["older"] })); await stale;
    assert.equal(h.state.componentLogEntries.platform[0].event, "newer");
    assert.equal(h.state.componentLogServerPaged, true);
  }
});
