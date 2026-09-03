# Phase P — migration-gaps register

**Status:** authoritative Phase-P migration completion gate for PR #23 and its follow-up increments

**Last reviewed:** 2026-09-02
**Scope:** the installed standalone EP Server, Operations Console, CENTRAL
project projection, retained historical-parity implementation, and the
installation runtime.

## Purpose and reading rule

This register records the audits performed while restoring Phase-P functional
parity.  It prevents a presentation fix, a CENTRAL overlay, or a retained
historical adapter from being mistaken for completion of the standalone
migration.

This register is the authoritative Phase-P migration completion gate.
Functional-parity receipts, green pull requests, Console-parity receipts and
transition-baseline qualifications do not supersede unresolved **ACTIVE GAP**
entries.  PR #23 is a transition/consolidation baseline, not completion of the
CENTRAL cutover.

The target architecture is deliberately simple:

```text
CENTRAL (one EP-owned database, installation scope)
    ├── platform/provider state and policy
    ├── project registrations and local execution bindings
    └── project-scoped submissions, FIFO lanes, leases, runs and evidence
             ↓
validated project binding / Project Agent execution
             ↓
local checkout and worktree (physical execution only)
```

A local checkout is permitted as a physical execution binding.  It is never a
second authority for queue, run state, history, configuration, logs,
telemetry, provider usage, downloads, or project identity.  Historical
DJConnect records may remain as read-only provenance, but must not be on a
standalone product path.

The non-negotiable operational-authority target is:

```text
OPERATIONAL_DATABASE_COUNT = 1
CANONICAL_OPERATIONAL_DATABASE = <installation data root>/engineering.db
ACTIVE_REPOSITORY_LOCAL_DB_AUTHORITY = 0
ACTIVE_LOCAL_STATESTORE_AUTHORITY = 0
ACTIVE_SECONDARY_SERVER_DATABASE_AUTHORITY = 0
```

The rows below use these dispositions:

- **REPAIRED IN #23** — the live Server route now uses CENTRAL for this
  concern; regression evidence belongs to the named tests.
- **ACTIVE GAP** — reachable standalone behaviour still has a local-root or
  historical-authority dependency and must be replaced, not merely hidden.
- **RETIRED / HISTORICAL** — retained only as explicitly isolated forensic or
  provenance material; it is not an operational dependency.

## Audit results

