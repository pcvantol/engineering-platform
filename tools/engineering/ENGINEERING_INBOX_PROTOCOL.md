# Engineering Inbox Protocol v1

The local iCloud Engineering Inbox accepts UTF-8 `.txt`, `.md` and
filename-neutral files whose bounded content is recognizably Markdown;
iOS-created `.txt` files are supported. The watcher requires a regular,
non-empty, non-symlink file with stable size and mtime before atomically moving
it from Inbox to Running. Eligible files are processed by File Date Modified,
oldest first. Job identity derives from filename and content digest.

Jobs are serialized: `Inbox → Running → Completed|Failed`. A local immutable
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

Commands: `python3 -m tools.engineering.inbox_watcher once|run|status|install|uninstall|doctor`.
