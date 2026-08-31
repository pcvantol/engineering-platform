# Engineering Platform Architecture Handbook

**Status:** Stable execution architecture

**Platform status:** Engineering Platform 1.x `FEATURE_COMPLETE`

## Purpose

Engineering Platform is the stable, producer-neutral execution platform for
DJConnect engineering work. It accepts compliant Engineering Actions from a
Human Architect, Forge or a future Producer and applies one generic execution
architecture regardless of origin.

This handbook records the frozen boundary. It introduces no runtime capability,
Execution Host behavior or Forge implementation change.

## What Engineering Platform owns

- Engineering Action execution and the execution lifecycle.
- Execution qualification, Host Preflight, Workspace Preflight and Capability
  Preflight.
- Execution Evidence, immutable Execution Receipts and Engineering Reports.
- Generic execution telemetry, dashboard and Prompt History.
- Workspace authorization, host configuration resolution and Execution Host
  evolution.
- Producer-neutral execution semantics.

## What Engineering Platform never owns

- Mission Planning.
- Business Governance or Architecture Governance.
- Mission Recommendation.
- Decision Evidence.
- Runtime Planning or Runtime Instance concepts.
- Portfolio management.
- Forge implementation.

## Producer-neutral execution model

```text
Human Architect ─┐
Forge ──────────┼──> compliant Engineering Action
Future Producer ─┘             │
                                v
                    Engineering Platform Execution Host
                                │
                                v
       qualification → preflight → lifecycle → evidence → report / receipt
```

The action has identical admission, preflight, lifecycle, evidence and terminal
semantics for every Producer. Producer identity is immutable provenance only:
it supports traceability, audit and Execution Evidence. It does not alter
scheduling, reviewer selection, execution behavior or terminal outcomes.

## Forge boundary

Forge defines Mission, planning, Runtime Prompts, Decision Evidence and Runtime
Instance concepts. Engineering Platform executes the resulting Engineering
Actions and owns their execution lifecycle, Execution Evidence, Engineering
Reports and Execution Receipts.

No Engineering Platform capability may duplicate these Forge responsibilities.
An Engineering Report or Execution Receipt is execution evidence; it is not
Forge Decision Evidence and cannot become planning state.

## Home deployment profile: Forge, EP, Workspace and Workers

This is the canonical logical architecture for the intended home deployment.
It describes authority and deployment separately; it does **not** authorize
implementation work, an extraction phase, or an Increment 2 change.

```text
Workspace (human client)                         Repository / GitHub
        │                                                │
        │ planning / governance interaction              │ commit / branch / PR / merge truth
        v                                                v
Forge ── Engineering Action + canonical project_id ──> Engineering Platform
planning / governance truth                              admission / execution / evidence truth
                                                          │
                                                          │ capability-matched assignment
                                                          v
                                                   replaceable Worker(s)
                                                   Codex / Git / tests / builds
```

The initial home profile places one authoritative Forge installation, one
authoritative EP installation and one primary Worker on an always-on Mac mini.
That co-location is a deployment choice, not logical coupling or a product-wide
singleton invariant. Workspace is a client that can be installed elsewhere;
it may initiate and observe work, but owns no canonical planning or execution
state. A MacBook or later host may supply a Worker without acquiring Forge or
EP authority.

### Principles

- **Separate authorities.** Forge/Workspace own planning and governance truth;
  EP owns admission, execution lifecycle, leases, provider dispatch,
  validation, delivery evidence, qualification and reconciliation; repositories
  and GitHub own commit, branch, pull-request and merge truth. Workers execute
  only under EP authority.
- **Separate state machines.** Forge Mission state and EP Execution state are
  related through immutable Producer/Engineering-Action provenance, but neither
  is a projection or lifecycle transition of the other.
- **Canonical identity.** `project_id` is the opaque canonical identity supplied
  by Forge/Workspace to EP. EP treats it as a foreign identity and never mints,
  infers or translates it from a path, repository name or label.
