# EP parallel Action recovery V1 (PA-E3)

**Status:** SOURCE_FIXED. **Owner:** Engineering Platform. **Version:** NO_BUMP.

PA-E3 extends the opt-in PA-E2 repository and capacity reservation profile.
The Action intake, canonical submission, run ID, project and repository binding,
and PA-E1 predecessor decision remain the authority for every attempt. A
dispatcher may resume an existing run; it cannot create a second run or reset
the runner's persisted provider-repair budget through a duplicate submission.

## Process and checkpoint fence

Each PA-E2 dispatch must run in its own operating-system process. Before
preflight or provider work, it records an exact process identity and a unique
attempt in CENTRAL `ep_execution_leases`. An active owner excludes another
attempt. A dead owner may be replaced only if it has not entered the runner,
or if the runner synchronously returned, its owned provider process group was
observed empty, and the returned checkpoint was recorded. Each replacement
keeps the same run and Action identity and the existing repository reservation.

The runner-entry marker is committed immediately before `runner.run`. A crash
after that marker without a returned checkpoint has unknown external effects.
The installation records `PROVIDER_EFFECT_UNCERTAIN`, retains the repository
and provider slot, excludes the Action from worker dispatch, and exposes
`WAITING_RECOVERY` in producer readback. A stale PID, different process birth,
unavailable host, ambiguous process observation, changed repository identity,
or mismatched Action/run cannot grant execution. An operator dismiss or retry
cannot release an uncertain run. This first profile deliberately requires
separate, evidence-based recovery before such a hold can be resolved.

The two PA-E2 slots remain independent. An uncertain Action on repository A
holds its own slot and repository aliases; eligible work on repository B can
still use the other slot. Existing same-repository, origin-alias and legacy
project gates remain in force. A terminal Action cannot be dispatched again.

## Cancellation

The private, project-scoped Console `POST /api/execution-cancel` accepts only
`{"run_id":"..."}` for an active PA-E2 Action. It audits accepted and rejected
requests, enforces the selected project and a literal loopback or Tailnet
address with matching origin, and persists one idempotent `CANCEL_REQUESTED`
intent. An accepted Console request and its CENTRAL audit row commit together;
rejected-request logging follows the existing Console audit path. The Console shows the action only for a
qualifying active run. The intent is not a claim that the provider stopped.

Before any runner entry, the dispatcher can acknowledge cancellation without a
provider effect, mark the run failed and dismissed, and release both slot and
repository reservation. If an earlier attempt returned at a proved checkpoint,
cancellation before the next runner entry retains the repository for operator
review. After runner entry, the Codex client checks the
durable request, signals only its own provider process group, and verifies that
the group is empty. A process that ignores `SIGTERM` receives `SIGKILL` after a
bounded grace period. Only then is cancellation acknowledged and the provider
slot released. The run remains blocked with an open operator decision and its
repository remains exclusive while existing workspace, commit or PR effects
are reviewed. Explicit dismissal releases that repository reservation.
Terminal state, cancellation outcome, attempt closure and capacity/resource
release commit together in CENTRAL; a crash cannot expose a terminal run with
an unresolved provider slot.
Producer readback distinguishes `CANCEL_REQUESTED` from
`CANCEL_ACKNOWLEDGED` and reports whether the repository is still held.

This V1 cleanup proof covers the provider's owned process group. A descendant
that deliberately creates another session is outside that proof; qualifying
such a provider requires stronger containment before its effects can be
treated as stopped. The repository reservation stays held after a normal
post-entry cancellation for operator review.

If provider stop or process-group cleanup cannot be proved, the run is
`PROVIDER_EFFECT_UNCERTAIN` and both reservations stay held. If the runner
already completed before the request could take effect, the request is marked
`CANCEL_TOO_LATE`; no retroactive cancellation is inferred. A cancellation
exception is not a provider-turn interruption and does not spend or replenish
the bounded provider-repair allowance.

## Qualification boundary

Deterministic tests start new dispatcher processes at claim, runner-entry and
returned-checkpoint boundaries, then restart against the same CENTRAL and
assert the exact run, attempt, resource and capacity state. They also exercise
pre-runner and active cancellation, an actual owned provider process group,
cross-project refusal, and independent sibling progress. The HTTP route,
five-locale Console control, browser behavior, complete Python suite and
coverage gate are part of the protected source qualification. PA-EQ retains
the separate installed Forge-to-EP matrix.
