# P-TRANSPORT authority map

**Status:** canonical-ingress closure evidence

| Ingress | Classification | Normalization and destination | Prohibited authority / evidence |
| --- | --- | --- | --- |
| HTTP `POST /v1/projects/{project}/submissions` | THIN_TRANSPORT | `request_from_mapping` → `submission_service.submit` in the Server CENTRAL database | no retry, queue, lifecycle worker, or execution call |
| `engineering-platform submit` | THIN_TRANSPORT | parses local prompt/constraints → authenticated HTTP endpoint → same service | no SQLite import or direct CENTRAL-table access |
| Server-owned File Inbox | SERVER_INTERNAL_THIN_TRANSPORT | structured `.json` directly, or Human Intent `.md`/`.txt` through `submission-intake-v1` → bounded in-process `FILE_INBOX` principal → `request_from_mapping` → `submission_service.submit` | only external caller authentication is bypassed; project/repository, mode/Genesis, idempotency, admission and lifecycle validation remain mandatory; no credential, HTTP endpoint, database, StateStore, lifecycle, queue, execution, CWD or repository inference |
| Server-owned Dependabot producer | SERVER_INTERNAL_THIN_TRANSPORT | verified GitHub Dependabot PR → CENTRAL `ExternalProducerBinding` resolution → bounded `DEPENDABOT` principal → `submission_service.submit` | only external caller authentication is bypassed; the binding is identity context, not authorization; no local mapping, Git remote/CWD/default inference, credential, HTTP endpoint, database, queue or execution authority |
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

Without a selected project, the Server supplies a minimal `PLATFORM` Console
snapshot solely to hydrate that shared read-only Console surface. It contains
no project queue, run, telemetry, or lifecycle data; project-only endpoints
remain unavailable until the operator selects a project.

| Card | Healthy vocabulary | Bounded information shown | Explicitly excluded |
| --- | --- | --- | --- |
| HTTP/API ingress | `HEALTHY`, `DEGRADED`, `DOWN` | listener/endpoint, useful protocol/runtime version, most recent successful submission, bounded error | queue, run, execution or CENTRAL retry state |
| CLI ingress | `AVAILABLE`, `DEGRADED`, `UNAVAILABLE` | canonical-submission compatibility, useful CLI/runtime version, most recent successful CLI submission, bounded error | daemon/running claim and lifecycle state |
| File Inbox ingress | `RUNNING`, `NOT_READY`, `DEGRADED`, `STOPPED` | adapter heartbeat, watched location, submission-readiness, most recent submission, ingress-delivery retry, quarantine count, bounded error | CENTRAL execution/run retry or lifecycle state |

File Inbox retry information denotes delivery from the ingress adapter to the
canonical Server admission application service only. It is deliberately not an execution
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

### Installed 3×2 ingress qualification

`tools/qualification/p_transport_installed_ingress_matrix.py` is the permanent
installed integration gate (`npm run test:p-transport-ingress-matrix`).  It
builds the candidate wheel, installs it into a temporary virtual environment,
and uses only public boundaries: HTTP, the installed CLI executable and a
physical File Inbox input file.  For each MANAGED and GENESIS submission it
records the canonical submission identifier and the lifecycle run identifier.
The worker reaches `RUNNING` before the qualification-only provider stop;
provider, checkout and PR side effects are intentionally outside this ingress
gate.  CI executes the target in Engineering Platform validation.

The same executable also qualifies the Server-owned durability windows (down
before claim, after claim, after CENTRAL acceptance and empty restart), the
HTTP/CLI/File Inbox negative matrix, storage authority and the Human Intent
File Inbox 2× matrix.  Human files retain original intent in the canonical
prompt, record normalization `submission-intake-v1`, and require explicit
project, repository, mode and (for Genesis) target metadata.  See
[`HUMAN_INTENT_FILE_INBOX.md`](HUMAN_INTENT_FILE_INBOX.md) for the public
operator contract and historical-capability replacement map.

File Inbox is an EP Server-owned ingress.  When the Server is stopped, no
ingress claimant is active: files remain in `incoming/` and are delivered only
after Server restart.  Its durable input/archive files, bounded quarantine
receipts and secret-free heartbeat are transport evidence, never Actions,
runs or an operational database.  Transport durability is therefore distinct
from independent transport availability.

### File Inbox internal-principal security decision