- **Concurrency and leases.** The repository/execution scope is the concurrency
  boundary. At most one mutating execution may hold a scope at a time. Queue
  ordering defaults to FIFO, but selection is policy-driven. Its mutation lease
  remains held through delivery, finalization and reconciliation, not merely
  until a provider returns.
- **Replaceable Workers.** A Worker is selected for declared capabilities, not
  because it is a particular machine or provider. Losing or replacing a Worker
  cannot transfer EP lifecycle ownership; recovery uses EP's durable state and
  fresh repository evidence.
- **Storage and discovery.** External NVMe is reconstructable execution storage
  for checkouts, worktrees, builds, caches and artifacts. Canonical Forge and
  EP databases remain on durable internal control-plane storage. Discovery uses
  stable instance identity plus mDNS and configured Tailscale endpoints.
  Tailscale is transport, never authentication; each service still authenticates
  and authorizes its caller.

## 1.x freeze and future evolution

Engineering Platform 1.x is feature complete. Future innovation is expected
primarily within Forge. Engineering Platform may evolve only through explicitly
authorized platform revisions focused on:

- platform hardening, security and performance;
- operational tooling;
- generic execution capabilities;
- Execution Host evolution; and
- Forge-driven execution-contract changes.

Forge planning concepts do not automatically authorize Engineering Platform
growth. No Forge-specific planning capability belongs in Engineering Platform.

## Current runtime architecture — EP 2.0 repository-local

> **Scope notice.** This section describes the current repository-local
> Engineering Platform runtime. The EP 2.x extraction changes deployment,
> package and storage topology; it preserves the execution authority,
> admission, lifecycle, evidence and operator-merge semantics described here.
> ADR-0024 authorizes a future data-only schema-40 cutover to the
> installation-owned store. It preserves those semantics and permits exactly
> one writable authority; the current repository-local store remains active
> until explicit operator tooling completes the authorized procedure.

EP is an execution operating system, not a script that merely starts an AI
provider. Its current runtime accepts an Engineering Action through either a
physical Inbox route or a compliant producer route, admits one bounded run,
and carries that run through checkpoints, repository evidence, review,
operator hand-offs, finalization and reconciliation.

### Authority and evidence

```text
Forge / Human Architect / future Producer
                    │  Engineering Action + provenance
                    v
          Engineering Platform Execution Host
                    │
     admission → lifecycle → evidence → receipt / report
                    │
                    v
            operator-owned merge hand-off
```

- **Forge** owns planning intent, Missions, Engineering Actions, dependencies,
  Runtime Prompts and Decision Evidence. Workspace may initiate a submission
  through a consumer route, but it is not thereby the planning or producer
  authority.
- **EP** owns admission, execution, queueing, leases, checkpoints, execution
  telemetry, evidence, Engineering Reports, Execution Receipts, Prompt
  History, finalization and governed recovery.
- **The operator** owns pull-request merge authority. Owner authorization is a
  SHA-specific admission/merge gate where policy requires it; it is neither a
  formal review nor a merge.
- **SQLite and the persisted checkpoint** are operational truth. Status files,
  dashboard cards, watcher activity, telemetry, reviewer projections and AI
  analysis are read-only projections. Repository and GitHub evidence outrank
  every projection.

The dashboard can explain state and expose expressly allowed operator actions,
but it never becomes a second lifecycle, planning or repository authority.

### Standalone installation boundary

The extracted EP product provides a signed native macOS installer for its
own host runtime. The app is a user-facing wrapper around the one idempotent
`engineering-platform-host --install` engine; it does not duplicate host
mutation logic. That engine installs the pinned EP package and supported
provider CLIs, creates an empty installation-owned data root/database,
configures dashboard and watcher services, verifies one-writer health and then
opens the loopback Console for explicit first-run provider login. This is
distinct from DJConnect developer-machine bootstrap: EP does not inherit Apple
signing, Home Assistant lab or product-specific runner requirements.

