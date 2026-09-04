# P-TRANSPORT Console route ownership matrix

Status: `P-TRANSPORT AWAITING_HUMAN_UI_REVIEW`

This is the readable form of the executable canonical matrix in
`engineering_platform.console_route_ownership`. Every supported Console or
Console-adjacent API route has exactly one owner. A selected project may add
explicitly documented display context to a Platform projection; it cannot
change authority, select a checkout, or delegate that route.

| Owner | Routes | Component / intent |
| --- | --- | --- |
| PLATFORM | `GET /`, Console assets/icons, `/health`, `/api/platform-status`, `/api/dashboard-snapshot`, `/api/status`, `/api/events` | Console shell and Platform Components projection (selected project is presentation-only) |
| PLATFORM | `GET /api/components/{ep_server,platform_database,lifecycle_worker,operations_console,dashboard_relay,http_ingress,cli_ingress,file_inbox_ingress}/details`, `GET /api/logs/{dashboard,inbox}`, `/api/{process-metrics,usage}` | Platform Components, status popout, logging and File Inbox projection |
| PLATFORM | `GET /api/provider-login-status`; `POST /api/provider-login/{repair,logout}` | Provider status and login actions |
| PLATFORM | `GET /api/execution-runtime-status`; `POST /api/execution-runtime/repair` | Execution runtime status and repair |
| PLATFORM | `GET /api/provider-capacity`, `/api/github-rate-limit`; `GET/POST /api/provider-capacity/configuration` | Provider capacity/readiness diagnostics and settings |
| PLATFORM | `GET/POST /api/configuration`; `GET /api/central-database/download`; `POST /api/central-database/open-directory`; `GET/POST /api/central-database/configuration` | Server settings and Central database operations |
| PLATFORM | `GET /v1/operations/projects` | Operations Console platform listing |
| PROJECT | `GET /api/prompt-history`, `/api/prompt-history/{run}/{report,chat,details}`, `/api/telemetry/{date}`; `POST /api/execution-{dismiss,retry}`, `/api/dashboard-translate` | Project history, telemetry and project actions; no valid selected project returns `409 CONSOLE_PROJECT_UNAVAILABLE` |
| TRANSPORT_INTERNAL | `/diagnostics/topology`, `/healthz`, `/readyz`, `/v1/projects/{project}/submissions`, `/v1/agent/{pair,register,heartbeat,attachment}` | Transport probes and authenticated transport endpoints, not Console delegation |
| HISTORICAL_UNREACHABLE | `POST /api/runtime-directory/open` | Explicitly retired checkout-bound runtime action (`410 RUNTIME_DIRECTORY_RETIRED`) |

## Enforced invariants

- `PLATFORM_ROUTE(<geen>) == PLATFORM_ROUTE(selected_project)` for authority and health semantics. The selected project can only affect documented display context.
- A Platform component family has one scope across status, detail, repair/action, restart (where present), and diagnostics/logs: `COMPONENT_ROUTE_SCOPE_CONSISTENT=PASS`.
- Project routes fail closed without a valid project identity; they do not inherit a first checkout.
- `tools/qualification/console_route_ownership_guard.py` runs in normal validation and emits `PLATFORM_ROUTE_PROJECT_DELEGATION=0`, `PLATFORM_ROUTE_CHECKOUT_DEPENDENCY=0`, and `AMBIGUOUS_ROUTE_OWNERSHIP=0`.

## Removed fallback paths

There are no platform-to-project fallback paths. Provider login repair/logout and execution-runtime repair resolve before project lookup; the retired runtime-directory action is unreachable rather than delegated.

## Test and qualification coverage

The matrix is not documentation-only. The following checks are the normal
qualification contract for this ownership boundary.

| Concern | Evidence | Required result |
| --- | --- | --- |
| Closed, unambiguous matrix | `tests/engineering/test_console_route_ownership.py` | Every representative route resolves to one declared owner. |
| Source ordering and dependency guard | `tools/qualification/console_route_ownership_guard.py --source-root src` | `PLATFORM_ROUTE_PROJECT_DELEGATION=0`; `PLATFORM_ROUTE_CHECKOUT_DEPENDENCY=0`; `AMBIGUOUS_ROUTE_OWNERSHIP=0`; `COMPONENT_ROUTE_SCOPE_CONSISTENT=PASS`. |
| No-project and selected-project integration contexts | `tests/engineering/test_server_foundation.py::ServerFoundationTest.test_root_reuses_historical_console_with_request_scoped_project_selection` | The Platform health, status, component-detail, logging, provider, execution-runtime and configuration routes retain `EP-Console-Route-Owner: PLATFORM` in both contexts and never fail for missing project scope. |
| Project fail-closed boundary | The same integration test | Project history is `409` without project scope; Platform requests are never used as an implicit project fallback. |
| Platform Components and status popout | `tests/engineering/dashboard.spec.mjs` (`shows live platform readiness in the titlebar health indicator`, `keeps platform health authoritative while an execution is active`, `renders canonical platform components in the platform card`, `opens canonical ingress details from the status popout`) | The eight canonical components remain the source for cards, popout and details; an active execution cannot hide an unhealthy Platform component. |
| Browser qualification | `npm run test:engineering-dashboard` and the four `browser-dashboard` CI shards | Console interaction and presentation remain covered in the normal validation profile. |

The browser specification uses one shared canonical eight-component fixture for
these contracts. This keeps the card, popout and ingress-detail tests aligned
with the installed Server inventory instead of reintroducing historical
watcher or execution-host identities through test data.

### Candidate qualification evidence

Qualification is candidate-SHA-specific: every PR update reruns the ownership
guard, focused status-popout browser coverage, all four CI browser shards,
validation, UI localisation, CodeQL, Trusted Delivery and exact-SHA Owner
Authorization. The current candidate's required checks are the authoritative
evidence; this architecture document intentionally does not preserve a stale
commit hash as a substitute. The required human status remains
`P-TRANSPORT AWAITING_HUMAN_UI_REVIEW`.

## Canonical platform component inventory

The installed Server owns one component inventory, used by Platform Components,
the status popout and every component detail modal. It contains `ep_server`
(DAEMON), `platform_database` (STORAGE), `lifecycle_worker`
(IN_PROCESS_COMPONENT), `operations_console` (UI_SERVICE), `dashboard_relay`
(UI_SERVICE), and `http_ingress`, `cli_ingress`, `file_inbox_ingress`
(TRANSPORT). The File Inbox is installation-owned at `<data_root>/file-inbox`
with `incoming`, `processing`, `accepted` and `quarantine` dispositions; its
Server-composed service writes the liveness heartbeat. This is not a watcher,
checkout, selected-project or filesystem-backlog queue model.
