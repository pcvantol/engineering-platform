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
To release the queue, submit a corrected replacement prompt or explicitly
resubmit the archived blocked prompt from the private dashboard. Both routes
create a retry prompt containing this standalone line, with the exact blocking
run ID shown on the status page:

```text
Retry-Of: inbox-<blocking-run-id>
```

The corrected retry takes precedence over later queued prompts. It receives a
new content-derived run ID and must complete before normal oldest-first Inbox
processing resumes. The dashboard only exposes **Opnieuw indienen** while a
predecessor is actively blocking the queue. It asks for confirmation, copies
the immutable archived prompt into the iCloud Inbox transport with `Retry-Of`,
and does not bypass watcher ownership, bootstrap or runner checks. This
deliberately supplies sequential safety before a future Engineering Intent
`depends_on` model can express finer-grained rules.

Commands: `python3 -m tools.engineering.inbox_watcher once|run|status|install|uninstall|doctor|migrate-icloud-archives`.
