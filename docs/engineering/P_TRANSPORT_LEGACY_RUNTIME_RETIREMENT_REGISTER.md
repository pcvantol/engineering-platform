# P-TRANSPORT legacy runtime retirement register

**Status:** audit in progress — not completion evidence.

This register applies the consequence-closure rule to the standalone Server
transition.  It is derived from the Phase-P standalone roadmap, ADR-0026,
the P-CENTRAL-CORE authority map, the P-TRANSPORT authority map and the
executable Console route matrix.  Historical ADRs, migration receipts and
forensic evidence are outside this production-runtime inventory.

## Authority transition map

| Legacy responsibility | Previous owner | Current owner | Cutover | Supported old reads/writes | Classification |
| --- | --- | --- | --- | --- | --- |
| Dashboard HTTP route delegation and platform shell | checkout-root Dashboard | EP Server + explicit PLATFORM/PROJECT routes | active | no old route fallback is allowed | ACTIVE_CANONICAL successor; dashboard callers require audit |
| Inbox scan, admission and durable transport disposition | Inbox watcher | Server-owned File Inbox | active | physical transport receipts only | ACTIVE_CANONICAL successor |
| Submission queue, lifecycle, runs and retry truth | watcher/local root | CENTRAL operational store + Lifecycle Worker | active | no local authority | ACTIVE_CANONICAL successor |
| Component identity, health, detail and log choice | Dashboard maps/aliases | `PLATFORM_COMPONENTS` | active | no selectable legacy identity | ACTIVE_CANONICAL successor |
| Component-log persistence | root `.engineering/logs` | CENTRAL `engineering_component_logs` | active | bounded stderr only on persistence failure | ACTIVE_CANONICAL successor |

`AMBIGUOUS_AUTHORITY_TRANSITIONS = 0` for the transition map above.  This
does not yet assert that all old code is removed.

## Material discovery and reachability ledger

