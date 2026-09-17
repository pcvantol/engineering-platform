# Forge Action dependency and EP admission boundary

**Status:** target Engineering Platform contract boundary; canonical when merged. Implementation and qualification remain separately governed.

## Parallel-runtime refinement — 2026-09-17

Increment `FORGE_EP_PARALLEL_ACTION_RUNTIME_V1` specifies the EP side of
multiple independent Actions from one Mission actually executing concurrently.
See [owning roadmap](../development/PARALLEL_ACTION_EXECUTION_V1_ROADMAP.md)
and [documentary DAG](../development/PARALLEL_ACTION_EXECUTION_V1_DAG.json).
All six added EP implementation/qualification nodes remain PLANNED; NO_BUMP.
Forge owns the companion `docs/architecture/PARALLEL_ACTION_RUNTIME_V1.md`
and shared PA-01..PA-26 qualification catalogue. This refines existing
[P-QUEUE/Agent delivery scheduling](AGENT_DELIVERY_SCHEDULING_ARCHITECTURE.md),
not a second scheduler or the native nested-subagent roadmap.

Observed source basis: EP `f45694d40b753d235feac8b803d364e1450cae26` and Forge
`03f9514e0f1e66a79ccb684c9fdbe51b28b88090`. Forge's `_OneActionProvider`,
single current-Action projection and loop-level target still limit that
composition. EP's existing parallel read-only capability reviews do not prove
parallel mutating Action execution. No new installed-runtime audit is claimed.

## Purpose

Engineering Platform must support a producer-supplied hard Engineering Action dependency graph without becoming the planner that invents that graph.

For Forge-produced work:

- Forge owns which Actions exist;
- Forge owns each Action's target repository/write scope;
- Forge owns hard logical `depends_on` decisions and predecessor evidence requirements;
- EP durably persists the immutable submitted dependency snapshot;
- EP enforces dependency eligibility during admission;
- EP independently enforces repository/resource exclusion, Agent capability and execution capacity.

`FORGE_OWNS_ENGINEERING_DEPENDENCY_REASONING = TRUE`
`EP_OWNS_DEPENDENCY_ADMISSION_ENFORCEMENT = TRUE`
`EP_DOES_NOT_INVENT_FORGE_DEPENDENCIES = TRUE`

## Three independent gates

An Action can wait for three fundamentally different reasons:

1. **Hard logical dependency.** The producer-supplied predecessor evidence is not yet satisfied.
2. **Execution resource/lease.** The plan permits execution, but the required repository/resource is exclusively leased or otherwise unsafe.
3. **Qualified execution capacity.** The plan and resource state permit execution, but no authorized Agent/provider capacity can accept it yet.

These states must remain distinguishable in CENTRAL evidence and projections. Priority must never bypass any of them. Current authorization and compatible target/profile capability are also required; missing authority is not disguised as capacity waiting. Preserve all relevant blocker reasons with a deterministic primary reason if a legacy UI needs one.

```text
producer Action is logically eligible?
        |
        +-- no --> WAITING_DEPENDENCY
        |
        v
required repository/resource available?
        |
        +-- no --> WAITING_RESOURCE / WAITING_LEASE
        |
        v
qualified Agent/provider capacity available?
        |
        +-- no --> WAITING_CAPACITY
        |
        v
admit / place / execute
```

## Immutable dependency snapshot

The submitted Action contract must be able to bind at least:

```text
Action identity/revision
Mission/Intent provenance supplied by producer
target project/repository
bounded write scope
depends_on predecessor Action identities
required predecessor outcome
dependency evidence requirements/references
producer correlation/idempotency identity
```

EP validates and stores the dependency snapshot with the canonical submission/run lineage. A producer retry or transport recovery must not silently change predecessor requirements under the same idempotency identity.

EP may reject malformed or contradictory dependency input. That validation is not planning.

The parallel implementation must additionally bind the source Mission/graph
revision, target baseline/Truth reference, effective execution policy and
supported concurrency profile through the existing versioned contract family.
Exact field names/schema versions are implementation work. Unknown required
parallel semantics fail closed; they are never silently discarded by a legacy
single-run adapter. No arbitrary peer URL or credential is accepted from a
provider-generated Action. Each actual target requires its own verified
project/repository scope; the existing forge consumer does not automatically
authorize another repository.

## Evidence-gated predecessor satisfaction

A predecessor edge can require more than a generic COMPLETE flag. The Action contract may require concrete predecessor evidence such as:

- delivered repository revision;
- qualified artifact identity;
- artifact SHA-256;
- source revision;
- receipt/report identity;
- protocol/schema version evidence;
- qualification/provenance reference.

The successor becomes dependency-eligible only when the required canonical predecessor evidence is present and verifies against the submitted requirement.