One macOS user has one EP installation. The engine acquires an
installation-wide lock and detects any existing installation marker, writer and
database before changing host state. It requires an explicit non-destructive
choice to reuse existing data, replace it only after a verified backup, or
remove the exact EP data root and start clean after a second confirmation.
`engineering-platform-host --verify` is read-only and returns token-free,
structured diagnostics for the native installer: it either offers a confirmed
EP-managed repair or an official external help link for matters EP cannot fix.
It never treats missing provider login as a successful installation or admits
work while that condition remains unresolved.

The installer creates no project by inference. A Workspace consumer connects a
new or existing Git checkout only by supplying canonical project identity; EP
then validates the selected checkout and project Inbox route. Consumers pin the
wheel and use the Local Consumer API, while their CI exercises that adapter
against an ephemeral EP store. They never install a user host, manipulate EP
SQLite, start LaunchAgents or authenticate Codex/GitHub in CI.
The Local Consumer API is loopback-only.

ADR-0022 authorizes the next, still-unimplemented consumer boundary: EP-owned
registration for an exact `consumer_id` and `project_id`, production
verifier-only credentials, and consumer-side macOS Keychain storage. It does
not change this API's read-only surface, loopback bind, lifecycle authority or
current Forge, Workspace and DJConnect integrations.

### Common lifecycle invariants

Every run is admitted only after host, workspace and capability preflight.
Unknown required facts fail closed. The Execution Host holds one exclusive
lease, writes durable checkpoints and records evidence before advancing a
state. A restart or recovery never blindly continues from an old checkpoint:
it revalidates the checkpoint together with current repository and, where
applicable, GitHub evidence.

Provider readiness is phase-aware and token-free. A new Managed admission
requires Codex and GitHub readiness; a resumed execution uses the same durable
gate. Passive PR observation requires GitHub alone, while agent work requires
Codex too. Failed readiness is persisted as a non-terminal recovery block with
the original phase/action, rather than a retry loop or terminal failure. The
dashboard may offer one explicit local provider repair at a time, but cannot
start a repair agent or consume credits until fresh readiness evidence passes.

Codex capacity can additionally have an operator-selected admission reserve.
The reserve is local host configuration, is bounded to supported percentage
choices, and is checked only before a new Inbox claim using fresh read-only
quota evidence. Below the reserve (or when that evidence cannot be verified),
EP retains the queue item unclaimed. A claimed run is not interrupted by this
protection, which prevents a capacity guard from corrupting in-flight durable
state while reserving capacity for the operator.

Reviewer selection is policy-driven and may select zero reviewers. When it
does select reviewers, they receive bounded, fresh, read-only repository facts
and produce advisory observations only. The primary runner retains lifecycle
authority; a reviewer cannot merge, authorize, mutate the repository or turn
its recommendation into final evidence.

```text
admission → preflight → initialize → reviewer selection / review where selected
          → implementation → local repository validation (Managed)
          → autonomous quality control → evidence and delivery
          → finalization → reconciliation → terminal report
```

The exact delivery branch differs by execution mode; evidence, bounded scope,
lease ownership and terminal reporting do not.

### Managed execution flow

Managed is the normal flow for a configured repository with its required
remote, upstream, branch and pull-request rules.

1. EP claims the admitted Inbox or producer submission and acquires the run
   lease.
2. It validates the Managed repository profile, synchronizes the authorized
   baseline and captures fresh repository evidence.
3. It initializes the transaction, optionally conducts policy-selected
   specialist review, and performs the bounded implementation on its managed
   branch.
4. Before an implementation PR exists, EP runs the target repository's
   canonical required local validation and the relevant regression suite. This
   is a visible, bounded local-validation gate: the agent may make narrowly
   scoped production-code or test changes on the same branch to make the gate
   green, records the problem and action for each attempt, and stops fail-closed
   after at most three iterations. It must not create the implementation PR
   until that gate succeeds.