| ID | Area and finding | Disposition | Root cause and required completion evidence |
| --- | --- | --- | --- |
| MG-01 | **Per-project FIFO queue.** The old local Inbox status exposed `queue_depth: 0` although CENTRAL contained admitted `QUEUED` work. | **REPAIRED IN #23** | The Console queue projection now reads CENTRAL submissions and is selected-project scoped.  Keep a two-project test proving that a queued item for project A is invisible for project B and that only one item from each project lane is claimed at a time. |
| MG-02 | **Project identity and workspace label.** A selected CENTRAL project could show a package/default local-root name such as `djconnect`. | **REPAIRED IN #23** | The selected CENTRAL project identity is the display authority; the local root is an internal binding.  Keep a regression test whose project label differs from the checkout directory name. |
| MG-03 | **No-project projection.** `<geen>` must retain platform cards but must not leak a default/first project. | **PARTIALLY REPAIRED IN #23** | The selector and no-project banner exist, and provider capacity is available without selection.  The retained health tooltip still combines a root-derived `latestStatus` with global component health.  It must hide project execution, queue, watcher-state and workspace rows at `<geen>`, or show an explicitly labelled all-project aggregate. |
| MG-04 | **Provider capacity and admission reserve.** Codex account limits were visually live but their history and reserve policy were root/project scoped. | **REPAIRED IN #23** | Capacity history and `codex_capacity_reserve_percent` are now CENTRAL platform metadata, exposed by `/api/provider-capacity`; worker admission resolves the same Server data root.  Keep tests for no-project visibility, cross-project policy equality and child-process inheritance. |
| MG-05 | **Managed Codex CLI location.** Diagnostics could show a temporary-process `HOME` path while the server used the installed CLI elsewhere. | **PARTIALLY REPAIRED IN #23** | `managed_codex_cli_prefix` is installation configuration and is passed to the active Server/worker/child path. Historical independently launched watcher, dashboard and direct entrypoints are not yet proven to consume the same typed configuration; `DJCONNECT_ENGINEERING_CODEX_EXECUTABLE` remains dead configuration. Do not reintroduce `Path.home()` as a managed-CLI locator. |
| MG-06 | **Local and secondary database authority.** Historical `storage.database_path(root)` can resolve `root/.engineering/engineering.db`; root-based helpers still call `open_storage(root)`. A possible `ep-server.db` installation database must also be disproven or consolidated. | **REPAIRED IN P-CENTRAL-CORE** | Active lifecycle composition now supplies an explicit CENTRAL binding; retained local resolvers are exact-path historical/forensic compatibility only. The source authority guard, fresh Managed and fresh Genesis canaries prove no repository-local or secondary operational SQLite database is created. |
| MG-07 | **Historical dashboard delegation.** The Server delegates most routes to `dashboard.handler(root, ...)`; CENTRAL validates the root but does not own every displayed datum. | **ACTIVE GAP — P0** | `server._delegate_dashboard` remains a compatibility boundary, not a CENTRAL-native projection service.  Replace its status, history, configuration, log, report/download and action routes incrementally.  No-project routes must never select a first bound root as a hidden data source. |
| MG-08 | **Run state, lease, retry and recovery.** The retained lifecycle uses `.engineering/engineering-runs` through `StateStore`; watcher and recovery logic read/write it. | **REPAIRED IN P-CENTRAL-CORE** | Checkpoints, lease/retry/recovery, merge waits and finalization are CENTRAL-owned. The supported watcher path is retired before storage access; same-run operator-merge resume preserves the one-run-per-project ownership guard. |
| MG-09 | **Telemetry.** `telemetry.py` persists run rows, daily aggregates, phase spans and a terminal outbox through local storage. | **REPAIRED IN P-CENTRAL-CORE** | Active execution timing, provider usage, phase spans and terminal telemetry use the explicit CENTRAL binding. The retained watcher chain is historical compatibility only and is source-guarded. |
| MG-10 | **Prompt history, chat, provider usage and reports.** These retained routes read or write root-based historical tables/files. | **PARTIALLY REPAIRED IN P-CENTRAL-CORE** | Active lifecycle history, provider usage, reports and immutable artifact indexes are CENTRAL-indexed and survive rebind/delete-checkout. Presentation/download/chat routes retained behind the historical Dashboard boundary remain P-CENTRAL-CONSOLE work; they may not fall back to repository-local canonical data. |
| MG-11 | **Configuration, logs and database maintenance.** Historical dashboard configuration and component logs use root-local `engineering_metadata`/logs; previous database maintenance targeted that store. | **PARTIALLY REPAIRED IN #23** | CENTRAL database maintenance and provider-capacity reserve were moved.  Remaining platform settings, project settings, component logs and their downloads require explicit scope and CENTRAL backing.  The legacy maintenance surface must be removed or quarantined, never silently invoked. |
| MG-12 | **Inbox transport.** The historical Inbox watcher keeps local transaction/status/retry state, so it is more than a transport adapter. | **ACTIVE GAP — P1** | File, CLI and HTTP may be ingress transports only.  They must normalize into CENTRAL submissions, with CENTRAL owning queue position and lifecycle.  Retire local watcher run state and prove an inactive legacy watcher cannot change CENTRAL lifecycle truth. |
| MG-13 | **Legacy names and service topology.** Product code and service definitions retain `DJCONNECT_*` variables and `com.djconnect.*` LaunchAgent labels. | **ACTIVE GAP — P1** | Some mentions are valid archival provenance; active environment variables, labels, defaults and CI conditions are not.  Rename or retire active surfaces and add a source/packaging allowlist that permits only clearly marked historical evidence. |
| MG-14 | **Release/update path.** Runtime health polling exists, but no authoritative signed EP release source, manifest, checksum, attestation, controlled installation or rollback flow exists. | **ACTIVE GAP — P1** | Do not implement a generic Python/Homebrew update button.  First establish the EP wheel release channel, signed/checksummed manifest and compatibility contract; then implement quiesce, atomic upgrade, preflight and rollback. |
| MG-15 | **Status-card scope.** Platform health is mixed with a selected-root execution snapshot. | **ACTIVE GAP — P1** | Dashboard/relay/Server health are platform scoped.  Queue, execution, watcher state and workspace are project scoped.  Render separate platform and project blocks; selection absence must be an intentional projection, not empty-looking root data. |
| MG-16 | **Provider timeouts and cancellation.** Managed review could remain non-terminal while successor work became eligible. | **REPAIRED IN #23, CONTINUING ASSURANCE** | The timeout policy is now visible/read-only per workflow step and an operator cancellation path reaches a failed terminal state with cleanup.  Keep concurrency tests that reject a successor while its predecessor has any non-terminal state, including review/timeout recovery. |