A source merge alone is therefore not equivalent to publication of a qualified installable artifact when the dependency explicitly requires the artifact.

## Cross-repository dependencies

A hard dependency may cross repositories and products. Project or repository membership alone does not create a dependency.

Example:

```text
EP-A5: publish qualified EP Server + Project Agent artifacts
  evidence = artifact identities + SHA-256 + source revisions + qualification refs

FP-A3: finalize Forge Platform component manifest
  target_repository = forge-platform
  depends_on = [EP-A5, FP-A2-installer-support]
```

EP must keep `FP-A3` in `WAITING_DEPENDENCY` until the `EP-A5` evidence required by the submitted edge is satisfied, even if a Forge Platform Agent/lease is otherwise available.

## Parallel execution

When Forge supplies two Actions with no dependency between them, EP may admit them independently if resource and capacity rules allow. Different repositories are independently lockable and are the primary safe first case for mutating parallel execution.

Forge deciding that two Actions are independent does not force EP to run them concurrently. EP retains execution-safety and capacity authority.

Conversely, EP finding spare capacity must never override a Forge-supplied hard dependency.

For a qualified parallel profile with at least two available compatible slots,
EP must be able to execute independent targets concurrently: acceptance of two
requests or concurrent eligibility alone does not close this feature. No
Mission-ID/project-ID-wide execution mutex may serialize them merely because
they share a Mission. Ordinary serialization of brief CENTRAL transactions is
not serialization of full provider/validation/delivery lifetimes.

The first profile uses separate repositories/workspaces on one host and one
configured compatible execution service. It does not require a distributed
Agent fleet, Workspace, full universal installer or native subagent engine.
The relevant existing worker/provider/lease paths must nevertheless actually
support and be qualified for multiple deliveries, not just expose a limit flag.

Same-repository parallel mutation remains a separately qualified stricter case.
Until then, independent Actions sharing its mutable repository resource wait on
that resource even with distinct branch names. Different repositories can also
share ports, test databases, build outputs, signing identity, release namespace
or updater target; declare and enforce those real resources. Independent
read-only resources need not acquire a writer lease merely for matching a
project label. Resolve actual Git/common-worktree identity, not path strings
that can alias the same storage.

## Capacity, isolation and restart-safe ownership

Reuse existing CENTRAL admission, lifecycle worker, execution leases and
provider-invocation ledger. Do not introduce a shadow run database or let an
Agent invent acceptance. Admission/claim/lease fencing must prevent two workers
owning the same delivery after races or restart. Capacity reservation and release
are atomic/idempotent for the owning attempt; an uncertain process or stale
heartbeat is not proof resources are free. No automatic duplicate execution to
test whether an old one is alive.

Enforce effective global/host/provider/target limits and bounded backpressure.
Limits count actual applicable child invocations, not only parent Action rows;
provider-internal concurrency cannot multiply an allowance invisibly. Where
child capacity cannot be observed/enforced, do not advertise that unqualified
mode as bounded. Pending dependencies must not occupy scarce provider slots
needed by their predecessors; a blocked first queue item must not starve an
unrelated eligible target. Priority cannot bypass scope, predecessor evidence
or exclusive resources.

Each delivery owns its workspace, branch, subprocess/cancellation identity,
callbacks, validation state, candidate/reviewer evidence, output capture, usage
and timers. Avoid shared mutable provider-client result fields or process-global
cwd/environment mutation. Short-lived locks never span an entire unrelated
provider request or network wait. Shared helpers are permitted only where their
contracts are demonstrably concurrency-safe.

Quality/Security remains independent and candidate-bound for each Action. The
new profile does not require simultaneous mandatory reviews within an Action:
that is a separate qualification. It does require isolation across concurrently
running Actions. Existing provider-recovery and repair allowances retain attempt
lineage; releasing/reacquiring a slot does not reset them. Deterministic checks
and publication may reuse existing qualified adapters, but this documentation
does not reopen the separate post-assurance-publication repair or SA programme.

## Relationship to Agent local scheduling

CENTRAL decides logical eligibility and placement before an Agent owns delivery. An Agent may schedule already accepted independent work within its local resource policy, but may not execute an Action that CENTRAL has not admitted or bypass a hard predecessor.

Agent-local retries, validation repair and branch reconciliation remain execution attempts for the same admitted Action; they do not create new Forge dependency nodes.

An Action is not a provider subagent. Internal delegation remains within the
admitted target/effect scope and qualified provider profile. No second target
may be hidden in a child prompt. Conversely every helper does not need a new
Forge Mission/Action or full PR/finalization transaction. Native nested-agent
implementation is not delivered or newly authorized by this inter-Action design.

## Dynamic Forge replanning

EP does not need the whole future Living Mission Graph. It receives immutable materialized Action snapshots. Forge may reconcile completed EP evidence and then derive new successor Actions or changed future dependencies.

