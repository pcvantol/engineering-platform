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
Managed execution also requires the configured branch, a valid origin and an
in-sync upstream; Genesis requires only a local target repository and does not
require a remote. It never changes repository state.

A Workspace Preflight `FAIL` leaves the Inbox item untouched, starts no
transaction and records a bounded identifier, reason and recovery
recommendation in `.engineering/status/workspace_preflight.json`. This evidence
contains workspace, target repository, branch, execution mode, timestamp,
duration, checks and outcome. It does not inspect Missions, Engineering
Actions, Runtime Prompts or capabilities. Capability-specific checks are a
future Level 3 concern.

The private status page shows the current unclaimed queue from this watcher
projection, oldest first. Each bounded entry contains only its filename,
Markdown title and File Date Modified timestamp; it never exposes prompt body
content or absolute iCloud paths.

The Inbox is fail-closed across a sequence. When a run ends `BLOCKED` or
`FAILED`, the watcher moves no later file from Inbox to Running. It publishes
`WAITING_FOR_PREDECESSOR` with the blocking run, prompt and recovery action.
**Resume Queue** is the queue-recovery action and appears only while dependent
Inbox work is held at `WAITING_FOR_PREDECESSOR`. **Retry Execution** is a
separate engineering action available for every terminal `BLOCKED` run,
whether or not later Inbox work is waiting. It always creates a new immutable
engineering execution using current repository state; it never changes the
original run.

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

## Dismiss Execution

**Dismiss Execution** is a confirmed operator acknowledgement, not an
engineering operation. It is available only for the current terminal execution
and clears Active Execution so the watcher returns to idle without changing the
queue. The confirmation shows Run ID, prompt title and terminal state, and
explains that no work will restart.

Dismiss records `dismissed`, `dismissed_at` and `dismissed_by` in local audit
evidence. Engineering Reports, terminal evidence, telemetry, Prompt History
and retry relationships remain immutable. A dismissed `BLOCKED` execution may
still be retried later, while Queue Recovery remains the separate explicit
operation for dependent Inbox work. Dismiss never resumes that queue.

Commands: `python3 -m tools.engineering.inbox_watcher once|run|status|install|uninstall|doctor|migrate-icloud-archives`.
