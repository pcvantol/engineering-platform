export function normalizeDashboardStatus(status, fallback) {
  return status && typeof status === "object" && !Array.isArray(status)
    ? status
    : fallback;
}

export function normalizeDashboardSnapshot(snapshot) {
  return snapshot && typeof snapshot === "object" && !Array.isArray(snapshot)
    ? snapshot
    : {};
}

export function createDashboardStatusStore({ fallback, render }) {
  if (typeof render !== "function")
    throw new TypeError("A dashboard status renderer is required.");

  const store = {
    status: fallback,
    snapshot: {},
    update(status, snapshot = {}) {
      store.status = normalizeDashboardStatus(status, fallback);
      store.snapshot = normalizeDashboardSnapshot(snapshot);
      render(store.status, store.snapshot);
      return { status: store.status, snapshot: store.snapshot };
    },
  };
  return store;
}
