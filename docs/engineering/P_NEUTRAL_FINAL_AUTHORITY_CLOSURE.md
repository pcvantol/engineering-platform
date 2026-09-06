# P-NEUTRAL Final Authority Closure Register

This register classifies residual DJConnect references by their operational
responsibility.  It deliberately does not use a clean string search as a
completion condition.

## Current Engineering Platform authority

| Domain | Current identity | DJConnect current authority |
| --- | --- | --- |
| Service/install/lifecycle | `com.engineeringplatform.dashboard-relay` through the canonical component inventory | None |
| Repair/health | Server relay component resolution | None |
| Submission routes | HTTP JSON, installed CLI, File Inbox → Server → Submission Service → CENTRAL | None |
| Configuration/logging | `ENGINEERING_PLATFORM_*` configuration namespace | None |

`local_api` is retired and is not an installable component, lifecycle target,
repair target, health authority, or supported ingress.  Its retained contract
fixture is not a package entry point.

## Bounded retained platform-reference compatibility

| Source | Classification | Boundary |
| --- | --- | --- |
| `server_relay.py` legacy relay label | `MIGRATION_SOURCE_ONLY` / rollback source | Exact pre-cutover relay recognition and rollback; canonical resolution comes from `dashboard_relay`. |
| `central_store_migration.py` dashboard/inbox labels | `HISTORICAL_ONLY` | Recognition of stale pre-neutral lock evidence; never lifecycle start/stop order. |
| `local_api.py` legacy label | `HISTORICAL_ONLY` | Provenance/regression fixture for a retired service; not an executable or component. |
| `producer.py` predecessor envelope name | `MIGRATION_SOURCE_ONLY` | Immutable predecessor evidence; new emission uses `engineering_platform.producer_submission`. |
| `platform_bootstrap.py` `.djconnect` marker | `MIGRATION_SOURCE_ONLY` | One-way historical workspace-evidence migration to `.engineering`. |

The machine guard at `tools/qualification/p_neutral_authority_guard.py`
counts each retained literal at its exact source boundary.  It rejects new
generic DJConnect services, configuration prefixes, component inventory,
workflow authority, lifecycle ordering, Local API entry points, or a fourth
submission ingress.

## Documentation, provenance, and project identity

Historical audit records, migration receipts, negative fixtures, and links to
the real DJConnect project are retained as `PROVENANCE_ONLY`,
`DOCUMENTATION_HISTORICAL_ONLY`, or `PRODUCT_SPECIFIC_NON_PLATFORM_REFERENCE`.
They are not operated as Engineering Platform identity.  Current operator and
installation documentation uses Engineering Platform terminology.

`DJCONNECT_PROJECT_IDENTITY_ALLOWED = TRUE`

`DJCONNECT_PLATFORM_IDENTITY_ALLOWED = FALSE`

## Separate credential-schema increment

`local_api_credentials` and `local_api_consumer_registrations` are a historical
Local Consumer API database namespace, not DJConnect platform identity and not
a service/ingress authority.  Their neutral schema migration is deliberately
owned by the separate preceding
`P_NEUTRAL_CONSUMER_CREDENTIAL_NAMESPACE_RETIREMENT` increment; it is not
silently folded into this authority closure.

## P-INSTALLER-V1 handoff

`P_INSTALLER_V1_PROFILE = EP_SERVER_ONLY`.  It may install neutral EP Server
components, CENTRAL, HTTP JSON, installed CLI, File Inbox and Server-owned
relay/console lifecycle.  It must not install `com.djconnect.*`, Local Consumer
API, Forge, Workspace, or Project Agent productization.

| P-INSTALLER-V1 boundary | Contract |
| --- | --- |
| Must include | EP Server, CENTRAL, HTTP JSON, installed CLI, File Inbox, Server-owned lifecycle and Server-owned relay/console where applicable. |
| Must exclude | Legacy DJConnect services, Local Consumer API, Forge Runtime, Workspace and Project Agent productization. |
| Project consumption | DJConnect may be attached later as a project; it is never an EP platform component. |

The installed P-TRANSPORT matrix is a real-wheel canary.  It waits a bounded
30 seconds for the two independently bound Server-child Dependabot admissions,
so a hosted SQLite retry cannot be misreported as a missing authority binding.
