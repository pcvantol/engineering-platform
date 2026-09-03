# P-CENTRAL-CORE authority map

**Status:** P-CENTRAL-CORE completion inventory; child record of
[`PHASE_P_MIGRATION_GAPS_REGISTER.md`](PHASE_P_MIGRATION_GAPS_REGISTER.md).

**Baseline commit:** `2e999a3ac49f3bca7c69b2a31d3feced78cc793a` (#27).

All active lifecycle and evidence paths have a classification below;
`AMBIGUOUS = 0`.  `CENTRAL_CANONICAL` means the installation-owned
`<data_root>/engineering.db`; a repository binding is physical execution
context, not operational authority.

The completion rescan found 29 product modules and 120 raw `open_storage(...)`
references, plus 6 product modules and 14 raw `StateStore(...)` references.
Those raw references are classified by exact path below; they are not active
authority counts. The source guard rejects an unclassified new marker. Active
standalone root-derived storage, local StateStore and secondary operational
database counts are all **0**; `AMBIGUOUS = 0`.

| Module / symbol | State or evidence | Current authority | Target authority | Cutover strategy | Completion proof |
| --- | --- | --- | --- | --- | --- |
| `server.py`: `initialize`, topology, submissions | installation, project/repository bindings, submissions, CENTRAL run envelope | CENTRAL_CANONICAL | CENTRAL_CANONICAL | extend the existing schema in the one database | fresh install has one SQLite database |
| `central_database.py` | CENTRAL identity, maintenance, capacity policy | CENTRAL_CANONICAL | CENTRAL_CANONICAL | retain typed installation DB entry point | restart/path test |
| `parity_lifecycle_dispatcher.py`: `_claim`, `_set_state` | FIFO claim, run envelope, operator resolution | CENTRAL_CANONICAL | CENTRAL_CANONICAL | extend project/run repositories | two-project claim/operator tests |
| `parity_lifecycle_dispatcher.py`: `_persist_historical_input`, `_default_runner`, terminal history reconciliation | runner input, admission, checkpoint, report/history projection | CENTRAL_CANONICAL | CENTRAL_CANONICAL | inject explicit CENTRAL operational context into the preserved runner | installed Managed/Genesis no-local-storage canaries |
| `storage.py`: `database_path`, `open_storage` | historical root database resolver | REPOSITORY_LOCAL_DB | HISTORICAL_READ_ONLY | exclude from installed paths; isolate forensic tooling | path/symbol-aware source guard |
| `agent_state.py`: `StateStore(central_database=...)` | checkpoints, lifecycle events | CENTRAL_CANONICAL | CENTRAL_CANONICAL | explicit database binding injected by parity composition; no JSON projection | dispatcher test proves no checkout `.engineering` directory |
| `agent_state.py`: unbound `StateStore(directory)` | retained watcher/recovery compatibility state | HISTORICAL/FORENSIC_ONLY | CENTRAL_CANONICAL or HISTORICAL/FORENSIC only | active product composition rejects unbound authority | source guard and compatibility-boundary tests |
| `execution_host.py`: CENTRAL-bound runner | checkpoints, leases, recovery, provider usage, phase spans, validation profiles/command receipts/results, managed-autonomy evidence and terminal report index | CENTRAL_CANONICAL | CENTRAL_CANONICAL | explicit database/artifact bindings injected from `StateStore` | focused host/recovery/timing/validation tests |
| `execution_executor.py`: validation diagnostics | immutable validation evidence | CENTRAL_CANONICAL index + ARTIFACT_FILE_ONLY | CENTRAL_CANONICAL index + ARTIFACT_FILE_ONLY | explicit CENTRAL database and artifact-root binding | host/storage tests |
| `execution_lease.py`, `provider_recovery.py`: CENTRAL runner route | leases, heartbeats, recovery and retry lineage; recovery-result artifact integrity and retrieval | CENTRAL_CANONICAL | CENTRAL_CANONICAL | explicit database and artifact-root binding through runner | focused lease/recovery tests |
| `provider_interruption.py`, `emergency_recovery.py` | retained watcher/direct-dashboard compatibility branches | CENTRAL_CANONICAL on supported composition | CENTRAL_CANONICAL on supported composition | Server/Execution Host supply explicit CENTRAL bindings; unbound branches are guard-classified legacy compatibility | focused binding and authority-guard tests |
| `execution_finalization.py`, `status_reconciliation.py`, `managed_autonomy.py` | merge wait, finalization and reconciliation evidence | CENTRAL_CANONICAL | CENTRAL_CANONICAL | central lifecycle/evidence repository | merge wait/finalization tests and Managed terminal canary |
| `execution_timing.py`, `provider_usage.py`: CENTRAL runner route | phase spans; provider/model/token usage | CENTRAL_CANONICAL | CENTRAL_CANONICAL | explicit runner database binding | focused timing/provider tests |
| `telemetry.py` | terminal telemetry/outbox/read models | CENTRAL_CANONICAL on active lifecycle paths | CENTRAL_CANONICAL | explicit active APIs require CENTRAL binding; retired watcher branch is historical compatibility only | focused telemetry and source-guard tests |
| `prompt_history.py`, `codex_chat.py`, `execution_reporting.py`, `execution_evidence.py` | history, chat, reports, receipts and artifact indexes | CENTRAL_CANONICAL index + ARTIFACT_FILE_ONLY | CENTRAL_CANONICAL index + ARTIFACT_FILE_ONLY | CENTRAL identity/digest/location/discovery metadata | rebind/delete-checkout evidence test |
| `dashboard.py`, `dashboard_configuration.py`, `component_logging.py`, `dashboard_state.py` | compatibility status/history/configuration/log projections | REPOSITORY_LOCAL_DB | CENTRAL_CANONICAL or explicitly unavailable | never portray root-local data as CENTRAL; project minimal CENTRAL projection or fail closed | route-level boundary tests |
| `inbox_watcher.py` | historical watcher transaction/preflight state | RETIRED_TRANSPORT_COMPATIBILITY | CENTRAL_CANONICAL / PHYSICAL_EXECUTION_ONLY | active watcher entrypoints fail closed; Server/LifecycleWorker own lifecycle truth | dispatch works with watcher state absent |

## Database identity

At this baseline `SERVER_DATABASE_FILENAME` is `engineering.db`; a fresh
installed Server uses `<data_root>/engineering.db`.  `ep-server.db` is not an
active installed-Server database, and is classified **HISTORICAL_ONLY**. The
active standalone lifecycle uses exactly one operational database. Retained
Console projections are a separate P-CENTRAL-CONSOLE presentation boundary;
they are not CENTRAL operational authority and must fail closed rather than
fall back after a core concern has moved.

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
| `execution_host.main` | direct installed host entrypoint | direct-host lifecycle | ACTIVE_STANDALONE_OPERATIONAL | CENTRAL_CANONICAL (explicit `--central-database` required) | fail closed without the composition-root binding; no local StateStore projection |
| `inbox_watcher` retry, cancellation, merge-wait and recovery calls | retired checkout-bound watcher commands | retry, merge wait, recovery lifecycle | HISTORICAL_PROVENANCE | REPOSITORY_LOCAL_DB + LOCAL_STATESTORE | operational `once`, `run` and `install` commands fail closed; retained code is not reachable from Server/LifecycleWorker | focused retirement boundary test |
| `provider_recovery._validate_control_target` | qualification control CLI and Execution Host | recovery eligibility | ACTIVE_STANDALONE_OPERATIONAL | CENTRAL_CANONICAL | CENTRAL lifecycle repository; fault marker remains PHYSICAL_EXECUTION_ONLY |
| `provider_interruption.terminalize_after_host_exit`, `prepare_same_run_recovery_after_host_exit` | retired watcher recovery scan | interruption terminalization and recovery | HISTORICAL_COMPATIBILITY_ONLY | retained compatibility fallback | watcher CLI rejects operational invocation before storage; active host route is CENTRAL-bound |
| `emergency_recovery._plan`, `execute` | Dashboard emergency-recovery route | cancel/rollback lifecycle | ACTIVE_STANDALONE_OPERATIONAL | CENTRAL_CANONICAL (explicit Server binding) | validated project/run ownership and CENTRAL lifecycle/recovery repository; git rollback remains PHYSICAL_EXECUTION_ONLY |

`StateStore` raw-module count is **6**. It is not a completion count: every
active lifecycle caller is CENTRAL-bound, while retained unbound forms are
explicit historical/forensic compatibility only. No row is classified
`DEAD_UNREACHABLE`.

## Active terminal telemetry boundary

The retired `inbox_watcher` source retains the legacy terminal chain
`queue_terminal_telemetry` → `terminal_telemetry_outbox` →
`materialize_pending_terminal_telemetry` → `persist_execution`. It is
**HISTORICAL_PROVENANCE**, not active standalone authority: the CLI refuses
`once`, `run` and `install` before workspace provisioning or storage access.
The Server/LifecycleWorker composition has no watcher launch path. The typed
telemetry APIs remain CENTRAL-bound on active lifecycle paths; the retained
watcher implementation may only be exercised by isolated historical/forensic
tests until a separate P-TRANSPORT contract replaces it. Deriving CENTRAL from
the checkout is forbidden.

## Explicit retained fallback classifications

| Exact path | Classification | Supported installed reachability | Reason |
| --- | --- | --- | --- |
| `telemetry.py` unbound `central_database=None` branches | HISTORICAL_COMPATIBILITY_ONLY | no | Their only product callers are the retired watcher; active runner calls supply its explicit CENTRAL binding. |
| `provider_interruption.py` unbound branches | HISTORICAL_COMPATIBILITY_ONLY | no | The retained watcher is rejected before storage access; active Execution Host calls use its bound CENTRAL store. |
| `emergency_recovery.py` unbound branches | HISTORICAL_COMPATIBILITY_ONLY | no | The supported Server Dashboard composition supplies both CENTRAL database and validated project scope; direct historical dashboard composition remains compatibility-only. |

`tools/qualification/p_central_core_authority_guard.py` is the deterministic
classification gate. Every file containing an operational-storage marker must
have an exact path classification; an unclassified new product module fails.
The guard separately asserts the direct-host CENTRAL requirement and the
retired watcher entrypoint. Its negative fixture is rejected and its exact
historical telemetry fixture is accepted.
