/* Test-only ownership of held route callbacks; every reason remains fatal. */
export function createOwnedHeldRoutes() {
  let release, closing = false, closed = false, observedCount = 0;
  const released = new Promise(resolve => { release = resolve; });
  const callbacks = [], outcomes = [], removalErrors = [];
  const handler = route => {
    const task = closed
      ? Promise.reject(new Error("Own route callback invoked after owner closure"))
      : released.then(() => route.continue());
    // Observe immediately to prevent an unhandled process exit. Mandatory close
    // and verification rethrow original reasons after all owned work settles.
    const observation = task.then(() => ({ status: "fulfilled" }),
      reason => ({ status: "rejected", reason }));
    callbacks.push(observation);
    return observation.then(() => undefined);
  };
  const drain = async () => {
    while (observedCount < callbacks.length) {
      const batch = callbacks.slice(observedCount); observedCount = callbacks.length;
      outcomes.push(...await Promise.all(batch));
    }
  };
  const verdict = () => {
    const errors = [...outcomes.filter(result => result.status === "rejected").map(result => result.reason), ...removalErrors];
    const receipt = { started: callbacks.length, settled: outcomes.length,
      rejected: errors.length, actual_owner_closed: closed };
    if (errors.length) {
      const error = new AggregateError(errors, "Owned route callback/removal failures retained");
      error.receipt = receipt; throw error;
    }
    return receipt;
  };
  const close = async removeOwnRoute => {
    if (closing || closed) throw new Error("Own route owner closed more than once");
    closing = true; release();
    await drain();
    try { await removeOwnRoute(); } catch (error) { removalErrors.push(error); }
    await drain(); closed = true;
    return verdict();
  };
  const verifyClosed = async () => {
    if (!closed) throw new Error("Own route owner has not actually closed");
    await drain(); return verdict();
  };
  return { handler, release, close, verifyClosed };
}