| Legacy ID | Finding / reverse dependency | Classification | Required disposition |
| --- | --- | --- | --- |
| LR-01 | `inbox_watcher.py` retained local queue, preflight, retry, launch-agent and root-state implementation. Reverse-dependency analysis after the Dependabot migration found no production import: its only consumers were its historical test suite and the already-retired Dependabot-to-Inbox helper. The canonical lifecycle dispatcher owns run identity/admission evidence; Server-owned File Inbox owns physical transport. | RETIRE_AND_REMOVE | Repaired in this audit pass: the module and its historical runtime tests were deleted; the package/source authority guard now fails if it reappears. `legacy_inbox_migration.py` is the separate, read-only archive migration tool. |
| LR-10 | `migrate_icloud_archives()` was reachable only through the retired watcher CLI. | MIGRATION_TOOLING_KEEP | Repaired in this audit pass: it now lives in `legacy_inbox_migration.py` with its own test. It moves archive evidence only and cannot admit, queue, dispatch or execute work. |
| LR-02 | `dashboard.py` remains the Server template, asset/provider helper and direct historical handler. `server.py` uses it for assets, HTML and provider operations. The handler's watcher-backed and root-bound mutations were removed; its remaining presentation helpers still require separation from the direct HTTP/service wrapper. The browser harness now starts the Server `_HealthHandler` with an explicitly registered CENTRAL fixture project; it no longer starts `DashboardHTTPServer`. Workspace, checkout and provider paths are read-only diagnostics; historical Finder actions and destructive checkout-local branch scanning/cleanup are structurally absent from the supported Console, its templates, assets and direct routes. | UNRESOLVED | Split canonical Console presentation/provider services from the direct handler; migrate the remaining shared dashboard asset to a native Server Console client; then remove the residual root-bound diagnostic/service wrapper. The Server-native harness and source guard prevent reintroducing the direct listener, local Finder routes or local branch-cleanup controls. |
| LR-03 | `component_logging.py` and `storage.py` retain `inbox`/`dashboard` compatibility identifiers for the direct historical handler. | UNRESOLVED | This audit pass removed all repository-local component-log fallbacks: unavailable CENTRAL logging now emits only a bounded diagnostic; reads, versions and clears neither consult nor create `.engineering/logs`. The final active writers now use `operations_console` and `file_inbox_ingress`; the remaining historical identifiers are read/route compatibility owned solely by the direct Dashboard wrapper. Remove those aliases with that wrapper; retain only canonical component identities. |
| LR-04 | `dashboard_configuration.py` and root-bound configuration readers remain reachable through retained Dashboard helpers. The selected-project Console event stream also used its root-local interval. | UNRESOLVED | Repaired one supported Server leak in this audit pass: both project and no-project event streams now read `dashboard_stream_interval_seconds` from CENTRAL. The remaining direct Dashboard and legacy platform-API consumers must be removed or separated before this row can close. |
| LR-08 | The generic qualification catalog and coverage contract still treated `inbox_watcher.py` as the inbox sequencing implementation. | RETIRE_AND_REMOVE | Repaired in this audit pass: the catalog, provider-boundary guard and coverage target now point to the Server-owned `file_inbox.py` transport adapter. |
| LR-09 | `_admit_dependabot_pull_requests()` used to discover external PRs, publish an Inbox envelope and record root-bound local admission evidence. Its only caller and its package module are removed. The canonical successor is Server-owned `dependabot_producer.py`, which resolves an explicit CENTRAL `ExternalProducerBinding` and calls `submission_service.submit`. The old schema-29 record remains historical migration evidence inside the already-classified legacy storage layer; no supported caller creates or reads it. | RESOLVED — CANONICAL_SUCCESSOR_PROVEN_AND_LEGACY_RETIRED | Installed two-project, source-validation, replay/restart and negative-binding qualification is in `p_transport_installed_ingress_matrix.py`; `test_dependabot_producer.py` supplies service-level regression coverage. |
| LR-11 | `workspace_inbox_api.py` and `human_text_ingress.py` wrote a root-selected iCloud Inbox, created repository-local submission evidence and named the watcher as lifecycle owner. | RETIRE_AND_REMOVE | Repaired in this audit pass: their only production caller was the retired watcher; the modules and their historical tests were removed. The supported successor is Server-owned `file_inbox.py` plus transport-neutral `submission_intake.py`, documented by `HUMAN_INTENT_FILE_INBOX.md` and qualified through the structured 3×2 and Human Intent 2× gates. `ENGINEERING_INBOX_PROTOCOL.md` and the historical pattern in `forensic_attribution.py` remain extraction/forensic evidence only; neither is package data or an executable runtime caller. |
| LR-12 | `dashboard.py` still contains a historical `dashboard`/`dashboard_relay` inventory for its direct HTTP/LaunchAgent wrapper, while the Server uses `PLATFORM_COMPONENTS`. | UNRESOLVED | This audit pass removed the wrapper's provider-login, execution-runtime and component-restart mutations. Their only supported owner is now the bounded PLATFORM route in `server.py`, with canonical component resolution and CENTRAL logging. The remaining direct Dashboard inventory and HTTP/service wrapper must be extracted or removed before claiming `DUPLICATED_COMPONENT_INVENTORIES=0` or `USER_MUTATION_WITHOUT_REAL_SERVER_BOUNDARY=0`. |
| LR-13 | `ENGINEERING_PLATFORM_VERSION.json` still carries `watcher_version`, consumed by `platform_version.py` as part of the published manifest compatibility shape. It does not load, start, package or advertise a watcher runtime. | INTENTIONAL_COMPATIBILITY_KEEP | Keep the inert manifest field until a governed manifest-major compatibility change. Owner: platform manifest contract. Non-authority proof: no runtime import or component projection reads it. |
| LR-14 | `_console_project_boundary()` injects the CENTRAL-selected project into the transitional dashboard JavaScript fetch/EventSource calls. | INTENTIONAL_COMPATIBILITY_KEEP | Owner: Server Console migration. Purpose: the shared dashboard asset has not yet gained a native CENTRAL project-scope client. Non-authority proof: it receives the selected project only from the Server-rendered document; every request is revalidated by the Server against CENTRAL registration, and it cannot inspect a checkout, CWD, selected local root or default project. Retirement condition: migrate the shared dashboard asset and browser fixture to a native Server Console client, then remove the request wrapper. |
| LR-05 | `central_store_migration.py`, forensic attribution and ADR/receipt material mention watcher/database paths. | MIGRATION_TOOLING_KEEP / HISTORICAL_EVIDENCE_KEEP | Retain only as read-only migration/forensic evidence; exclude from installed runtime surface. |
| LR-06 | `/diagnostics/topology` formerly served a second hard-coded Dashboard. | RETIRE_AND_REMOVE | Repaired in `75dd4b2`: now JSON-only transport diagnostic; the old helper is pending physical deletion. |
| LR-07 | Browser log state held `inbox` and `dashboard` buffers after the one-table migration. | RETIRE_AND_REMOVE | Repaired in `5299831`; regression logic remains model-driven. |

## Initial successor assumptions

| Successor | Assumption produced by P-TRANSPORT | Evidence / state |
| --- | --- | --- |
| P-QUEUE | CENTRAL is the only submission queue and lifecycle authority. | `P_TRANSPORT_AUTHORITY_MAP.md`; installed ingress matrix. |
| P-NEUTRAL | Transport adapters have no execution authority. | File Inbox internal-principal and durability qualification. |
| P-INSTALLER | File Inbox is a Server child; no standalone File Inbox service is installed. | package-entrypoint and installed-wheel qualification remain to be re-run after retirement. |
| P-RELEASE | one Server operational database and explicit route/component topology. | route guard and storage gate; exact-head reinstall remains required. |

No removal is authorized from this register while its classification is
`UNRESOLVED`.  The next audit pass must turn every material row into one of
the permitted terminal classifications with exact callers and installed-wheel
evidence.

## Closure decisions: host boundary, data and configuration

These decisions are normative for the remaining LR-02/LR-03/LR-04/LR-12
work.  They prevent the direct wrapper from being retired by either silently
retaining a checkout authority or deleting a useful host capability without a
replacement decision.

