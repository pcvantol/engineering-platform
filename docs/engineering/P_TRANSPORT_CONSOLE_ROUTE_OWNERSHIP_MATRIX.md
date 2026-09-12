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
| PLATFORM | `GET /api/components/{ep_server,platform_database,lifecycle_worker,operations_console,dashboard_relay,http_ingress,cli_ingress,file_inbox_ingress,dependabot_producer}/details`, `GET /api/logs/all`, `GET /api/logs/{component}`, `POST /api/logs/all`, `POST /api/audit/user-action`, `/api/{process-metrics,usage}` | Platform Components, status popout, one CENTRAL log projection (server-filtered by EP-component, time, text, level, event, sort and page), controlled log clearing and bounded redacted Console-action audit, File Inbox and Dependabot producer projection |
| PLATFORM | `GET /api/provider-login-status`; `POST /api/provider-login/{repair,logout}` | Provider status and login actions |
| PLATFORM | `GET /api/execution-runtime-status`; `POST /api/execution-runtime/repair` | Execution runtime status and repair |
| PLATFORM | `GET /api/provider-capacity`, `/api/github-rate-limit`; `GET/POST /api/provider-capacity/configuration` | Provider capacity/readiness diagnostics and settings |
| PLATFORM | `GET/POST /api/configuration`; `GET /api/central-data/export`; `POST /api/central-data/{relocate,relocate/browse,relocate/discard,import}`; `GET/POST /api/central-database/configuration` | Server settings and Central data operations; paths and imported bytes are never placed in the audit log |
| HOST_ADMIN | `GET /api/host-admin/diagnostics` | Bounded installation-only disk and managed-runtime observation; no project, queue, execution or mutation authority |
| PLATFORM | `GET /v1/operations/projects` | Operations Console platform listing |
| PROJECT | `GET /api/prompt-history`, `/api/prompt-history/{run}/{report,analysis,chat,details}`, `/api/telemetry/{date}`, `/api/execution-diagnostic/current`; `POST /api/prompt-history/{run}/analysis-retry`, `/api/telemetry/clear`, `/api/execution-{dismiss,retry}`, `/api/dashboard-translate` | Project history, active redacted diagnostic, centrally indexed advisory analysis, telemetry and project actions; no valid selected project returns `409 CONSOLE_PROJECT_UNAVAILABLE` |
| TRANSPORT_INTERNAL | `/diagnostics/topology`, `/healthz`, `/readyz`, `/v1/projects/{project}/submissions`, `/v1/agent/{pair,register,heartbeat,attachment}` | Transport probes and authenticated transport endpoints, not Console delegation |
| HISTORICAL_UNREACHABLE | `POST /api/runtime-directory/open` | Explicitly retired checkout-bound runtime action (`410 RUNTIME_DIRECTORY_RETIRED`) |

## Enforced invariants

- `PLATFORM_ROUTE(<geen>) == PLATFORM_ROUTE(selected_project)` for authority and health semantics. The selected project can only affect documented display context.
- A Platform component family has one scope across status, detail, repair/action, restart (where present), and diagnostics/logs: `COMPONENT_ROUTE_SCOPE_CONSISTENT=PASS`.
- Project routes fail closed without a valid project identity; they do not inherit a first checkout.
- The Host Admin diagnostic resolves before project selection and receives only the explicit EP Server installation root. It neither accepts a filesystem target nor exposes Finder, worktree, Git-lock or arbitrary-command actions.
- `tools/qualification/console_route_ownership_guard.py` runs in normal validation and emits `PLATFORM_ROUTE_PROJECT_DELEGATION=0`, `PLATFORM_ROUTE_CHECKOUT_DEPENDENCY=0`, and `AMBIGUOUS_ROUTE_OWNERSHIP=0`.

## Removed fallback paths

There are no platform-to-project fallback paths. Provider login repair/logout and execution-runtime repair resolve before project lookup; the retired runtime-directory action is unreachable rather than delegated.

## CENTRAL execution and audit projection

The Console reads the immutable accepted `ep_submissions` record together with
its run/dispatch lineage. A `CLAIMED` or `RUNNING` run is shown only in the
current-execution projection. Only `COMPLETE`, `BLOCKED` and `FAILED` runs are
shown in execution history. The same immutable Producer record supplies the
current card and run-detail values for Producer ID/type/version, submission,
Mission, Engineering Action, correlation and target repository. For Forge, the
accepted versioned provenance also supplies host, mission/intent revisions,
runtime-prompt identifier and digest, and retry correlation when present.

