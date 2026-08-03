# Engineering Inbox Protocol v1

The local iCloud Engineering Inbox accepts UTF-8 `.txt`, `.md` and
filename-neutral files whose bounded content is recognizably Markdown;
iOS-created `.txt` files are supported. The watcher requires a regular,
non-empty, non-symlink file with stable size and mtime before moving it out of
iCloud Inbox into local Engineering Platform storage. Job identity derives from
filename and content digest.

Jobs are strictly sequential: `iCloud Inbox → .engineering/inbox/Running →
.engineering/inbox/Completed|Failed`. A local immutable
`.engineering/inbox-processing/<job-id>/prompt.md` copy is the only executed
input. The watcher invokes only the repository-owned `engineering-execution-host` with owner
authorization and a stable run ID. Reports remain under `.engineering/reports/`
and status under `.engineering/status/`. iCloud is transport only; it retains no
reports, status or prompt archive after a job is claimed.

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

Commands: `python3 -m tools.engineering.inbox_watcher once|run|status|install|uninstall|doctor|migrate-icloud-archives`.
