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

## Provider readiness and explicit repair

Before an Inbox item is claimed, capability preflight verifies the local Codex
session for every execution and the GitHub CLI session for Managed execution.
Genesis does not require GitHub until a future transaction explicitly declares
that dependency. A missing CLI, expired login or indeterminate provider check
fails closed before an agent starts, so it cannot consume credits in an
authentication retry loop.

The dashboard projects the same token-free evidence in Configuration and in
separate sticky notifications for Codex and GitHub. An operator can explicitly
install a missing CLI or open its browser-backed terminal login. At most one
interactive provider repair may be active at once. A repair never runs from an
execution, never exposes credentials, and does not resolve its banner until a
new check confirms readiness. The same installation action is available beside
the affected provider in Configuration; per-provider sign-out remains available
there to test a fresh session deliberately.

The dashboard checks both providers immediately when it opens, then rechecks
while its tab is visible at a configurable **1, 5 or 10 minute** interval
(five minutes by default). These are read-only local readiness checks: they
never reveal credentials, claim queue work, start an execution or consume
Codex credits. If an initial check fails because the dashboard or its local
connection was restarting, the next return to the visible tab immediately
rechecks both providers instead of retaining a stale warning until the next
polling interval.

## Codex capacity reserve for new work

The local **Available AI capacity** panel has a bounded optional reserve of
`0`, `5`, `10`, `15`, `20`, `25`, `50` or `75` percent (default `0`). Its
pulldown only offers values that are at or below the currently observed
remaining capacity. The dashboard also obtains fresh, read-only Codex quota
evidence before it persists an increased reserve: a direct API request above
the fresh value is rejected, and an unavailable reading never permits an
increase. Lowering an existing reserve remains possible when the reading is
temporarily unavailable.

When a reserve is configured, Capability Preflight obtains fresh, read-only
Codex quota evidence before an Inbox claim. If the lowest remaining quota
window is below the reserve—or cannot safely be read—the item remains
unclaimed in Inbox and the admission record reports the capacity-reserve
failure. No agent is started and no Codex credit is consumed by a retry loop.
This is an admission-only guard: an execution that was already claimed remains
eligible to finish.

The same gate applies when an existing execution resumes. A failed check is a
non-terminal, durable `provider_auth_repair_required` checkpoint: it preserves
the original phase and next action, and records only the affected provider
names. A green verification restores that action. Passive Managed PR waiting
requires GitHub only; Codex is required immediately before an agent repair,
finalization, reconciliation, or other agent action can start. This prevents
both accidental credit use and needless blocking of passive merge observation.

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

Execution-detail downloads are projections of the same durable run evidence as
the dashboard: both Markdown and JSON include linked implementation/finalization
pull requests when present, plus the verified phase-commit timeline. The JSON
retains the structured `pull_requests` and `commit_timeline` records; Markdown
renders their GitHub links and localized phase, commit-type and description
labels for human review.

The daily telemetry detail keeps wide per-run evidence in its own horizontally
scrollable table region. This prevents a wide table from making the complete
detail dialog scroll sideways, while retaining access to every column on narrow
screens.

Terminal telemetry is durable but non-authoritative. On watcher startup it
first drains its local telemetry outbox, then performs a bounded, fail-closed
recovery for a terminal run missing from telemetry. Recovery verifies the
canonical checkpoint, Prompt History and recorded terminal timing before it
creates the projection; it never restarts or mutates an execution, rewrites a
report, or changes repository state. A recovered run keeps its original
terminal date, and a repeated recovery cannot add a second run or count.

## Operator actions

## Local repository validation gate

Validation is selected from the actual bounded-branch diff. Documentation and
run-evidence-only changes use document/link/contract validation; dashboard and
runtime changes retain their relevant Python and browser coverage; mixed or
unknown scope always selects the full required suite. The selected tier and
command categories are iteration evidence; bounded validation evidence
separately records the executed command summaries. GitHub keeps the required
validation check; only its costly browser work is skipped for an unambiguous
documentation tier.

For a Managed implementation, the Execution Host first creates and pushes the
bounded branch without creating a pull request. The visible **Local repository
validation** step discovers and runs the target repository's canonical required
local validation. It may make scoped production-code and test corrections on
that same branch and retries at most three times. Each attempt records its safe
problem, corrective action, result and commit evidence. Only a passing attempt
may create the draft implementation pull request. Remote GitHub check repair
remains a separate, later bounded gate.

Both bounded gates preserve the same immutable per-attempt shape: iteration,
observation time, observed problem, proposed action, safe agent summary,
commit evidence and outcome. Local validation uses `validated`,
`validation_failed` or `agent_failed`; PR repair uses
`submitted_for_recheck`, `agent_failed` or `agent_timed_out`. The latter is a
host-owned deadline outcome, not an invitation to start another repair: the
run is blocked with its evidence intact and requires a new explicit recovery
decision. The Console renders these records as iteration evidence and uses
the same five-language status contract as the rest of the lifecycle.

## Verified phase-commit timeline

Alongside the per-attempt records, each mutating execution phase may append a
verified commit event: UTC observation time, phase, full SHA and a bounded
description. The host records it only after a clean local repository proves
the exact branch/SHA reported by the agent. Operator merge events require the
separate GitHub merge and `origin/main` ancestry proof. Events are append-only,
deduplicated by phase and SHA, and saved atomically with the transaction
checkpoint. The execution-details modal renders them chronologically in a
bounded, vertically scrollable card beside AI-provider usage; missing evidence
is shown as missing rather than reconstructed from report text.

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
