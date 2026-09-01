import assert from "node:assert/strict";
import test from "node:test";

import {
  createDashboardStatusStore,
  normalizeDashboardSnapshot,
  normalizeDashboardStatus,
} from "../../src/engineering_platform/assets/dashboard_status_store.mjs";
import {
  createLocaleService,
  normalizeLocale,
} from "../../src/engineering_platform/assets/dashboard_locales.mjs";

test("normalizes only object-shaped status and snapshots", () => {
  const fallback = { watcher_state: "DEGRADED" };

  assert.equal(normalizeDashboardStatus(null, fallback), fallback);
  assert.equal(normalizeDashboardStatus("invalid", fallback), fallback);
  assert.equal(normalizeDashboardStatus([], fallback), fallback);
  assert.deepEqual(normalizeDashboardStatus({ run_id: "run-1" }, fallback), {
    run_id: "run-1",
  });
  assert.deepEqual(normalizeDashboardSnapshot(null), {});
  assert.deepEqual(normalizeDashboardSnapshot("invalid"), {});
  assert.deepEqual(normalizeDashboardSnapshot([]), {});
  assert.deepEqual(normalizeDashboardSnapshot({ telemetry: [] }), {
    telemetry: [],
  });
});

test("stores the normalized snapshot before invoking one renderer", () => {
  const fallback = { watcher_state: "DEGRADED" },
    renders = [],
    store = createDashboardStatusStore({
      fallback,
      render: (status, snapshot) => renders.push({ status, snapshot }),
    });

  const result = store.update({ run_id: "run-1" }, { telemetry: [] });

  assert.equal(renders.length, 1);
  assert.deepEqual(result, {
    status: { run_id: "run-1" },
    snapshot: { telemetry: [] },
  });
  assert.deepEqual(store, {
    status: { run_id: "run-1" },
    snapshot: { telemetry: [] },
    update: store.update,
  });
});

test("keeps the latest revision when dashboard snapshots arrive out of order", () => {
  const renders = [], store = createDashboardStatusStore({
    fallback: { watcher_state: "DEGRADED" },
    render: (status, snapshot) => renders.push({ status, snapshot }),
  });

  store.update({ lifecycle: { current_step: "WAIT_FOR_OPERATOR_MERGE" } }, {
    snapshot_source: "dashboard-instance-a", snapshot_revision: 8,
  });
  store.update({ lifecycle: { current_step: "REPAIR_AGENT" } }, {
    snapshot_source: "dashboard-instance-a", snapshot_revision: 7,
  });

  assert.equal(renders.length, 1);
  assert.equal(store.status.lifecycle.current_step, "WAIT_FOR_OPERATOR_MERGE");
  assert.equal(store.snapshot.snapshot_revision, 8);
});

test("accepts a fresh dashboard process after restart", () => {
  const renders = [], store = createDashboardStatusStore({
    fallback: { watcher_state: "DEGRADED" },
    render: (status) => renders.push(status),
  });

  store.update({ current_phase: "WAIT_FOR_OPERATOR_MERGE" }, {
    snapshot_source: "dashboard-instance-a", snapshot_revision: 8,
  });
  store.update({ current_phase: "FINALIZE_AGENT" }, {
    snapshot_source: "dashboard-instance-b", snapshot_revision: 1,
  });

  assert.equal(renders.length, 2);
  assert.equal(store.status.current_phase, "FINALIZE_AGENT");
});

test("requires one renderer and falls back safely on malformed updates", () => {
  assert.throws(
    () => createDashboardStatusStore({ fallback: {}, render: null }),
    /renderer is required/,
  );

  const fallback = { watcher_state: "DEGRADED" },
    rendered = [],
    store = createDashboardStatusStore({
      fallback,
      render: (status, snapshot) => rendered.push([status, snapshot]),
    });
  store.update([], []);

  assert.deepEqual(rendered, [[fallback, {}]]);
});

test("centralizes dashboard language, formatting and collation", () => {
  for (const language of ["en", "nl", "de", "fr", "es"]) {
    const locale = createLocaleService(language);
    assert.equal(locale.language, language);
    assert.notEqual(locale.dateTime(new Date("2026-08-03T19:40:44Z")), "");
    assert.notEqual(locale.logDateTime(new Date("2026-08-03T19:40:44Z")), "");
    assert.notEqual(locale.number(0.3, { maximumFractionDigits: 1 }), "");
    assert.equal(typeof locale.compare("agent-2", "agent-10"), "number");
    assert.equal(locale.lower("ÉVÉNEMENT"), "événement");
  }
  assert.equal(normalizeLocale("nl-NL"), "nl");
  assert.equal(normalizeLocale("unsupported"), "en");
});