The active card names two deliberately separate EP states: **EP execution
phase** is the state recorded for the Execution Host run; **EP dispatcher
state** is the FIFO dispatch record's orchestration state. They can both be
`RUNNING` during normal operation, but neither is inferred from the other.

The active projection and every terminal history detail include the full
persisted lifecycle path (the read-only step-bubble flow) for that exact run.
The active diagnostic endpoint reads only that run's redacted CENTRAL
component-log diagnostics and responds as `text/plain`; absent, malformed, or
JSON-shaped content becomes the localized unavailable state. Raw JSON error
responses are never Console prose.

The Console never derives a branch, checkout, tracked-file count, Mission
summary or prompt content. Those remain absent until the Execution Host records
run-specific evidence or Forge supplies a separately versioned context.

All lifecycle decisions and phase checkpoints are written to CENTRAL component
logs with the run identifier and an appropriate level. Mutating Console
operations additionally write a bounded `dashboard_action_completed` audit
record. Read/download/copy actions requested by the Console can also be
audited, but the log contains only action, actor, outcome and canonical target
identifiers: no prompt text, AI-chat content, queue reason, local path, import
payload or downloaded bytes.

An advisory AI analysis is a separately indexed, integrity-verified CENTRAL
Markdown artifact for the exact terminal `(project_id, run_id)`. The Console
never falls back to a repository checkout or a legacy local analysis file.
Only the bounded retryable processing states may be regenerated from that
run's CENTRAL report; regeneration records a `dashboard_action_completed`
audit event and cannot change the execution outcome. Document responses that
are absent, malformed, or not Markdown are shown as a localized unavailable
message, never as a JSON payload in the Console.

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
| CENTRAL log query and actions | `tests/engineering/test_server_foundation.py::StandaloneServerFoundationTest.test_central_log_route_filters_sorts_and_paginates_before_responding`; `tests/engineering/dashboard.spec.mjs` | Filters, sort and pagination run in CENTRAL; download keeps active filters and omits page bounds; copy includes structured fields; clear is component-scoped. See `CENTRAL_COMPONENT_LOG_QUERY_CONTRACT.md`. |
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

`engineering_platform.platform_components.PLATFORM_COMPONENTS` is the sole
Server-native component model. It supplies stable identity, product name key,
kind, health group, restart capability, log identity and detail metadata to
the Server status projection. The Console receives that model with
`/api/platform-status`; its cards, status popout and log-component picker do
not carry a second component inventory. Historical log aliases are read-only
migration mappings and never selectable component identities.

The rendered CENTRAL Console structurally removes the retired local
Inbox-watcher location picker, folder chooser and watcher-restart path.
`POST /api/configuration/inbox-location` and `/browse` are explicitly retired
(`410`). The canonical File Inbox scan interval and Open PR polling control
are both placed under **Server settings** inside Configuration; neither is a
project, checkout or selected-project setting. Component logs render one
CENTRAL Platform table; a second watcher/dashboard log card is not emitted in
either no-project or selected-project Console documents.

The installed Server owns one component inventory, used by Platform Components,
the status popout and every component detail modal. It contains `ep_server`
(DAEMON), `platform_database` (STORAGE), `lifecycle_worker`
(IN_PROCESS_COMPONENT), `operations_console` (UI_SERVICE), `dashboard_relay`
(UI_SERVICE), and `http_ingress`, `cli_ingress`, `file_inbox_ingress`
(TRANSPORT). The File Inbox is installation-owned at `<data_root>/file-inbox`
with `incoming`, `processing`, `accepted` and `quarantine` dispositions; its
Server-composed service writes the liveness heartbeat. This is not a watcher,
checkout, selected-project or filesystem-backlog queue model.

## Dashboard relay runtime contract

The Dashboard access path is deliberately narrower than the Server's Platform
Components projection:

```text
Tailnet device → Tailscale IPv4 :8765 → Dashboard relay → 127.0.0.1 EP Server Console
```

The Server's relay component projection proves only this access path. It is
healthy when the loopback Server Console and its relay are healthy; it must neither start
nor require the historical `inbox_watcher` LaunchAgent. A missing watcher is
therefore never a reason for the relay endpoint to return `503`.

File Inbox health, retry state and quarantine count are instead exposed by
the installed EP Server's canonical `file_inbox_ingress` component. That
component is Server-owned and has no independently installed watcher or
daemon. The Dashboard relay has no submission, File Inbox, Action, run or
execution authority.

This separation keeps a successful remote access check from claiming that
the Server itself is running: Server and File Inbox availability remain visible
through the canonical Server component projection.
