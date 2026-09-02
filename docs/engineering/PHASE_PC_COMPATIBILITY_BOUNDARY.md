# Phase P-C CENTRAL/project compatibility boundary

**Status:** `P-C COMPATIBILITY BOUNDARY READY FOR P-A/P-B`  
**Base:** schema 44, `f2ca7ff`  
**Implementation:** `engineering_platform.parity_context`

**Migration-gap register:** The parity boundary deliberately retains historical
physical execution while CENTRAL is introduced.  The resulting active and
repaired migration gaps are tracked in
[Phase P migration-gaps register](PHASE_P_MIGRATION_GAPS_REGISTER.md).  A
validated local root is not evidence that a retained local projection has
become CENTRAL-owned.

## Authority and scope

CENTRAL remains the lifecycle and durable-store authority. The installed local
historical execution host is the temporary Phase-P physical implementation;
Project Agent remains trusted/attached but has no Phase-P execution authority.
This boundary does not schedule, allocate a run, acquire a lease, invoke a
provider, or restore the Operations Console.

`project_context` is the only CENTRAL-to-parity construction path. It validates
the active project, repository/project ownership and authority repository, then
uses schema-44 `resolve_execution_repository` exclusively when a local root is
required. There is no CWD, Git, Agent-storage, repository-name, or portable
declaration-path fallback. Read-only Console data can request a rootless context;
repository actions and future local execution require a bound root.

## Context and submission bridge

`ParityProjectContext` carries the installation ID, installation data root,
project ID, repository ID, authority repository ID and optional validated local
root. It has no HTTP/CLI/browser state, Agent credential, or provider command.

`historical_candidate` reads one `ep_submissions` row only when it is the same
project/repository and is `QUEUED`/`ADMITTED`. It preserves submission ID, prompt
and digest, producer identity/version, transport, correlation/mission/action
lineage, and constraints. Its `producer_envelope()` is accepted by the preserved
`parse_producer_submission` parser, so P-A can invoke the historical core
directly without fabricating Inbox files.

Execution mode is transported using the existing historical
`execution_context.execution_mode_for(prompt)` rule: `MANAGED` by default and
`GENESIS` only for the existing prompt declaration. P-C does not implement either
workflow.

## Run, queue, lease, and recovery contract

P-C does not allocate runs. P-A retains historical allocation at its admission
boundary (`inbox_watcher._allocate_run_id`) and must append the durable
submission-to-run association to CENTRAL at that same single-writer boundary.
Until dispatch, CENTRAL `QUEUED` + `ADMITTED` maps to an eligible historical
candidate; it is not a new scheduler state. Retry/predecessor/recovery retain the
same explicit project/repository context and use the eventual run ID.

`ParityProjectStore` is the common project-bound P-A/P-B access pattern. It
supplies scoped queued-submission and run reads, a dashboard-shaped projection,
and server-side action-target validation. It has no lifecycle mutation methods.
P-A will inject the same context at the preserved storage boundary for leases,
history, reports/evidence, telemetry and provider usage; no repository-local
database or shadow queue is introduced.

Rendered reports, receipts and evidence remain filesystem artifacts under a
future installation/project/run-owned artifact location. They are not lifecycle
authority and no artifact path is created by P-C.

## Console and Phase-S handoff

P-B consumes `ParityProjectStore.dashboard_projection()` only after server-side
project/repository validation. Browser selection is presentation state, never
authority. Installation identity, schema, Server health, database diagnostics,
installed version and global component state remain installation-scoped.

Phase S replaces only the local physical execution implementation with typed
CENTRAL-to-Project-Agent dispatch. It must preserve this context and the
historical Engineering Action semantics.

## Qualification

Focused tests use two bound projects and prove context/read isolation,
cross-project submission rejection, action-scope rejection, rootless read
contexts, required bound-root contexts, Managed/Genesis transport, and preserved
Producer Submission Envelope parsing. No test dispatches a run or invokes a
provider.
