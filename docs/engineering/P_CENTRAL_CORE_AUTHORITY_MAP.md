# P-CENTRAL-CORE authority map

**Status:** active cutover inventory; child record of
[`PHASE_P_MIGRATION_GAPS_REGISTER.md`](PHASE_P_MIGRATION_GAPS_REGISTER.md).

**Baseline commit:** `2e999a3ac49f3bca7c69b2a31d3feced78cc793a` (#27).

All active lifecycle and evidence paths have a classification below;
`AMBIGUOUS = 0`.  `CENTRAL_CANONICAL` means the installation-owned
`<data_root>/engineering.db`; a repository binding is physical execution
context, not operational authority.

| Module / symbol | State or evidence | Current authority | Target authority | Cutover strategy | Completion proof |
| --- | --- | --- | --- | --- | --- |
| `server.py`: `initialize`, topology, submissions | installation, project/repository bindings, submissions, CENTRAL run envelope | CENTRAL_CANONICAL | CENTRAL_CANONICAL | extend the existing schema in the one database | fresh install has one SQLite database |
| `central_database.py` | CENTRAL identity, maintenance, capacity policy | CENTRAL_CANONICAL | CENTRAL_CANONICAL | retain typed installation DB entry point | restart/path test |
| `parity_lifecycle_dispatcher.py`: `_claim`, `_set_state` | FIFO claim, run envelope, operator resolution | CENTRAL_CANONICAL | CENTRAL_CANONICAL | extend project/run repositories | two-project claim/operator tests |
| `parity_lifecycle_dispatcher.py`: `_persist_historical_input`, `_default_runner`, terminal history reconciliation | runner input, admission, checkpoint, report/history projection | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL_CANONICAL | inject explicit CENTRAL operational context into the preserved runner | installed managed/Genesis no-local-storage canaries |
| `storage.py`: `database_path`, `open_storage` | historical root database resolver | REPOSITORY_LOCAL_DB | HISTORICAL_READ_ONLY | exclude from installed paths; isolate forensic tooling | path/symbol-aware source guard |
| `agent_state.py`: `StateStore(central_database=...)` | checkpoints, lifecycle events | CENTRAL_CANONICAL | CENTRAL_CANONICAL | explicit database binding injected by parity composition; no JSON projection | dispatcher test proves no checkout `.engineering` directory |
| `agent_state.py`: unbound `StateStore(directory)` | retained watcher/recovery compatibility state | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL_CANONICAL or HISTORICAL/FORENSIC only | classify callsite-by-callsite before removal | not a completion authority |
| `execution_host.py`: CENTRAL-bound runner | runner checkpoints and Genesis active-run scan | CENTRAL_CANONICAL | CENTRAL_CANONICAL | uses `StateStore.run_ids()` instead of checkpoint-file discovery | CENTRAL checkpoint test |
| `execution_executor.py` | provider invocations, artifact records | REPOSITORY_LOCAL_DB | CENTRAL_CANONICAL | explicit CENTRAL telemetry/evidence repository pending | not yet a completion authority |
| `execution_lease.py`, `provider_recovery.py`, `provider_interruption.py`, `emergency_recovery.py` | leases, heartbeats, recovery, retries, cancellation | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL_CANONICAL | CENTRAL transactional lifecycle/lease/recovery repository | stale lease, retry, recovery, isolation tests |
| `execution_finalization.py`, `status_reconciliation.py`, `managed_autonomy.py` | merge wait, finalization and reconciliation evidence | REPOSITORY_LOCAL_DB | CENTRAL_CANONICAL | central lifecycle/evidence repository | merge wait/finalization tests |
| `telemetry.py`, `execution_timing.py`, `provider_usage.py` | timing, phase spans, outbox, provider/model/token usage | REPOSITORY_LOCAL_DB | CENTRAL_CANONICAL | central telemetry/provider repositories; aggregates derived | project-isolation and retention tests |
| `prompt_history.py`, `codex_chat.py`, `execution_reporting.py`, `execution_evidence.py` | history, chat, reports, receipts and artifact indexes | REPOSITORY_LOCAL_DB (files are ARTIFACT_FILE_ONLY) | CENTRAL_CANONICAL index + ARTIFACT_FILE_ONLY | CENTRAL identity/digest/location/discovery metadata | rebind/delete-checkout evidence test |
| `dashboard.py`, `dashboard_configuration.py`, `component_logging.py`, `dashboard_state.py` | compatibility status/history/configuration/log projections | REPOSITORY_LOCAL_DB | CENTRAL_CANONICAL or explicitly unavailable | never portray root-local data as CENTRAL; project minimal CENTRAL projection or fail closed | route-level boundary tests |
| `inbox_watcher.py` | historical watcher transaction/preflight state | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL_CANONICAL / PHYSICAL_EXECUTION_ONLY | normalize ingress only; no lifecycle truth | dispatch works with watcher state absent |

## Database identity

At this baseline `SERVER_DATABASE_FILENAME` is `engineering.db`; a fresh
installed Server uses `<data_root>/engineering.db`.  `ep-server.db` is not an
active installed-Server database, and is classified **HISTORICAL_ONLY**. The
baseline still fails the single-authority target because active dispatcher
compatibility, runner evidence/telemetry, recovery and delegated Console paths
use root-local storage. The explicit checkpoint binding is a partial cutover;
it does not turn the #27 compatibility environment into an authority solution.

## Cutover contract

All new standalone operational lifecycle and execution evidence is owned by
the installation CENTRAL database. Local repositories are physical execution
bindings only.

## StateStore callsite classification (post-#27 reinventory)

This is deliberately per callsite, rather than a module-level allowlist. A
reference remains **ACTIVE_STANDALONE_OPERATIONAL** until its caller is
converted or removed from every supported installed entrypoint.

| Path / symbol | Installed caller | Concern | Classification | Current authority | Required disposition |
| --- | --- | --- | --- | --- | --- |
| `parity_lifecycle_dispatcher._default_runner` | `LifecycleWorker` → dispatcher | checkpoint writes and Genesis conflict scan | ACTIVE_STANDALONE_OPERATIONAL | CENTRAL_CANONICAL (explicit binding) | migrated; retain test proof |
| `parity_lifecycle_dispatcher.reconcile_terminal_history` | Server startup reconciliation | terminal checkpoint read | ACTIVE_STANDALONE_OPERATIONAL | CENTRAL_CANONICAL (explicit binding) | migrated; retain test proof |
| `execution_host.main` | legacy `python -m engineering_platform` entrypoint | direct-host lifecycle | ACTIVE_STANDALONE_OPERATIONAL | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | replace or isolate behind historical/forensic entrypoint |
| `inbox_watcher` retry, cancellation, merge-wait and recovery calls | watcher / Dashboard compatibility routes | retry, merge wait, recovery lifecycle | ACTIVE_STANDALONE_OPERATIONAL | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL lifecycle repository; then Console adapter or fail closed |
| `provider_recovery._validate_control_target` | qualification control CLI and Execution Host | recovery eligibility | ACTIVE_STANDALONE_OPERATIONAL | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL lifecycle repository; fault marker remains PHYSICAL_EXECUTION_ONLY |
| `provider_interruption.terminalize_after_host_exit`, `prepare_same_run_recovery_after_host_exit` | watcher recovery scan | interruption terminalization and recovery | ACTIVE_STANDALONE_OPERATIONAL | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL lifecycle/recovery repositories |
| `emergency_recovery._plan`, `execute` | Dashboard emergency-recovery route | cancel/rollback lifecycle | ACTIVE_STANDALONE_OPERATIONAL | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL lifecycle/recovery repository; git rollback remains PHYSICAL_EXECUTION_ONLY |

`StateStore` raw-module count is **6**. It is not a completion count: two
dispatcher callsites are already CENTRAL-bound; all other rows above remain
active operational migration work. No row is classified `DEAD_UNREACHABLE`.

## Active terminal telemetry boundary

`inbox_watcher` owns the legacy terminal chain
`queue_terminal_telemetry` → `terminal_telemetry_outbox` →
`materialize_pending_terminal_telemetry` → `persist_execution`. It is
**ACTIVE_STANDALONE_OPERATIONAL**, not historical: it claims work, launches
the runner and is launched by supported watcher/Dashboard composition. The
typed telemetry APIs now accept an explicit CENTRAL database binding, but the
watcher has no such composition input yet. It must be migrated through an
explicit lifecycle composition root or made unavailable; deriving CENTRAL from
the checkout is forbidden.