`FILE_INBOX` is a Server-owned in-process principal, not an external consumer.
It has **no** bearer credential, project-token store, internal HTTP route or
persisted secret. The Server passes a bounded in-process admission callback to
its child adapter. That callback invokes the same canonical submission
application service as authenticated HTTP and CLI after those transports have
completed caller authentication. It cannot infer a project from a folder,
checkout, CWD or default; each physical envelope still supplies an explicit
project and repository and CENTRAL validates their active binding.

The installed matrix proves project A/repository A and project B/repository B
admit through the same Server-owned Inbox, while project A/repository B and an
unknown project quarantine without an Action or run. The matrix process has no
`EP_CONSUMER_TOKEN` for this File Inbox canary.

```text
FILE_INBOX_EXTERNAL_CREDENTIAL = NONE
FILE_INBOX_PROJECT_TOKEN_STORE = NONE
FILE_INBOX_INTERNAL_HTTP_BYPASS = NONE
FILE_INBOX_USES_CANONICAL_ADMISSION = TRUE
FILE_INBOX_BYPASSES_ADMISSION_VALIDATION = FALSE
FILE_INBOX_MULTI_PROJECT_AUTHORITY = PASS
CROSS_PROJECT_FILE_INBOX_SUBMISSION = 0
```

### External Producer Binding / Dependabot security decision

`ExternalProducerBinding` is a CENTRAL-owned registry with the unique external
key `(producer_type, external_resource_type, normalized_external_resource_identity)`.
It resolves only to an existing active project and a repository canonically
registered to that project. The initial bounded producer is `DEPENDABOT` with
the normalized GitHub `owner/repository` identity. The identity is normalized
for case, HTTPS/SSH form, `.git` and trailing slash; it is never read from a
checkout or Git remote.

Bindings are managed solely through the Server's local installation-owner
administration commands (`register-producer-binding`, `list-producer-bindings`
and `deactivate-producer-binding`). The actor is derived from ownership of the
private Server data root; it is not supplied by a browser, a project consumer
or an arbitrary CLI argument. Registration and deactivation record immutable
CENTRAL audit evidence. The registry contains no consumer token, producer
credential or project-token map.

The Server-owned Dependabot child observes only active bindings, validates
GitHub Dependabot PR metadata, resolves a binding and invokes the same
`submission_service.submit` call as the other ingress paths. It therefore
bypasses only irrelevant external caller authentication. Project/repository
validation, mode/Genesis validation, idempotency, admission, provenance and
lifecycle initialization remain canonical. Its heartbeat is observation state,
not a discovery cursor, queue, Action or run authority.

If a binding changes after a PR head was admitted, that historical submission
retains its original binding id/version in canonical constraints. Rediscovery
of that same repository/PR/head under a different target fails closed with
`BINDING_DRIFT_REQUIRES_NEW_HEAD`; a later head SHA is a distinct immutable
delivery identity and is evaluated using the current active binding.

```text
FILE_INBOX_EXTERNAL_CREDENTIAL = NONE
FILE_INBOX_PROJECT_TOKEN_STORE = NONE
FILE_INBOX_INTERNAL_HTTP_BYPASS = NONE
FILE_INBOX_USES_CANONICAL_ADMISSION = TRUE
FILE_INBOX_BYPASSES_ADMISSION_VALIDATION = FALSE

EXTERNAL_PRODUCER_BINDING_MODEL = DEFINED
INTERNAL_PRODUCER_IDENTITY_MODEL = DEFINED
PRODUCER_BINDING_OPERATIONAL_AUTHORITIES = 1
LOCAL_PRODUCER_BINDING_AUTHORITY = 0
PRODUCER_BINDING_BYPASSES_ADMISSION = FALSE
INTERNAL_PRODUCER_USES_CANONICAL_ADMISSION = TRUE
INTERNAL_PRODUCER_DIRECT_DB_ADMISSION = FALSE
```

```text
P_TRANSPORT_INGRESS_MATRIX = PASS
SAME_CANONICAL_SUBMISSION_SERVICE = PASS
SAME_CENTRAL_ADMISSION_MODEL = PASS
SAME_LIFECYCLE_INITIALIZATION_MODEL = PASS
DIRECT_RUNNER_BYPASS = 0
P_TRANSPORT_FILE_DURABILITY_GATE = PASS
P_TRANSPORT_NEGATIVE_INGRESS_GATE = PASS
P_TRANSPORT_STORAGE_AUTHORITY_GATE = PASS
P_TRANSPORT_FILE_HUMAN_2X = PASS
HUMAN_INTENT_FAIL_CLOSED = PASS
```

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