5. It runs autonomous quality control, then creates or updates the
   implementation pull request and observes its checks. A green PR moves to
   `WAIT_FOR_OPERATOR_MERGE`; it remains an active persisted hand-off, not a
   completed merge.
6. The operator grants any required owner authorization and merges in GitHub.
   EP observes the merge and proves the resulting commit is reachable from
   `origin/main`; it never merges on the operator's behalf.
7. EP performs finalization, which may itself create a distinct finalization
   PR. That PR has its own operator merge hand-off and must not be confused
   with the completed implementation merge.
8. After all required merge and repository evidence is proven, EP reconciles
   the checkout, writes terminal evidence and restores the workspace to its
   declared ready state.

```text
Managed repository
  → synchronize / preflight
  → implement → local repository validation (up to three iterations)
  → autonomous quality control → implementation PR / PR checks
  → WAIT_FOR_OPERATOR_MERGE
  → operator merge proven on main
  → finalization (optional finalization PR + separate hand-off)
  → end reconciliation
  → COMPLETE / BLOCKED / FAILED
```

### Managed PR, finalization and recovery hardening

The implementation and finalization PR paths use the same delivery safety
model. A remote PR is not a substitute for the persisted transaction: its
number, head branch, observed checks, merge evidence and repair audit are
durably associated with the corresponding transaction before the lifecycle can
advance. A finalization PR therefore remains distinct from the implementation
PR, including when an interrupted process is recovered.

For every mutating lifecycle phase, EP also maintains an append-only commit
timeline in the transaction checkpoint. An event contains the UTC observation
time, lifecycle phase, full commit SHA and a bounded safe description. It is
written in the same SQLite checkpoint transaction only after the active
repository proves a clean transaction branch at the exact reported SHA, or,
for an operator merge, after GitHub merge evidence and `origin/main` ancestry
are proven. A missing, dirty or mismatched repository never produces timeline
evidence. The execution-detail view projects this history beside provider
usage in a scrollable card, so audit volume cannot expand the lifecycle view.

For either PR, EP can enter bounded PR-control repair when current GitHub
evidence shows failed required checks or a repairable merge condition such as
an out-of-date (`BEHIND`), dirty or unstable branch. Repair stays on the
existing transaction branch and PR; it may update the branch and make only
objective-scoped changes. Each observed problem, proposed repair and result is
recorded, and no more than three repair attempts are permitted. Exhaustion,
ambiguous evidence or a changed branch/PR fails closed and requires an explicit
new decision rather than consuming provider time indefinitely.

Recovery always begins by re-verifying the persisted checkpoint against the
current repository and GitHub. In particular, it first looks for and proves an
already-created finalization PR before any action that could create one. A
recovery may attach verified existing evidence to the original finalization
transaction, but must never create a duplicate PR from stale state. The
operator remains the only merge authority for both hand-offs.

A missing or expired runner lease does not turn a persistent non-terminal
transaction into an idle or invisible run. The watcher and dashboard project
the durable phase as active/recoverable until terminal evidence is recorded;
they never invent a second lifecycle. This applies to finalization, PR
recovery and reconciliation phases as well as implementation work.

Terminal execution telemetry follows the same authority boundary, but is a
separate, derived operational projection. EP first queues immutable terminal
telemetry intent in SQLite and materializes the execution record, receipt and
daily aggregate idempotently. If projection fails, bounded recovery can rebuild
only from admissible transaction, prompt-history and timing evidence, with
provenance recorded as live, recovery or backfill. It cannot infer lifecycle
facts from report prose or alter a terminal outcome; uncertainty remains
visible and retryable instead of being silently counted or discarded.

