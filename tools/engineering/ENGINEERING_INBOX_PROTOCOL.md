# Engineering Inbox Protocol v1

The configured Runtime Prompt transport accepts UTF-8 `.txt`, `.md` and
filename-neutral files whose bounded content is recognizably Markdown;
iOS-created `.txt` files are supported. The watcher requires a regular,
non-empty, non-symlink file with stable size and mtime before moving it out of
the transport inbox into local Engineering Platform storage. Job identity derives from
filename and content digest.

Jobs are strictly sequential: `iCloud Inbox → .engineering/inbox/Running →
.engineering/inbox/Completed|Failed`. A local immutable
`.engineering/inbox-processing/<job-id>/prompt.md` copy is the only executed
input. The watcher invokes only the repository-owned `engineering-execution-host` with owner
authorization and a stable run ID. Reports remain under `.engineering/reports/`
and status under `.engineering/status/`. iCloud is transport only; it retains no
reports, status or prompt archive after a job is claimed.

The private presentation contract for the dashboard is defined in
[`OPERATIONS_CONSOLE_DESIGN_SYSTEM.md`](OPERATIONS_CONSOLE_DESIGN_SYSTEM.md).
Prompt wording is governed separately by the producer-neutral
[`EP_PROMPT_AUTHORING_CONTRACT.md`](../../docs/engineering/EP_PROMPT_AUTHORING_CONTRACT.md);
this protocol governs transport and admission, not prompt structure.

## Producer Contract

The Execution Host consumes declared Producer metadata as immutable provenance:
`Producer ID`, `Producer Type`, `Producer Version`, `Producer Correlation ID`,
optional `Mission ID`, optional `Engineering Action ID`, and `Execution
Constraint Version`. Forge owns this contract and its semantics. The Execution
Host does not implement or interpret Forge logic; it persists and reports the
metadata only. Missing metadata remains compatible with existing prompts and
records `Producer Type: HUMAN` and `Producer ID: legacy`. Producer identity
never changes admission, scheduling, preflight, lifecycle or execution.

### Dependabot admission

On each watcher cycle, the Engineering Platform performs a bounded, read-only
REST discovery of open pull requests in its configured GitHub repository. Only
PRs whose author is `dependabot[bot]` or `app/dependabot` are eligible. For an
eligible PR not previously admitted, the watcher publishes one atomic JSON
Producer Submission Envelope into the same Inbox with `Producer Type:
EXTERNAL` and `Producer ID: github-dependabot`.

The generated objective binds the Managed transaction to that existing
Dependabot PR and branch. It requires compatibility/release-note review and
normal validation, and directs any bounded required-check repair to the same
PR. It never creates a replacement PR, changes approvals, enables auto-merge,
or merges. The existing implementation and Finalization operator merge gates,
repair-attempt limit, terminal report, Prompt History and post-Finalization
reconciliation remain authoritative.

Each publication receives one append-only local admission record containing
the repository, PR number, observed head commit, branch, submission ID and
timestamp. Discovery failures are logged with a bounded diagnostic but do not
block already submitted Inbox work and do not create an uncertain prompt.

## Execution Host Preflight Level 1

Before claiming a discovered Inbox item, the watcher runs fail-closed **Execution
Host Preflight Level 1**. It validates only the local Execution Host: readable
platform configuration, required runtime directories and their write access,
configured free disk capacity, Codex CLI presence/invocation, enabled telemetry
SQLite access, structured logging initialization, and host identity/version/
Bootstrap Contract. It does not inspect a Git repository, workspace state,
Engineering Actions, capability or mission.

`DJCONNECT_ENGINEERING_PREFLIGHT_MIN_FREE_BYTES` configures the minimum free
disk threshold in bytes (default: 1 GiB). A `FAIL` leaves the Inbox item in
place, starts no run and moves no prompt into `Running`. Evidence is stored
locally under `.engineering/status/host_preflight.json`; each failure records a
check identifier, bounded reason and recovery recommendation. `WARNING` is
reserved for future non-blocking host observations.

## Execution Host Preflight Level 2 (Workspace Preflight)

After Host Preflight passes and before the Inbox item is claimed, Workspace
Preflight validates only the selected engineering workspace. It canonically
resolves the target and evaluates the trusted Workspace Authorization policy:
configured roots with `direct_children` or explicit `descendants` scope,
optional repository allow-list and deny-list (deny wins), and the configured
symlink policy. The stable `WORKSPACE_TARGET_AUTHORIZED` check fails closed
before a claim when no policy authorizes the target. It then verifies Git metadata access and
write access, requires a clean staged/unstaged/untracked worktree, and rejects
index locks or unfinished merge, rebase, cherry-pick, revert and bisect work.
It also performs the exact Git index-lock transaction used by Git: atomically
create `index.lock`, read repository status through the locked index, then
remove only the lock it created. This prevents a generic directory write check
from passing when Git itself cannot safely use its index.
The Execution Host performs the immediately following managed synchronization
while it owns the active-run lease, before it invokes the agent. If it sees Git's explicit
temporary *index lock already exists* signal, the Execution Host retries only
that command up to three times with short backoff. It never removes a foreign
lock. A permission-denied or other Git failure remains fail-closed immediately
and its original bounded diagnostic is retained in the terminal checkpoint.