EP therefore sees a durable stream of immutable submitted Actions rather than a mutable planning database shared with Forge.

```text
Forge mutable future plan
  -> immutable Action A submission
  -> EP A terminal evidence
  -> Forge replan
  -> immutable Action B/B2/C submission(s)
```

No cross-product direct SQL or shared graph store is introduced.

Deliver each terminal result/readback independently. A finished A must become
available to Forge while B still runs; do not require a global batch/wave
completion. A-only successor C may execute after the required A proof. A joint
qualifier Q depending on A and B waits for both exact requirements. This is
producer-owned fan-in, not EP inventing a plan or automatically submitting Q.

A new graph revision never mutates an admitted sibling's target/baseline or
expected output. Per-Action repository Truth must remain isolated. A real
invalidated read/dependency input is handled explicitly through the existing
failure/hold/recovery boundary; harmless progress in another repository is not
an input conflict.

## Partial submission, failures and cancellation

A accepted/B timed-out is not an atomic failure of both. Readback uses each
persisted request identity and unchanged bytes; duplicate delivery/callback
identities do not create new runs or transfer evidence to the wrong Action.
Persist independent slot outcomes even when arrival order differs from submission
order. Historical retry parents belong to one Action, never to its successful
sibling merely because both share a Mission.

A failed predecessor blocks affected dependants without rewriting independent
successes. Continue, hold or cancel other work under the actual impact and
approved policy; uncertain scope/resource compromise is not ignored. Cancellation
is a request/acknowledged-effect protocol: don't report no-running-work until
providers and leases are safely reconciled. EP reports unresolved effects;
Forge cannot legitimately COMPLETE the Mission while an accepted writer or
uncertain cancellation remains. A Mission-wide pause/maintenance request must
account for all relevant deliveries, not one legacy current-run field.

## Readmodels, telemetry and efficiency

Expose all active/waiting/terminal Actions by exact producer Mission, Action,
request, attempt and target identity with policy/source-as-of evidence. Keep
technical delivery, host qualification and Forge Mission acceptance distinct.
Legacy singleton projections must explicitly indicate multiplicity rather than
hide all but the last run. Consumer readback remains scoped and secret-free.

Use the existing canonical timing/usage/export contracts. Sibling Actions form a
Mission collection, not a retry chain. Sum valid usage exactly once with coverage
and conflicts preserved; do not call EP totals complete Mission usage when Forge
planning is excluded. Show Action elapsed spans, overall elapsed/busy interval
union, overlap, dependency/resource/capacity waiting and actual provider totals.
Inclusive parallel durations are not additive wall time. The same snapshot's MD
and JSON contain the entire selected Action population and its available details.

A real overlap proof uses known comparable clocks/intervals and distinct active
provider/delivery identities; two RUNNING labels are insufficient. Compare serial
and parallel on the same bounded task, gates, policy and resource/provider limits.
Report observed time and complete/partial usage without fabricated speedup or
savings. Capacity-constrained serial execution can be correct operational behavior,
but cannot be presented as the successful concurrency canary.

## Qualification

The dependency-admission contract requires deterministic and end-to-end evidence for:

- hard predecessor blocks successor despite free capacity;
- dependency becomes eligible only after required terminal evidence exists;
- wrong predecessor/run/project/repository evidence is rejected;
- artifact requirement is not satisfied by source merge alone;
- changed dependency set under one idempotency identity is rejected;
- independent repository Actions can be simultaneously eligible;
- resource/lease wait remains distinct from dependency wait;
- capacity wait remains distinct from dependency/resource wait;
- restart/recovery preserves dependency identity and eligibility;
- priority cannot bypass hard dependencies;
- Agent local execution cannot self-admit blocked work.

The added PA-01..PA-26 catalogue in Forge further requires actual overlapping
execution; bounded resource/capacity fairness; invocation/result isolation;
partial-submit/crash/replay correctness; incremental A-only release while B runs;
proper A+B artifact joins; no premature Mission completion; scoped multi-active
readmodel/export parity; serial compatibility and honest efficiency measurement.
EP PA-EQ uses published Forge contract fixtures and need not wait for final Forge
PA-FQ. Forge's live integrated canary then consumes the qualified EP slice; no
cross-product qualification cycle or Workspace predecessor is introduced.

The first Forge dynamic inner-Mission canary may remain serial and does not require this full multi-repository parallel qualification. The cross-repository Forge canary requires this boundary together with EP's qualified multi-execution, repository lease and Agent/provider capacity capabilities.

This design change performs no runtime implementation, schema migration, active
policy change, process start, reset, credential operation, release or installation.
The existing reset-development streams and serial Mission-3 acceptance are not
expanded or reclassified by this document.
