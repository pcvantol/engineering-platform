import assert from "node:assert/strict";
import test from "node:test";

import {
  createDashboardStatusStore,
  normalizeDashboardSnapshot,
  normalizeDashboardStatus,
} from "../../tools/engineering/assets/dashboard_status_store.mjs";

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