### Host Admin classification

The following root-local capabilities are **not** Console, CENTRAL or Project
authority.  They are classified pending a dedicated Server-installed Host
Admin surface.  They have no supported direct-dashboard route or mutation
until that surface, its authorization model and qualification exist.

| Capability | Classification | Target owner | Removal rule |
| --- | --- | --- | --- |
| Git index-lock diagnosis and proven-stale lock recovery | `HOST_ADMIN_DIAGNOSTIC` / `HOST_ADMIN_ACTION` | installation-owned Server Host Admin service | retain source only while explicitly quarantined; do not expose through the Console wrapper |
| Local worktree inventory and safe worktree removal | `HOST_ADMIN_DIAGNOSTIC` / `HOST_ADMIN_ACTION` | installation-owned Server Host Admin service | no deletion until a target contract proves root allowlisting and non-project authority |
| Disk, runtime and Codex-installation diagnostics | `HOST_ADMIN_DIAGNOSTIC` | installation-owned Server Host Admin service | no checkout/CWD/default-project inference in the successor |
| Local report, chat and workspace diagnostics | `HOST_ADMIN_DIAGNOSTIC` | installation-owned Server Host Admin service or `HISTORICAL_EVIDENCE_KEEP` per dataset | classify every input dataset before extraction/removal |

`HOST_ADMIN_*` is deliberately not an execution authority: it cannot admit a
submission, select a project/repository, access CENTRAL lifecycle state as a
writer, or infer a project from a root, checkout, Git remote, directory name,
selected project or default project.

### Local dataset inventory

| Dataset family | Classification | Authoritative owner / disposition |
| --- | --- | --- |
| `engineering.db`, component logs, submissions, Actions, runs, retries and lifecycle records | `SERVER` | installation-owned CENTRAL database; no repository-local fallback or second store |
| File Inbox `incoming`, `processing`, `accepted`, `quarantine` and heartbeat | `INSTALLATION` | Server-owned transport durability evidence, not a queue or lifecycle store |
| Server `server.json`, runtime identity/runtime files and relay binary | `INSTALLATION` | Server data root only |
| Explicitly registered project/repository bindings and project reports/history | `PROJECT` | CENTRAL registration and project projection; never discovered from a local root |
| Legacy root `.engineering` reports, chats, workspace snapshots and run JSON | `HISTORICAL` | read-only migration/forensic evidence until each reader is replaced or retired |
| Git-lock, worktree, disk and installed-runtime observations | `INSTALLATION` | Host Admin diagnostic inputs; no implicit project scope |

### Configuration ownership

| Configuration family | Owner | Constraint |
| --- | --- | --- |
| listener host/port, managed Codex executable prefix, relay lifecycle and File Inbox location | `INSTALLATION` | Server data root; installation administration only |
| scan, health, details and provider-readiness intervals; log level/retention; Codex capacity reserve | `SERVER` | stored in CENTRAL installation metadata; never root/dashboard preferences |
| project/repository bindings and project execution policy | `PROJECT` | explicit CENTRAL registration only |
| former root `dashboard_configuration.*` keys and legacy root configuration files | `HISTORICAL` | compatibility read only during wrapper extraction; no new root-local write authority |

The names `dashboard` and `inbox` remain temporary read/route compatibility
aliases solely for the direct-wrapper extraction.  They are neither selectable
components nor log writers; their retirement condition is removal of that
wrapper plus an installed-wheel absence check.

## LR-02 direct-route consequence map

The historical direct handler is not a supported route owner.  The following
map prevents a coverage repair from silently preserving it or deleting an
unreplaced capability.

| Historical route family | Canonical successor / disposition | State |
| --- | --- | --- |
| Provider login, provider logout, provider repair and execution-runtime repair | Server PLATFORM routes; direct mutations removed | RETIRED_FROM_DIRECT_HANDLER |
| Component restart | Server PLATFORM route using `PLATFORM_COMPONENTS`; direct mutation removed | RETIRED_FROM_DIRECT_HANDLER |
| Console HTML, assets, platform status, canonical component logs and Server settings | Server Console and explicit PLATFORM routes | SUCCESSOR_EXISTS; direct read wrapper pending removal |
| Project runs, reports, details and chat history | Server selected-project routes backed by CENTRAL projections | SUCCESSOR_EXISTS; direct read wrapper pending removal |
| Root-local telemetry, worktree, report, chat, Codex-update and workspace diagnostics | No canonical successor is yet established for each individual capability | DO_NOT_DELETE_UNTIL_CLASSIFIED |
| Historical Dashboard LaunchAgent and its relay installer | Relay remains an access adapter, but its lifecycle must move to Server-owned installation infrastructure | SERVER_INFRASTRUCTURE_EXTRACTION_REQUIRED |

The final two rows are explicitly not compatibility exemptions.  They are the
remaining LR-02 consequence-closure work and must be resolved before the
direct handler or its package/runtime surface can be declared retired.
