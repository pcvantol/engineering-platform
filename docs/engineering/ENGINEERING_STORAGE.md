# Engineering Platform Storage Contract

## Purpose

Engineering Platform persistent evidence is stored in the repository-local,
git-ignored `.engineering/` workspace. Its only database path is:

```text
.engineering/engineering.db
```

iCloud Drive remains transport only. It is not an Engineering evidence store.

## Versioned schema

The storage contract is independently versioned as **Engineering Storage
schema `6`**. The required version is declared as `storage_schema` in
`tools/engineering/ENGINEERING_PLATFORM_VERSION.json` and is validated by the
runner compatibility contract.

The database records every applied change in
`engineering_schema_migrations`. Opening it is fail-closed when:

- a database contains unrecognized tables without an Engineering schema
  history;
- the recorded schema version is newer than the installed Engineering
  Platform supports; or
- a required migration is unavailable or cannot complete safely.

Schema upgrades use a controlled SQLite transaction and rollback-journal mode.
The latter avoids persistent WAL sidecar files in `.engineering/`.

## Execution Host telemetry

Schema `2` adds the generic, local-only Execution Host telemetry model. Schema
`3` adds total elapsed time to that model. Schema `4` makes SQLite the
canonical component-log store and imports the previous redacted JSONL logs on
first upgrade:

- `execution_runs` stores one operational record per terminal run, including
  Inbox arrival, runner start, completion, measured Codex CLI duration and
  total elapsed duration from Inbox arrival through terminal status publication,
  explicitly reported token values, terminal state, execution mode, workspace,
  repository and Execution Host version;
- `daily_execution_statistics` stores daily aggregates for prompt counts,
  average Codex CLI, total elapsed and queue waiting times, explicitly reported token totals,
  and COMPLETE/BLOCKED/FAILED distribution.

Telemetry is best-effort and is scheduled only after terminal report delivery.
An unavailable database is logged by the watcher but never changes the
authoritative engineering checkpoint or its outcome. Token values remain null
when the provider did not report them; the platform never estimates them.

## Component logging

`engineering_component_logs` is the canonical store for redacted watcher and
dashboard events. The dashboard reads its bounded log views from this table,
and clearing a component log removes only that component's SQLite rows.

The table also records bounded, redacted dashboard user actions that have an
operational effect or access local evidence: component restart requests,
provider reset-credit requests, submitted AI-advice questions, and report or
log downloads. A user action records its event type and the applicable fixed
component or run identifier, never its free-form chat text, report body,
credentials or browser-supplied command.

The former `.engineering/logs/inbox.log` and `dashboard.log` files are no
longer normal application logs. They are created only as a private, rotating
fallback when SQLite cannot be opened during early startup or an application
failure. Existing redacted JSONL entries are imported once during the schema
`4` migration. LaunchAgent `*.out.log` and `*.err.log` streams remain separate
process-level crash diagnostics.

## Prompt history and retry lineage

Schema `5` adds `prompt_execution_history`, a canonical local index of every
terminal Engineering Platform run. It stores the immutable run identifier,
terminal status, prompt title, execution timestamp, available Git commit and
the relative location of a delivered Engineering Report. Existing reports and
telemetry runs are backfilled safely when the dashboard first requests the
history. The index is a convenience projection only: the checkpoint, report
and target repository remain authoritative evidence.

The private dashboard exposes this as **Promptgeschiedenis**. Its searchable,
sortable and paginated table can download an indexed report only when that
report was actually delivered locally.

Schema `6` adds immutable `retry_of`, `original_run_id`, `retry_generation`
and `retry_timestamp` fields to prompt history and execution telemetry. Each
retry has its own Run ID, report and telemetry row; lineage links evidence
without merging or overwriting original runs.

## Canonical workspace migration

`.engineering/` is the sole canonical local location for status projections,
transaction checkpoints, immutable artifacts, reports, redacted component logs
and locks. When an existing workspace contains the historical `.djconnect/`
directory, provisioning performs a local, fail-closed migration before any
component starts:

- existing evidence is moved to `.engineering/` without rewriting it;
- byte-identical duplicates are discarded only after verification;
- a conflicting historic log or qualification category is retained under
  `.engineering/legacy/` without replacing its active counterpart;
- a conflicting file, symlink or incompatible path type aborts the migration;
- the legacy directory is removed only after every child has migrated.

The migration has no cloud, release, deployment or publication effect.

## Integrity and privacy

The database is private to the local user (`0600` where supported), git-ignored
and contains only redacted Engineering Platform evidence. It has no cloud sync,
network listener, release, deployment or publication authority.
