# Execution Host Operations

This is the current operator guide for the Engineering Platform Execution Host.
It consolidates the operational outcome of PRs #715–#723 without replacing
their immutable Prompt History or finalization evidence.

## Stable execution boundary

The Execution Host is generic. It executes compliant Engineering Actions with
the same lifecycle, preflight, evidence and terminal semantics for every
Producer. Human Architect, Forge and future Producer identity is retained only
as provenance; operators must not use it to infer planning authority or alter
execution behaviour.

Forge owns Mission planning, Runtime Prompts, Decision Evidence and Runtime
Instance concepts. The Execution Host owns action execution, qualification,
Execution Evidence, Engineering Reports and Execution Receipts. It does not
implement Forge, recommend Missions or perform business, architecture or
runtime planning.

## Admission and target safety

Execution Host Preflight validates host readiness before an Inbox claim.
Workspace Preflight then validates the selected target, Git safety and mode
requirements. Workspace authorization is trusted host configuration: roots,
scopes and repository allow/deny lists are evaluated fail-closed. Managed
execution remains subject to its branch, remote and upstream requirements.

## Configuration and transport

The Execution Host Configuration Resolver is the only host-specific location
resolver. It selects Runtime Prompt transport, local evidence stores, runtime
and safe host identity. The current iCloud Inbox is transport only; Forge never
receives its path or any dashboard, launchd or local-storage detail.

## Telemetry retention and exports

Execution Host telemetry is operational observability, not repository evidence.
The dashboard keeps it for a configurable rolling period of **30, 60, 90, 120,
180 or 360 days**; the default is 90 days. The selected period determines both
the retained daily and per-run telemetry and the period shown in the telemetry
dashboard.

Lowering the period always asks for explicit confirmation before the
transactional cleanup runs. Cleanup removes only expired rebuildable telemetry
rows (`execution_runs` and daily execution statistics). It never removes
Execution Receipts, Engineering Reports, Prompt History, retry lineage or
repository evidence. Operators can export telemetry and download a consistent
database snapshot for offline backup before changing retention.

The daily telemetry detail keeps wide per-run evidence in its own horizontally
scrollable table region. This prevents a wide table from making the complete
detail dialog scroll sideways, while retaining access to every column on narrow
screens.

## Operator actions

- **Pull-request merge hand-off** is shown as a persistent, dashboard-native
  wait state with a direct GitHub link once required checks are green. Closing
  the browser does not cancel it: the watcher polls the persisted run and
  resumes it after the operator merges, even on a later day. **Abort
  execution** is the only explicit way to end this hand-off without merging;
  it records the dismissal and archives the execution as failed, without
  deleting its evidence.
- **Retry Execution** creates a new execution from a terminal `BLOCKED` or
  `FAILED` run and records immutable retry lineage.
- **Queue Recovery** is a separate explicit retry for a blocked predecessor
  when dependent Inbox work is waiting. It does not bypass queue ordering.
- **Dismiss Execution** is a confirmed acknowledgement of the current terminal
  execution. It clears operational attention only and records dismissal audit
  fields. It never deletes reports, telemetry, Prompt History or retry lineage,
  and it never resumes the queue.

Repository truth and engineering history are immutable under all three actions.
Only a new retry performs engineering work.

## Workspace branch controls

The Operations Console Workspace card shows the current local branch. Its
yellow actions are deliberately separate from terminal-status colours:

- **Scan branches for cleanup** opens its modal immediately, shows a waiting
  spinner while it checks, and lists only local branches that no longer exist
  on `origin` and are patch-equivalent to synchronized `main`. Matching merged
  GitHub pull requests are linked as operator context only. The red removal
  action stays disabled until the reviewed list is loaded. If no candidates
  exist, the modal remains open with that result and only a close action.
- **Switch to FF main** is shown only when `HEAD` differs from `origin/main`.
  After confirmation it refuses dirty workspaces, unavailable `origin`, or
  local commits on `main`; it switches only to the configured `main` branch
  and fast-forwards only. A yellow result modal reports either the completed
  switch or the precise safe refusal.
- **Open pull requests** appears as a compact Workspace subblock only when
  GitHub reports open PRs for the repository. Each entry preserves its PR
  link, title and source branch as read-only operator context. If GitHub
  context is unavailable or no PR is open, the subblock is omitted.

Neither control rewrites history, stashes work, or deletes a branch without
the explicit second confirmation.
