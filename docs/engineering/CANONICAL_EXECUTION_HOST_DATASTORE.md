# Canonical Execution Host Datastore

Engineering Platform operational state is owned by `.engineering/engineering.db`.
SQLite is the sole authority for lifecycle, active and terminal transaction state,
submission envelopes, retry lineage, prompt history, receipts, projection state,
artifact metadata and schema/migration provenance.

## Storage boundary

| Classification | Authority | Examples |
| --- | --- | --- |
| `PROJECTION` | SQLite record; file is regenerable | `status.json`, `current.json`, dashboard views |
| `ARTIFACT_PAYLOAD` | Immutable file payload plus SQLite metadata/digest | reports and analyses |
| `CONFIGURATION` | Filesystem | launchd and platform configuration |
| `OBSERVABILITY` | Filesystem/SQLite as appropriate | locks, leases, process data and logs |
| `RECOVERY_EXPORT` | Generated from SQLite | operator recovery exports |

Producer prompts are recorded in `execution_submissions` before Inbox work is
accepted. The source envelope, producer contract fields, content, target and
execution correlation are retained there. Forge-supplied execution context is
stored only as an immutable projection; Engineering Platform never derives
Mission lifecycle, intent, action, confidence, dispatcher or queue semantics.

## Migration and recovery

Schema migrations are ordered, transactional and idempotent. Versions 12 and 13 import
legacy status, valid checkpoints and immutable report metadata once, with a SHA-256 provenance record. A
changed legacy source after import, or a conflict with an existing canonical
transaction, fails closed. Invalid legacy checkpoints remain untouched for
diagnosis and are never promoted into authority.

The explicit compatibility importer exists only for hosts upgraded from an
earlier release where an old status file appears after the initial migration.
It imports once, then all reads use SQLite. Deleting `current.json`, a report
Markdown projection, or a dashboard view cannot delete operational history.

`regenerate_status_projections()` deterministically recreates `status.json` and
`current.json` from SQLite. Artifact metadata records a SHA-256 digest,
content type, storage location, producer/run/mission linkage and integrity
state. `verify_artifact_integrity()` marks a mismatch without silently trusting
the changed payload.

If SQLite cannot be opened, is corrupt, has an unknown schema, or reports a
migration conflict, lifecycle operations fail closed. The dashboard reports
unavailable status rather than reconstructing authority from arbitrary files.

## Compatibility exit

JSON and Markdown operational files are compatibility projections, not inputs
to new Engineering Platform behavior. Remove the explicit legacy importer only
after every supported host has completed the v13 migration and recovery exports
have been verified.