For a Managed transaction, the host's successful host, workspace and capability
preflights are also the authoritative admission evidence. The invoked agent must
not repeat development-host bootstrap verification or a predecessor lookup from
its sandboxed runtime: those checks can have different network access and create
a false blocking result after admission has already passed. The agent retains
GitHub access only for the transaction's own pull-request work.
The host supplies one explicit, authoritative checkout path for every Managed
transaction. Every Git and repository operation must use that checkout. A
`Target repository` value retained in the supplied objective is producer
provenance and cannot redirect execution to the dashboard source checkout or
another local worktree.
Managed execution also requires the configured branch, a valid origin and an
in-sync upstream; Genesis requires only a local target repository and does not
require a remote. It never changes repository state.

A Workspace Preflight `FAIL` leaves the Inbox item untouched, starts no
transaction and records a bounded identifier, reason and recovery
recommendation in `.engineering/status/workspace_preflight.json`. This evidence
contains workspace, target repository, branch, execution mode, timestamp,
duration, checks and outcome. It does not inspect Missions or Engineering
Actions.

## Execution Host Preflight Level 3 (Capability Preflight)

After Levels 1 and 2 pass and still before Inbox claim, Capability Preflight
evaluates the transaction's bounded, provider-neutral declaration against the
current Execution Host. Supported declarations include minimum **Execution Host
Version** and **Runner Version**, configuration/storage schema, checkpoint,
memory and report format, execution mode, required runtime components, provider
support and named capabilities. The host contract remains authoritative.

Failure leaves the Inbox item in place: no Run ID is allocated, no target
repository is changed, no execution telemetry is created and no Engineering
Report is generated. Instead the host writes bounded capability evidence to
`.engineering/status/capability_preflight.json`, including validated and
missing requirements, diagnostic identifiers, recovery recommendation,
**Failure Origin** (`CAPABILITY`) and **Recoverability**.

Recoverability is independent from a terminal execution state. A capability
failure is normally `RETRYABLE_AFTER_HOST_REPAIR`: repairing or upgrading the
host permits a new admission attempt, but does not restart engineering. Retry
availability is therefore based on recoverability, not merely on whether an
earlier execution ended `BLOCKED` or `FAILED`. Capability failures are counted
separately from engineering executions. The dashboard projects status,
recoverability, failure origin and the recommended operator action without
showing internal paths.

The private status page shows the current unclaimed queue from this watcher
projection, oldest first. Each bounded entry contains only its filename,
Markdown title and File Date Modified timestamp; it never exposes prompt body
content or absolute iCloud paths.

The Inbox is fail-closed across a sequence. When a run ends `BLOCKED` or
`FAILED`, the watcher moves no later file from Inbox to Running. It publishes
`WAITING_FOR_PREDECESSOR` with the blocking run, prompt and recovery action.
**Resume Queue** is the queue-recovery action and appears only while dependent
Inbox work is held at `WAITING_FOR_PREDECESSOR`. **Retry Execution** is a
separate engineering action available for every terminal `BLOCKED` or `FAILED` run,
whether or not later Inbox work is waiting. It always creates a new immutable
engineering execution using current repository state; it never changes the
original run.

Before either dashboard action creates a corrective Inbox prompt, it repeats
all three non-mutating admission preflights. A failed preflight returns its
bounded reason and recovery recommendation immediately, creates no retry
entry and leaves the same action enabled for a later attempt. The watcher
repeats preflight again when it later claims an accepted retry, so this early
operator feedback never weakens admission safety.

An operator can **Defer execution** for a still-waiting Inbox item from the
dashboard. After confirmation, the watcher lock atomically moves only that
source file to `Inbox/_deferred/`; it is retained intact, excluded from active
Inbox discovery and can be returned manually later. The action never deletes,
edits or claims an execution, and refuses an item that is no longer waiting.
Name collisions in `_deferred` are resolved without overwriting the earlier
file. The queue log records only bounded filenames and the outcome, never a
prompt body. A failed admission likewise records the failed preflight check,
safe recovery and bounded diagnostic in the component-log **Details** column;
it must not expose a secret or prompt content.

### Queue intervention acceptance criteria

Changes to retry, resume or defer behavior must prove all of the following:

- the request accepts only a basename for a still-waiting item; paths, missing
  items and claimed work are rejected;
- moving an item is atomic, retains its content and never overwrites an
  existing deferred filename;
- only the selected queue item leaves the active projection;
- the dashboard asks for confirmation before it sends a mutation; cancelling
  leaves the queue unchanged;
- operator log events contain actionable, bounded diagnostics without prompt
  content or credentials.

Both actions create a corrective prompt with explicit lineage metadata:

```text
Retry-Of: inbox-<blocking-run-id>
Original-Run-ID: inbox-<original-run-id>
Retry-Generation: <positive-integer>
Retry-Timestamp: <UTC ISO-8601 timestamp>
```

The corrective replacement takes precedence over later queued prompts. It
receives a new content-derived run ID and must complete before normal
oldest-first Inbox processing resumes. Retry Execution requires explicit
confirmation showing Run ID, prompt title, repository and execution mode, plus
an explanation that the original stays unchanged and a Retry relationship is
recorded. Neither action bypasses watcher ownership, bootstrap or runner
checks. This deliberately supplies sequential safety before a future
Engineering Intent `depends_on` model can express finer-grained rules.

### Verified execution-status reconciliation

**Restore execution status** is not a generic queue bypass or a retry. It is
available only for a terminal `BLOCKED` run when immutable state proves all of
the following: the terminal condition is `external_blocked`, neither an
implementation nor a Finalization pull request is recorded for that run, and
the bounded diagnostic identifies stale rolling status records. The dashboard
first performs this proof before it creates one dedicated governance-only
Finalization prompt with:

```text
Status-Reconciliation-Of: inbox-<blocked-run-id>
```

While that same run is the current blocking predecessor, the watcher admits
this marker only after repeating the immutable proof. A matching marker alone
is insufficient, and a marker for another predecessor stays queued at
`WAITING_FOR_PREDECESSOR`. The reconciliation then remains subject to normal
watcher ownership, preflight, runner and Finalization review. It reconciles
the required rolling status records with current `main`; it must not recreate
product implementation, rewrite Prompt History or alter retry semantics.

### Retry lineage and merged pull-request evidence

A retry may reconcile a previously merged implementation pull request only
when it can prove that the pull request belongs to that retry lineage: the
prompt contains `Retry-Of`, the recorded execution branch equals the pull
request head branch, the pull request targets `main`, and its merge commit is
present on `main`. The runner then records the existing merge as implementation
evidence and continues through the normal finalization or cleanup path. This
prevents an interrupted retry from being blocked solely because its own branch
was merged while it was running.

Any missing or mismatched condition remains a hard block. A retry must never
adopt an unrelated historical merged pull request as its own evidence.

## Development Host Drift Diagnostics

Development Host Qualification remains fail-closed and its admission behavior is
unchanged. When any preflight blocks admission, Engineering Platform now writes
an immutable evidence document under `.engineering/drift-evidence/` and
references the current evidence from the relevant preflight status projection.
Each evidence item contains a generated Drift ID, Category, Severity, Expected
Value, Observed Value, Resolution Recommendation, Detection Timestamp,
Qualification Stage, Affected Component, Affected Repository and Affected
Runtime. Evidence is append-only: a later qualification cannot rewrite an
earlier observed drift.

Supported categories are: **Runtime Database**, **Runtime Identity**, **Runtime
Schema**, **Execution Host Version**, **Bootstrap Contract**, **Checkpoint
Format**, **Memory Format**, **Report Format**, **Configuration**, **Workspace**,
**Repository**, **Capability**, **Producer Contract** and **Execution Policy**.
The taxonomy is extensible. Current Level 1 checks project runtime database,
runtime identity and configuration failures; Level 2 checks project workspace
and repository failures; Level 3 checks project version, schema, format,
capability and execution-policy failures. Producer Contract remains a supported
diagnostic category for a future producer validation check; producer metadata
continues to have no admission effect today.

The Dashboard projects the first current blocking drift read-only, including
severity, affected component, expected state, observed state and resolution.
The Engineering Report renders every drift item with its blocking reason,
required action and affected repository/runtime. A blocked qualification also
states retry/resume guidance: repair the listed prerequisite first, then retry
admission; resuming an unclaimed execution is not appropriate. Operator
intervention is required while blocking drift remains. These statements are
diagnostics only and do not alter retry, resume, queue or execution lifecycle
semantics.

## Dismiss Execution

**Dismiss Execution** is a confirmed operator acknowledgement, not an
engineering operation. It is available only for the current terminal execution
and clears Active Execution so the watcher returns to idle without changing the
queue. The confirmation shows Run ID, prompt title and terminal state, and
explains that no work will restart.

Dismiss records `dismissed`, `dismissed_at` and `dismissed_by` as immutable
operator-handling evidence in the canonical SQLite datastore. Engineering
Reports, terminal evidence, telemetry, Prompt History and retry relationships
remain immutable. A dismissed terminal execution is read-only: Retry, Resume,
Dismiss and every other lifecycle-mutating action are unavailable and rejected
server-side, including requests from a stale client. Queue Recovery remains the
separate explicit operation for dependent Inbox work. Dismiss never resumes
that queue. Existing records from the former JSON audit are imported
idempotently into SQLite; thereafter SQLite remains authoritative.

Commands: `python3 -m tools.engineering.inbox_watcher once|run|status|install|uninstall|doctor|migrate-icloud-archives`.
