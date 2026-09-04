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
| LR-01 | `inbox_watcher.py` retains local queue, preflight, retry and launch-agent implementation. The canonical lifecycle dispatcher no longer imports it: it owns run identity and admission evidence and calls the independent host/workspace/capability checks directly. Canonical emergency recovery also no longer publishes watcher status; only the lazy direct-dashboard compatibility boundary remains. | UNRESOLVED | Migrate the remaining direct-dashboard dependency; then remove watcher runtime, its tests, service installation and package artifact. |
| LR-02 | `dashboard.py` remains the Server template, asset/provider helper and direct historical handler. `server.py` uses it for assets, HTML, provider operations and historical compatibility. | UNRESOLVED | Split canonical Console presentation/provider services from the direct handler; remove root-bound handler and its routes only after callers migrate. |
| LR-03 | `component_logging.py` and `storage.py` retain `inbox`/`dashboard` local-log compatibility identifiers. | UNRESOLVED | Remove local writer/read compatibility once the remaining dashboard/watcher callers are gone; retain only canonical component identities. |
| LR-04 | `dashboard_configuration.py` and root-bound configuration readers remain reachable through retained Dashboard helpers. | UNRESOLVED | Move supported settings to Server configuration or remove their old consumers. |
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