The Operations Console exposes the local-validation and PR-repair attempts as
evidence, not as independent controls. Any new user-facing status or recovery
message follows the platform's five-language localization contract.

### Genesis execution flow

Genesis is a deliberately separate local-only lifecycle for an authorized
new or greenfield target. It never falls back to Managed.

1. The prompt declares `Execution Mode: Genesis` and one explicit,
   authorized local target repository.
2. EP validates the Genesis target profile: local Git safety, writable
   metadata, clean worktree, no conflicting lock and workspace ownership.
3. It initializes or evolves the local repository, performs reviewer selection
   and specialist review where policy selects it, then implements the bounded
   objective.
4. It performs autonomous quality control and local validation. Its generic
   quality/repair phase is not a Managed PR-control phase.
5. It writes a clean local commit checkpoint, gathers local reconciliation
   evidence, and emits its report and receipt.

Genesis neither requires nor contacts an upstream remote, `origin/main` or a
pull request. It has no operator merge hand-off. A target that is malformed,
outside authorization or indistinguishable from the Managed host repository is
blocked before provider execution starts.

```text
Genesis target
  → local preflight
  → initialize / implement / autonomous quality control
  → local commit checkpoint
  → local reconciliation
  → COMPLETE / BLOCKED / FAILED
```

### Current and target topology

| Concern | Current EP 2.0 runtime | Standalone EP 2.x target |
| --- | --- | --- |
| Product source | `djconnect` repository-local source | neutral EP package in its own repository and immutable wheel |
| Store | repository/workspace-local operational store | installation-owned SQLite store outside consumer repositories |
| Scope | current Managed checkout or Genesis target | immutable `project_id` plus registered workspace/repository |
| Display name | current workspace metadata | mutable Workspace-supplied `project_name`, used only as a label |
| Queues and Inbox | current configured route | one isolated Inbox route, FIFO queue and lease domain per project |
| Consumers | current local routes and dashboard | independently authenticated DJConnect, Forge and Workspace consumers through a versioned Local Consumer API |
| UI position | local Operations Console | same Operations Console semantics, selectable project projection |

In EP 2.x, `project_id` becomes the canonical cross-system scope. A path,
repository name or mutable display label is never an identity substitute. The
topology change must not weaken the current authority split: Forge remains the
planner, EP remains the execution authority, Workspace remains a presentation
and interaction consumer, and only repository/GitHub evidence proves delivery.

## Canonical documents

- [Platform status](../../tools/engineering/ENGINEERING_PLATFORM_STATUS.md)
- [Execution Host and Producer Contract](EXECUTION_HOST_CONTRACT.md)
- [Current lifecycle display and phase vocabulary](EXECUTION_LIFECYCLE_FLOW.md)
- [Genesis Mode](GENESIS_MODE.md)
- [Execution Host Operations](EXECUTION_HOST_OPERATIONS.md)
- [EP 2.x extraction and migration plan](../development/ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md)
- [Engineering Report Evidence Contract](ENGINEERING_REPORTING.md)
- [Execution Receipts](EXECUTION_RECEIPTS.md)
- [Platform 1.x Completion Report](ENGINEERING_PLATFORM_1_X_COMPLETION_REPORT.md)
- [Platform Governance](../../PLATFORM_GOVERNANCE.md)
- [Platform Evolution Backlog](../../PLATFORM_EVOLUTION_BACKLOG.md)

## Diff-derived validation profiles

The Execution Host owns one conservative validation-profile classifier for both
local validation and CI. It derives scope from the bounded branch diff:
documentation/run evidence, dashboard, runtime, or full. Unknown and mixed
changes always select the full suite. The local validation audit records the
chosen tier and required command categories, while the separate bounded
validation evidence records the executed command summaries. CI retains its
required check and skips only the expensive browser execution for an
unambiguous documentation profile.

An explicit negative success summary, such as `no whitespace errors`, records
`PASS`; only unnegated failure language is classified as a validation failure.
