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

  let latestSnapshotSource = null, latestSnapshotRevision = -1;
  const store = {
    status: fallback,
    snapshot: {},
    update(status, snapshot = {}) {
      const nextStatus = normalizeDashboardStatus(status, fallback);
      const nextSnapshot = normalizeDashboardSnapshot(snapshot);
      const source = typeof nextSnapshot.snapshot_source === "string" ? nextSnapshot.snapshot_source : null;
      const revision = Number.isSafeInteger(nextSnapshot.snapshot_revision) && nextSnapshot.snapshot_revision >= 0
        ? nextSnapshot.snapshot_revision
        : null;
      if (source && revision !== null) {
        if (source === latestSnapshotSource && revision < latestSnapshotRevision)
          return { status: store.status, snapshot: store.snapshot };
        if (source !== latestSnapshotSource) latestSnapshotSource = source;
        latestSnapshotRevision = revision;
      }
      store.status = nextStatus;
      store.snapshot = nextSnapshot;
      render(store.status, store.snapshot);
      return { status: store.status, snapshot: store.snapshot };
    },
  };
  return store;
}