## Evidence inventory

The detailed P-CENTRAL-CORE source inventory is maintained in
[`P_CENTRAL_CORE_AUTHORITY_MAP.md`](P_CENTRAL_CORE_AUTHORITY_MAP.md).  It is a
pre-cutover map, not closure evidence: entries remain ACTIVE until the
installed-wheel and no-local-authority qualification gates pass.

The audits inspected the Server boundary, CENTRAL database layer, retained
dashboard handler and browser assets, lifecycle dispatcher, watcher, storage,
telemetry, provider/preflight code, service launch configuration, CI and current
operator documentation.  The principal active source groups are:

- `server.py`, `central_database.py`, `parity_context.py` and
  `parity_lifecycle_dispatcher.py`;
- `dashboard.py`, `dashboard_configuration.py`, `component_logging.py` and
  `assets/dashboard.js`;
- `storage.py`, `execution_host.py`, `inbox_watcher.py`, `telemetry.py`,
  `prompt_history.py`, `codex_chat.py` and `provider_usage.py`;
- recovery/lease/usage helpers and active LaunchAgent/CI configuration.

The historic cutover machinery in `central_store_migration.py`, older receipts
and source-provenance documents are not themselves defects when isolated and
read-only.  They become an active gap if they are imported by a standalone
runtime path or presented as the new CENTRAL operational store.

## Required increment order

1. **P-CENTRAL-CORE — close authority leakage:** remove root-local database
   fallback and any secondary operational database from all installed runtime
   paths; establish CENTRAL repositories for the lifecycle, StateStore,
   telemetry and operational evidence/history/provider usage.
2. **P-CENTRAL-CONSOLE — close projection leakage:** replace historical
   dashboard delegation with CENTRAL-native status, history, configuration,
   logs, downloads and explicit no-project/platform projections.
3. **P-TRANSPORT — close ingress leakage:** reduce watcher state to transport
   normalization and CENTRAL submission ingress.
4. **P-QUEUE — close execution leakage:** migrate run state, leases, recovery and
   finalization before allowing broader scheduling or concurrency claims.
5. **P-NEUTRAL / runtime-authority consolidation:** retire active DJConnect
   service identities and converge supported entrypoints on runtime authority.
6. **P-RELEASE:** only after a signed wheel release contract exists may EP
   expose runtime-update availability or an update action.
7. **Phase-P parity re-audit**, then **P-D Managed / Genesis / armed-retry
   Goldens**; only later may Phase S and B9 be considered.

## Exit criteria

This register may be closed only when all `ACTIVE GAP` rows are either repaired
with focused and end-to-end evidence or explicitly retired by an approved
architecture decision.  The final qualification must prove:

- a clean standalone installation opens only the EP-owned CENTRAL database;
- two projects have isolated queue, lease, run, history, telemetry, logs and
  evidence views while each can execute one lane concurrently;
- `<geen>` reveals only platform/provider data and explicit aggregates;
- local checkouts remain physical bindings only;
- no active standalone process, environment variable, service label or wheel
  entrypoint depends on DJConnect; and
- the release/update path has an authoritative, verifiable artifact source.
