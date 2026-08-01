# Engineering Platform Storage Contract

## Purpose

Engineering Platform persistent evidence is being prepared for consolidation
under the repository-local, git-ignored `.engineering/` workspace. Its only
database path is:

```text
.engineering/engineering.db
```

iCloud Drive remains transport only. It is not an Engineering evidence store.

## Versioned schema

The storage contract is independently versioned as **Engineering Storage
schema `1`**. The required version is declared as `storage_schema` in
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

## Current transition status

Schema `1` is the versioned storage foundation. It defines normalized tables
for status projections, transaction checkpoints, immutable artifacts and
redacted component logs. The current watcher, runner and dashboard still use
the existing `.djconnect/` evidence layout until their complete migration is
implemented and qualified as one compatibility-preserving transaction.

This distinction is intentional: creating a database schema does not silently
change runtime authority or move live evidence. A future migration must first
copy and verify legacy status, reports, prompts, analyses, usage, checkpoints,
logs, qualification evidence and lock metadata; only then may `.engineering`
become the sole canonical local location.

## Integrity and privacy

The database is private to the local user (`0600` where supported), git-ignored
and contains only redacted Engineering Platform evidence. It has no cloud sync,
network listener, release, deployment or publication authority.
