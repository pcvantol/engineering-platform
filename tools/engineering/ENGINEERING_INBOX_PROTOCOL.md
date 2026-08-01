# Engineering Inbox Protocol v1

The local iCloud Engineering Inbox accepts UTF-8 `.txt`, `.md` and
filename-neutral files whose bounded content is recognizably Markdown;
iOS-created `.txt` files are supported. The watcher requires a regular,
non-empty, non-symlink file with stable size and mtime before atomically moving
it from Inbox to Running. Eligible files are processed by File Date Modified,
oldest first. Job identity derives from filename and content digest.

Jobs are strictly sequential: `Inbox → Running → Completed|Failed`. A local immutable
`.djconnect/inbox-processing/<job-id>/prompt.md` copy is the only executed
input. The watcher invokes only the repository-owned `dj-engineer` with owner
authorization and a stable run ID. iCloud is transport only, never repository
truth. Reports and status are convenience artifacts; credentials, prompt
contents and executable input are never published.

The runner's authoritative local report is written under `.djconnect/reports/`.
After a terminal checkpoint, the watcher copies a checkpoint-consistent report
to `DJConnect Engineering/Reports/`; it publishes a bounded corrected report
when the original report contradicts the terminal checkpoint. The watcher
status projection is `DJConnect Engineering/status.json`.
The Inbox is fail-closed across a sequence. When a run ends `BLOCKED` or
`FAILED`, the watcher moves no later file from Inbox to Running. It publishes
`WAITING_FOR_PREDECESSOR` with the blocking run, prompt and recovery action.
To release the queue, submit a corrected replacement prompt containing this
standalone line, with the exact blocking run ID shown on the status page:

```text
Retry-Of: inbox-<blocking-run-id>
```

The corrected retry takes precedence over later queued prompts. It receives a
new content-derived run ID and must complete before normal oldest-first Inbox
processing resumes. This deliberately supplies sequential safety before a
future Engineering Intent `depends_on` model can express finer-grained rules.

Commands: `python3 -m tools.engineering.inbox_watcher once|run|status|install|uninstall|doctor`.
