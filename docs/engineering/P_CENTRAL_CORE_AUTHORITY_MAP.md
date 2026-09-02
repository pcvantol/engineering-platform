# P-CENTRAL-CORE authority map

**Status:** baseline inventory; child record of
[`PHASE_P_MIGRATION_GAPS_REGISTER.md`](PHASE_P_MIGRATION_GAPS_REGISTER.md).

**Audited commit:** `711d1ea102df12a5fbeb77573d79571378800cb6`.

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
| `agent_state.py`: `StateStore` | checkpoints, lifecycle events, JSON projections | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL_CANONICAL | CENTRAL checkpoint/event repository; no JSON lifecycle projection | no `.engineering/engineering-runs` created |
| `execution_host.py`, `execution_executor.py` | runner state, provider invocations, artifact records | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL_CANONICAL | explicit CENTRAL repositories; retain only checkout/worktree/provider work locally | dispatcher-to-runner integration |
| `execution_lease.py`, `provider_recovery.py`, `provider_interruption.py`, `emergency_recovery.py` | leases, heartbeats, recovery, retries, cancellation | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL_CANONICAL | CENTRAL transactional lifecycle/lease/recovery repository | stale lease, retry, recovery, isolation tests |
| `execution_finalization.py`, `status_reconciliation.py`, `managed_autonomy.py` | merge wait, finalization and reconciliation evidence | REPOSITORY_LOCAL_DB | CENTRAL_CANONICAL | central lifecycle/evidence repository | merge wait/finalization tests |
| `telemetry.py`, `execution_timing.py`, `provider_usage.py` | timing, phase spans, outbox, provider/model/token usage | REPOSITORY_LOCAL_DB | CENTRAL_CANONICAL | central telemetry/provider repositories; aggregates derived | project-isolation and retention tests |
| `prompt_history.py`, `codex_chat.py`, `execution_reporting.py`, `execution_evidence.py` | history, chat, reports, receipts and artifact indexes | REPOSITORY_LOCAL_DB (files are ARTIFACT_FILE_ONLY) | CENTRAL_CANONICAL index + ARTIFACT_FILE_ONLY | CENTRAL identity/digest/location/discovery metadata | rebind/delete-checkout evidence test |
| `dashboard.py`, `dashboard_configuration.py`, `component_logging.py`, `dashboard_state.py` | compatibility status/history/configuration/log projections | REPOSITORY_LOCAL_DB | CENTRAL_CANONICAL or explicitly unavailable | never portray root-local data as CENTRAL; project minimal CENTRAL projection or fail closed | route-level boundary tests |
| `inbox_watcher.py` | historical watcher transaction/preflight state | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | CENTRAL_CANONICAL / PHYSICAL_EXECUTION_ONLY | normalize ingress only; no lifecycle truth | dispatch works with watcher state absent |

## Database identity

At this baseline `SERVER_DATABASE_FILENAME` is `engineering.db`; a fresh
installed Server uses `<data_root>/engineering.db`.  `ep-server.db` is not an
active installed-Server database, and is classified **HISTORICAL_ONLY**.  The
baseline nevertheless fails the single-authority target because active
dispatcher, runner and delegated Console paths still use root-local storage.

## Cutover contract

All new standalone operational lifecycle and execution evidence is owned by
the installation CENTRAL database. Local repositories are physical execution
bindings only.
