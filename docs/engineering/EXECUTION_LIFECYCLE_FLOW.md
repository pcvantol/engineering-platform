# Execution Lifecycle Flow

The Operations Console presents one read-only lifecycle flow for exactly one
Execution Run ID. It separates the canonical intended path for the run's
execution mode from the persisted actual progress recorded by the Execution
Host. It is not Repository Truth, Forge Mission state, telemetry or a browser
workflow controller.

The first `START` node is always neutral. Completed intermediate nodes are
turquoise with a checkmark, ordinary active nodes are turquoise with a
restrained pulse, and unreached nodes remain grey. An active implementation or
finalization merge that waits for an operator is orange: it is a warning that
external action is required, not ordinary execution activity. The dedicated
terminal node is green for `COMPLETE`, orange for `BLOCKED`, and red for
`FAILED`; operator dismissal and stale/reconciled lease handling never alter
that outcome. A repair count is a badge on the single repair node, never a
duplicated path. Skipped styling is used only when persisted lifecycle evidence
explicitly distinguishes it.

For example, an active run can show `START → INITIALIZE → IMPLEMENTATION`
with implementation active and later nodes pending. A successful run finishes
green, a blocked run orange, and a failed early run red while all later
intended steps remain grey. Historical runs use the same component without
animation. When old history lacks lifecycle events, the console says that
lifecycle detail is unavailable rather than inventing phase completion.

The flow is horizontal at every viewport size and is independently
horizontally scrollable on phones and desktop browsers, including Safari with
native momentum. The page and enclosing card must not gain horizontal
overflow. A connector is turquoise only when it leads to a reached active or
completed step; it is neutral grey when it leads to a future, pending or
blocked step. Active-node animation respects reduced-motion.

Lifecycle state and Execution Phase Telemetry are separate: timing may enrich
details but cannot establish progression. Retry children each have their own
Run-ID flow; resume displays only persisted evidence. The projection is scoped
to a run rather than a repository deployment, so it remains valid for future
Execution Projects, repository components, producers and a multi-repository
Engineering Action. Such an action still has one top-level flow per Run ID;
repository subflows remain out of scope.

For managed executions, reaching the pull-request hand-off is not merge
evidence. The Merge node becomes completed only after persisted finalization
evidence (or a successful terminal outcome). If required pull-request checks
fail and the Execution Host enters bounded validation repair, Merge is blocked
without a checkmark and the current action explicitly instructs the operator to
fix the pull-request checks. This keeps the visible lifecycle aligned with the
action that must happen next.
