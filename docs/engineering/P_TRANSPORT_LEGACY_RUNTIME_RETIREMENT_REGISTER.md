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
| LR-01 | `inbox_watcher.py` retains local queue, preflight, retry, launch-agent and root-state implementation. The canonical lifecycle dispatcher owns run identity and admission evidence and calls the independent host/workspace/capability checks directly; emergency recovery no longer publishes watcher status. The direct Dashboard handler has no watcher import, label, launch/restart path, local retry route or branch/worktree relocation route. A loopback regression proves those retired routes return 404. Reverse-dependency analysis finds no production import: remaining consumers are its historical test suite and a Dependabot-to-old-Inbox helper. | UNRESOLVED | Determine the canonical Dependabot ingress successor, then remove the watcher module, its historical tests and package artifact. |
| LR-10 | `migrate_icloud_archives()` was reachable only through the retired watcher CLI. | MIGRATION_TOOLING_KEEP | Repaired in this audit pass: it now lives in `legacy_inbox_migration.py` with its own test. It moves archive evidence only and cannot admit, queue, dispatch or execute work. |
| LR-02 | `dashboard.py` remains the Server template, asset/provider helper and direct historical handler. `server.py` uses it for assets, HTML and provider operations. The handler's watcher-backed and root-bound mutations were removed; its remaining presentation helpers still require separation from the direct HTTP/service wrapper. | UNRESOLVED | Split canonical Console presentation/provider services from the direct handler, then remove its residual root-bound diagnostic/service wrapper and related tests. |
| LR-03 | `component_logging.py` and `storage.py` retain `inbox`/`dashboard` compatibility identifiers for the direct historical handler. | UNRESOLVED | This audit pass removed all repository-local component-log fallbacks: unavailable CENTRAL logging now emits only a bounded diagnostic; reads, versions and clears neither consult nor create `.engineering/logs`. Remove the remaining historical identifiers with the direct Dashboard/watcher handler; retain only canonical component identities. |
| LR-04 | `dashboard_configuration.py` and root-bound configuration readers remain reachable through retained Dashboard helpers. The selected-project Console event stream also used its root-local interval. | UNRESOLVED | Repaired one supported Server leak in this audit pass: both project and no-project event streams now read `dashboard_stream_interval_seconds` from CENTRAL. The remaining direct Dashboard and legacy platform-API consumers must be removed or separated before this row can close. |
| LR-08 | The generic qualification catalog and coverage contract still treated `inbox_watcher.py` as the inbox sequencing implementation. | RETIRE_AND_REMOVE | Repaired in this audit pass: the catalog, provider-boundary guard and coverage target now point to the Server-owned `file_inbox.py` transport adapter. |
| LR-09 | `_admit_dependabot_pull_requests()` used to discover external PRs, publish an Inbox envelope and record root-bound local admission evidence. Its only caller and its package module are removed. The canonical successor is Server-owned `dependabot_producer.py`, which resolves an explicit CENTRAL `ExternalProducerBinding` and calls `submission_service.submit`. The old schema-29 record remains historical migration evidence inside the already-classified legacy storage layer; no supported caller creates or reads it. | RESOLVED — CANONICAL_SUCCESSOR_PROVEN_AND_LEGACY_RETIRED | Installed two-project, source-validation, replay/restart and negative-binding qualification is in `p_transport_installed_ingress_matrix.py`; `test_dependabot_producer.py` supplies service-level regression coverage. |
| LR-11 | `workspace_inbox_api.py` and `human_text_ingress.py` wrote a root-selected iCloud Inbox, created repository-local submission evidence and named the watcher as lifecycle owner. | RETIRE_AND_REMOVE | Repaired in this audit pass: their only production caller was the retired watcher; the modules and their historical tests were removed. The supported successor is Server-owned `file_inbox.py` plus transport-neutral `submission_intake.py`, documented by `HUMAN_INTENT_FILE_INBOX.md` and qualified through the structured 3×2 and Human Intent 2× gates. `ENGINEERING_INBOX_PROTOCOL.md` and the historical pattern in `forensic_attribution.py` remain extraction/forensic evidence only; neither is package data or an executable runtime caller. |
| LR-12 | `dashboard.py` still contains a historical `dashboard`/`dashboard_relay` inventory for its direct HTTP/LaunchAgent wrapper, while the Server uses `PLATFORM_COMPONENTS`. | UNRESOLVED | This audit pass removed the wrapper's provider-login, execution-runtime and component-restart mutations. Their only supported owner is now the bounded PLATFORM route in `server.py`, with canonical component resolution and CENTRAL logging. The remaining direct Dashboard inventory and HTTP/service wrapper must be extracted or removed before claiming `DUPLICATED_COMPONENT_INVENTORIES=0` or `USER_MUTATION_WITHOUT_REAL_SERVER_BOUNDARY=0`. |
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
