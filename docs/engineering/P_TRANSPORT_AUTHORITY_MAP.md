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

## Console platform projection and operator interaction

The installed Console reads all three ingress states from the Server/CENTRAL
platform-health projection. The cards are `PLATFORM` scoped: they remain
visible without a selected project and after a project is selected. Project
content is rendered separately and cannot replace, filter, or become the
authority for a transport card.

| Card | Healthy vocabulary | Bounded information shown | Explicitly excluded |
| --- | --- | --- | --- |
| HTTP/API ingress | `HEALTHY`, `DEGRADED`, `DOWN` | listener/endpoint, useful protocol/runtime version, most recent successful submission, bounded error | queue, run, execution or CENTRAL retry state |
| CLI ingress | `AVAILABLE`, `DEGRADED`, `UNAVAILABLE` | canonical-submission compatibility, useful CLI/runtime version, most recent successful CLI submission, bounded error | daemon/running claim and lifecycle state |
| File Inbox ingress | `RUNNING`, `DEGRADED`, `STOPPED` | adapter heartbeat, watched location, most recent submission, ingress-delivery retry, quarantine count, bounded error | CENTRAL execution/run retry or lifecycle state |

File Inbox retry information denotes delivery from the ingress adapter to the
canonical Server admission route only. It is deliberately not an execution
retry counter. Detail panels apply the selected Console locale to timestamps
and expose diagnostic identifiers rather than exception text, credentials, or
other secrets.

### Provider readiness handoff

Provider readiness is also a Server/CENTRAL projection. The Console may ask
the local Server to open a provider's explicit interactive login, but it never
receives or stores a token. On macOS the Server activates Terminal before it
dispatches the Codex device-login or GitHub browser-login command. The dispatch
itself is serialized so two AppleScript invocations cannot overlap; there is
intentionally no elapsed-time "login active" lock. The operator can cancel or
close Terminal and immediately retry the same provider or select the other
provider.

The platform projection verifies only host authentication. In particular, it
does not attempt a GitHub repository API call while no project identity has
been selected. Repository authorization is a project-admission concern and is
verified later against the bound canonical repository. This prevents a valid
host login from being misreported as a login failure merely because CENTRAL has
no checkout authority.

When several platform concerns need attention, the Console groups their rows
inside a native expandable disclosure. Refreshing readiness preserves an
operator-expanded group; only a newly appearing group begins collapsed. Row
diagnostics align at the top while the action remains vertically centred,
including when a message wraps on desktop. Narrow layouts stack the action
below the diagnostic to preserve readable text and avoid horizontal overflow.
This preserves a compact overview without removing individual provider actions
or diagnostics.

### Regression evidence

`tests/engineering/test_dashboard.py` verifies token-free projection data,
Terminal activation, repeat dispatch, cross-provider dispatch, and recovery
after a failed dispatch. `tests/engineering/dashboard.spec.mjs` verifies the
expandable attention group, retained expanded state, action centring, and a
second browser repair request after the temporary handoff state clears. The
transport locale and responsive qualification coverage remains in the same
browser specification.

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
