# P-TRANSPORT authority map

**Status:** canonical-ingress closure evidence

| Ingress | Classification | Normalization and destination | Prohibited authority / evidence |
| --- | --- | --- | --- |
| HTTP `POST /v1/projects/{project}/submissions` | THIN_TRANSPORT | `request_from_mapping` → `submission_service.submit` in the Server CENTRAL database | no retry, queue, lifecycle worker, or execution call |
| `engineering-platform submit` | THIN_TRANSPORT | parses local prompt/constraints → authenticated HTTP endpoint → same service | no SQLite import or direct CENTRAL-table access |
| `engineering-platform-file-inbox` | THIN_TRANSPORT | explicit JSON envelope → authenticated HTTP endpoint → same service | only physical archive/receipt acknowledgement; no database, StateStore, lifecycle, queue, or execution |
| `inbox_watcher.py` `once`, `run`, `install` | HISTORICAL_ONLY | fail closed with `WATCHER_RETIRED_CENTRAL_LIFECYCLE_REQUIRED` before operational access | retained implementation is unreachable from installed supported ingress |
| `submission_service.submit_legacy_file` | HISTORICAL_ONLY | direct test/provenance helper only | no script or supported runtime route reaches it |

There are no supported ambiguous ingress paths. CENTRAL is the sole queue,
lifecycle, project/repository authorization, retry-semantics, and execution
authority. The only operational database is the installation-owned
`engineering.db`; File Inbox uses durable filesystem acknowledgement, not a
second operational store.

## Installed source canaries

```text
FILE_INGRESS_LOCAL_DB = 0
CLI_INGRESS_LOCAL_DB = 0
HTTP_INGRESS_LOCAL_DB = 0
LOCAL_STATESTORE = 0
SECONDARY_OPERATIONAL_DB = 0
ACTIVE_WATCHER_LIFECYCLE_WRITERS = 0
ACTIVE_WATCHER_STATESTORE = 0
ACTIVE_WATCHER_LOCAL_DB = 0
ACTIVE_DIRECT_RUNNER_INGRESS = 0
SUPPORTED_TRANSPORT_BYPASSES_CANONICAL_SUBMISSION = 0
AMBIGUOUS_TRANSPORT_AUTHORITY = 0
OPERATIONAL_DATABASE_COUNT = 1
```
