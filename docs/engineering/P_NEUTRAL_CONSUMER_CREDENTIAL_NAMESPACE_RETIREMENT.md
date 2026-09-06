# P-NEUTRAL consumer credential namespace retirement

## Authority conclusion

The historical Local Consumer API service is retired. The credential state it
once named is not service state: it is scoped authentication persistence for
the canonical Engineering Platform Server HTTP JSON ingress.

Current authority is exclusively:

- `ep_consumer_credentials` for opaque verifier/fingerprint credential records;
- `ep_consumer_registrations` for consumer/project authorization state.

HTTP JSON, installed CLI and File Inbox remain the only submission ingresses.
They normalize through Engineering Platform Server, Submission Service and
CENTRAL. No credential migration step installs or addresses a Local API
service, LaunchAgent, port 8766 or lifecycle target.

## Reference classification

| Classification | Bounded references |
| --- | --- |
| `CURRENT_SERVER_AUTHORITY` | `server.py`, `submission_service.py`, `ep_consumer_credentials.py`, and the current storage schema use only `ep_consumer_*`. |
| `MIGRATION_SOURCE_ONLY` | The schema-53 Server migration and schema-41 storage migration inspect `local_api_*` only to copy and compare an already-existing store inside one transaction. |
| `ROLLBACK_SOURCE_ONLY` | Retained `local_api_*` tables are untouched source evidence after a successful migration; runtime code never reads or writes them. |
| `HISTORICAL_ONLY` | Historical central-store migration controls, forensic evidence and provenance retain predecessor table names to describe their original stores. |
| `TEST_ONLY` | Legacy-shape fixtures, `local_api_keychain.py` compatibility coverage and negative migration tests exercise rejection and preservation without exposing credential values. |

No reference is unclassified.

## Transfer and retention boundary

Schema migrations retain all credential values exactly: no credential is
regenerated, transformed, logged or exported. They create the neutral tables,
copy records, prove cardinality and bidirectional identity equivalence without
diagnostic values, and only then advance schema metadata. Any failure rolls
back the destination and metadata atomically.

The legacy tables are not dropped in this increment. Their final retention or
destructive removal is deferred to
`CONSUMER_CREDENTIAL_LEGACY_TABLE_RETENTION_CLOSURE`.
