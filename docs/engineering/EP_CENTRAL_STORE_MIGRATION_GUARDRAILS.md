# Engineering Platform central-store migration guardrails

**Status:** Phase 2 / Increment 3 — architecture authorized; not implemented
**Date:** 2026-08-31
**Authority:** [ADR-0024](../adr/0024-ep-controlled-central-store-cutover.md), [ADR-0023](../adr/0023-ep-central-store-migration-guardrails.md), [ADR-0019](../adr/0019-engineering-platform-central-installation-store.md)

## Scope and invariant

This document authorizes, but does not implement or execute, the deterministic Engineering Platform (EP) central-store cutover. It does not move a database, create a target store, change a service identity, activate schema 41, or start a second EP writer. Until a later qualified cutover is executed by explicit operator tooling, the DJConnect-hosted schema-40 store is the sole lifecycle, watcher, provider, recovery, qualification and SQLite writer authority.

## Installation data-root and store contract

EP resolves its installation-owned root with `platformdirs.user_data_dir("Engineering Platform")` (the approved `user_data_dir("Engineering Platform")` abstraction). The resolved directory is per user and machine, independent of checkout and consumer repository path, portable across supported operating systems, and deterministic for one installed EP identity. Consumers never construct or write this path.

On this macOS host the abstraction resolves to:

```text
/Users/pcvantol/Library/Application Support/Engineering Platform
```

The future canonical store is exactly `<user_data_dir("Engineering Platform")>/engineering.db`. It is one SQLite database per local EP installation, not a DJConnect file, not a consumer-repository file, and not one database per project. Project scope is represented by canonical `project_id` registrations and rows inside this one authority. The initial data-root layout reserves `backups/`, `migration/`, `logs/`, `runtime/`, and project Inbox roots below the root; those internals are not a consumer API.

## Candidate discovery and cardinality

The legacy store means only the current DJConnect-hosted schema-40 authority at `<current EP runtime root>/.engineering/engineering.db`. Future discovery receives explicit canonical runtime evidence: active watcher/Local API configuration and their resolved root, plus the EP storage resolver result (`storage.database_path(root)`). It normalizes and de-duplicates only these paths. It must never crawl the filesystem, choose a newest or first file, or derive a path from a prompt, repository name, or checkout location.

| Candidate count | Result |
| --- | --- |
| 0 | `LEGACY_STORE_NOT_FOUND`; blocked |
| 1 | eligible for the remaining preflight checks |
| more than 1 | `LEGACY_STORE_AMBIGUOUS`; blocked |

The target is classified without creating it: absent is eligible only after all other gates; an empty/new SQLite file is not authority and needs later cutover authorization; a compatible EP store blocks unless its verified receipt proves the same resumable operation; a non-empty conflict yields `TARGET_STORE_CONFLICT`; and an unreadable, unknown, or corrupt file yields `TARGET_INTEGRITY_FAILED`. No target is overwritten, merged, or selected automatically.

## Read-only preflight and migration receipt

Increment 2 implements the read-only `central-store-preflight` command:

```sh
python3 -m tools.engineering.central_store_migration preflight --repo <repository> --json
```

`dry-run` is an explicit alias with the same read-only semantics. It reports source path/schema, target root/path/state, candidate counts, quiescence, backup readiness, integrity readiness, project-scope summary and eligibility. It opens stores read-only, creates neither directory nor database, takes no lock, checkpoints no WAL, starts/stops no service, and makes no SQLite or filesystem mutation. Its JSON receipt is deterministic except for its timestamp; its migration ID is deterministically derived from the source fingerprint.

A later migration creates a bounded, redacted receipt in `<data-root>/migration/` and alongside its verified backup. It records migration ID; source path/store identity/schema/SHA-256 fingerprint; target root/store identity; timestamp; migration tool/version; project-scope baseline; backup reference; authority/rollback state; and verification result. It never contains a bearer, verifier, token, prompt, history payload, or plaintext secret.

## Admission freeze and quiescence

Migration starts only after an explicit operator-recorded admission freeze. The watcher remains running only long enough to acknowledge the freeze and admits no new HUMAN/iCloud submission or Managed execution. The Local Consumer API has no mutating submission capability. An active run must reach its normal terminal state; migration blocks rather than terminalizing it for quiescence.

Immediately before backup/copy, canonical preflight proves from source SQLite and runtime evidence:

- `engineering_transactions` has zero rows whose `phase` is not `COMPLETE`, `BLOCKED`, or `FAILED`;
- `execution_run_leases` has zero `ACTIVE` leases, including unexpired leases;
- `provider_recovery_attempts` has no `RECOVERY_AVAILABLE`, `RECOVERY_STARTING`, or `RECOVERY_IN_PROGRESS` state, nor an associated live process under the existing PID-reuse-safe identity check;
- watcher, dashboard, dashboard relay, Local Consumer API, and every separately managed execution/provider service are stopped with no live managed process; and
- every known `.engineering/locks` lock is non-blocking acquirable. An unreadable or indeterminate lock is unsafe.

Any nonzero or unavailable proof blocks with `ACTIVE_EXECUTION`, `ACTIVE_LEASE`, or `AUTHORITY_HANDOFF_NOT_SAFE`. The future stop order is admission freeze/stop watcher; stop separately managed Execution Host/provider service; stop Local Consumer API; stop dashboard relay; stop dashboard; then recheck all processes, locks, leases, transactions and recovery. No service retains writer authority during copy or handoff.

## Backup, SQLite consistency, and integrity

Before modifying/copying source, the controller creates `<data-root>/backups/legacy-schema40-<UTC timestamp>-<migration-id>.db`, with matching redacted metadata (source identity/schema/size/SHA-256/integrity/tool version). It must open read-only and pass `PRAGMA integrity_check`; failure is `BACKUP_FAILED`. The backup and legacy source are retained through the rollback-compatible period.

Under service quiescence, copying uses SQLite's online backup API (`sqlite3.Connection.backup`) from a read-only source to a new temporary target, then `fsync`, integrity verification and atomic rename. Raw `.db` copying is forbidden. The controller checks `-wal`/`-shm`; after quiescence it performs and verifies `PRAGMA wal_checkpoint(TRUNCATE)` using the sole controlled writer. Remaining WAL/SHM state blocks copy. The verified backup API snapshot, not loose WAL/SHM files, is copy input.

Source validation requires read-only open; exact schema `40`; `PRAGMA integrity_check = ok`; required tables `engineering_schema_migrations`, `engineering_metadata`, `engineering_transactions`, `execution_run_leases`, `provider_recovery_attempts`, `local_api_credentials`, and `local_api_consumer_registrations`; readable project/consumer/credential metadata; and no unresolved migration. Failure is `SOURCE_SCHEMA_MISMATCH` or `SOURCE_INTEGRITY_FAILED`.

Target validation repeats those tests and compares counts for every EP table plus the critical tables above; compares project IDs, consumer registrations, credential verifier/fingerprint metadata, run/project associations, and Prompt History/evidence links exactly; and scans receipt/projections for prohibited plaintext secret fields. Copy success alone is insufficient. Backup API failure is `COPY_FAILED`, target failure is `TARGET_INTEGRITY_FAILED`, and equivalence failure is `EVIDENCE_MISMATCH`.

Project scope is never rebuilt from a repository path. It preserves canonical `project_id`, registrations, credential scopes, execution/run/project associations and Prompt History/evidence links. The current schema-40 store represents consumer/project registration; full operational-row backfill is a later qualified Phase-2 increment.

## One-writer handoff and rollback states

The future atomic transition is `legacy sole writer -> verified backup/copy -> verified target while services stopped -> durable authority designation -> services restart only against central store`. There is no dual-writer interval. A failed pre-handoff validation leaves legacy authoritative and starts no central service.

`LEGACY_ROLLBACK_COMPATIBLE` is a strictly pre-write Stage-A state: central is current authority, admission freeze remains `ACTIVE`, legacy and backup are offline/read-only, and central has accepted no production execution evidence. The source/backup/target fingerprints, receipt and legacy-compatible service configuration remain available. It never means two active stores.

After a successful controlled Managed E2E writes central evidence, the state is `CENTRAL_STORE_ACTIVE_POST_WRITE` and direct legacy rollback is no longer permitted. The legacy store is historical/offline only; a later return requires an explicit reverse migration that preserves central writes. This is the divergent-write rule.

`LEGACY_ROLLBACK_RETIRED` is not authorized. It needs a later approved rollback-compatible observation window, central-store operational and restore evidence, qualified project-scope/consumer integrations, no unresolved recovery, and an approved retention/disposition decision. No legacy store is deleted now.

## Increment 3 controlled-cutover authorization

### State machine, freeze, and quiescence

The controller persists each transition, prior state, timestamp, tool version
and bounded diagnostic in a redacted migration receipt. Normal states are
monotonic:

```text
PRECHECK -> ADMISSION_FROZEN -> QUIESCENT -> BACKUP_VERIFIED
-> CENTRAL_STORE_CREATED -> TARGET_VERIFIED -> AUTHORITY_SWITCHED
-> SERVICES_RESTARTED -> POST_CUTOVER_VERIFIED
-> LEGACY_ROLLBACK_COMPATIBLE -> CENTRAL_STORE_ACTIVE_POST_WRITE
```

Failure states are `CUTOVER_BLOCKED` before authority switch and
`ROLLBACK_IN_PROGRESS -> ROLLBACK_COMPLETED` only from
`LEGACY_ROLLBACK_COMPATIBLE`. Failures never advance authority implicitly.

The future operator-only surface is:

```sh
python3 -m tools.engineering.central_store_migration admission freeze --repo <repository> --reason <reason>
python3 -m tools.engineering.central_store_migration admission status --repo <repository>
python3 -m tools.engineering.central_store_migration admission thaw --repo <repository> --migration-id <id>
python3 -m tools.engineering.central_store_migration cutover --repo <repository>
python3 -m tools.engineering.central_store_migration rollback --repo <repository> --migration-id <id>
python3 -m tools.engineering.central_store_migration status --repo <repository>
```

`freeze` durably writes `engineering_metadata.admission_freeze.v1` in the
current authoritative store with `ACTIVE`, reason, operator identity and
timestamps; the watcher acknowledges it before it stops. It survives shell,
watcher and host restart and is copied in the verified snapshot. `status`
reports `ACTIVE`/`INACTIVE` plus bounded reason/owner/timestamp. `thaw` is
explicit and audited, never automatic before Stage-A qualification. Freeze
blocks new iCloud/HUMAN admission, new Managed runs and mutable execution
ownership, but never kills/terminalizes an active run or changes history.

Prompt content, watcher payloads, Local Consumer API input and provider/LLM
output cannot freeze/thaw, start cutover, switch authority, roll back or delete
legacy data. Runtime migration is deterministic operator tooling; a provider
may only help develop it.

After freeze, the controller waits/blocks until non-terminal transactions,
`ACTIVE` leases, active recovery records/PID-reuse-safe Execution Host/provider
processes and unsafe locks are all zero/proven absent. It also proves no live
watcher, relay, dashboard, Local API or separately managed execution/provider
process. No force-kill is authorized merely to satisfy this gate. Stop order
is: Inbox watcher; separately managed Execution Host/provider service; Local
Consumer API; dashboard relay; dashboard. It then repeats all checks.

For `dashboard.lock`, `inbox-watcher.lock` and every discovered lock, the
controller proves its named owner stopped and tries non-blocking acquisition.
An unowned acquirable file is stale evidence, recorded and cleaned only by its
recognized owner/tool. An unreadable, held, unknown-owner or indeterminate lock
blocks; unknown live locks are never deleted.

### Source, backup, target, receipt, and authority switch

Immediately before copying, the receipt captures source/resolved path, schema
`40`, stat identity, SHA-256 fingerprint, integrity, journal/WAL state,
required tables, critical counts, project IDs, registration/verifier-scope
counts and bounded qualification/evidence inventory. It rechecks identity
after preflight: a change is `SOURCE_CHANGED_AFTER_PREFLIGHT` and blocks.

One backup is created at
`<data-root>/backups/legacy-schema40-<UTC-YYYYmmddTHHMMSSZ>-<migration-id>.db`
using `sqlite3.Connection.backup`, then fsync and read-only
integrity/fingerprint verification. Redacted backup metadata accompanies it.
`BACKUP_FAILED` stops with legacy authority and freeze retained until explicit
operator thaw. The backup never becomes authority.

First cutover accepts only `ABSENT`; `COMPATIBLE_EXISTING`,
`CONFLICTING_EXISTING`, `CORRUPT_UNREADABLE` and any other target state require
separate recovery authority. Under quiescence the sole controlled writer proves
`PRAGMA wal_checkpoint(TRUNCATE)`; unresolved `-wal`/`-shm` blocks. It uses the
backup API into a new temporary target, fsyncs/verifies, and atomically renames;
raw DB copying is forbidden. Target must prove schema 40, integrity PASS,
required tables, equivalent critical counts/project IDs/registrations/verifier
scopes/qualification-evidence counts, and no plaintext credentials. Mismatch is
`TARGET_EQUIVALENCE_FAILED` before switch.

Receipt fields are migration ID; redacted source/backup/target identities and
fingerprints; schema; equivalence/project summaries; timestamps; tool/version;
bounded operator identity; cutover/rollback state. It contains no secret,
credential value, prompt, history payload or raw audio.

The resolver reads only atomically-written
`<data-root>/runtime/store-authority.json`: authority generation, canonical
authoritative path, schema and verified fingerprint. Before it exists, only the
current legacy resolver is accepted. Afterwards every EP process requires the
validated pointer; it never selects newest/first DB, prompt, CWD or repository
preference. Transition: stopped services -> verified target -> atomic pointer
update/fsync -> resolver proves central -> restart -> every service proves
central. No partial/mixed start. Service labels/names/install commands/host
identities/package location do not change.

Restart order is separately managed Execution Host/provider service; Local
Consumer API; dashboard; dashboard relay after dashboard readiness; Inbox
watcher last. Every doctor/readiness emits safe central path plus authority
generation/fingerprint evidence. Mixed binding is `CENTRAL_STORE_NOT_IN_USE`.
`POST_CUTOVER_VERIFIED` requires schema 40, central integrity/equivalence,
watcher/relay/dashboard/Local API READY, desired-state MATCH, same-central
binding, no non-terminal runs/leases and bounded read-only Local API PASS.

### Thaw, post-write rule, rollback, and tests

Only this verified Stage-A state permits thaw: specifically for one normal
iCloud/HUMAN documentation-only clean Managed E2E with no fault injection. It
must create/finalize/reconcile in central, qualify PASS, write Prompt
History/evidence only centrally, and leave Local API READY. Central integrity,
schema/project scope/registrations remain intact; evidence counts increase only
centrally and the legacy source fingerprint/counts stay unchanged. Success is
`CENTRAL_STORE_ACTIVE_POST_WRITE`.

Before central's first production write, explicit rollback is allowed only for
central unreadability, post-restart NOT_READY, proven equivalence defect,
store-caused qualification failure, missing critical evidence, or migration-
attributable credential/auth breakage. It is not automatic or for unrelated
application failure: freeze stays active; stop services; prove central
quiescence and untouched legacy; atomically point to legacy; restart in the
canonical order; prove legacy integrity, doctor and MATCH; record
`ROLLBACK_COMPLETED`. No merge occurs. After central writes, stale legacy
rollback is forbidden; a reverse migration is required.

Future temporary-store/service tests cover durable freeze/status/thaw/prompt
safety/admission blocking/non-killed active runs; successful and failed copy
gates; no dual writable authority; same-central watcher/dashboard/Local API
binding; pre-write rollback without divergence; refusal after central writes;
and central-only Managed-E2E evidence projection.

## Stable diagnostics

Existing discovery diagnostics remain stable. Increment 3 additionally authorizes: `ADMISSION_FREEZE_FAILED`, `QUIESCENCE_FAILED`, `SERVICE_STOP_FAILED`, `BACKUP_FAILED`, `SOURCE_CHANGED_AFTER_PREFLIGHT`, `TARGET_CREATE_FAILED`, `TARGET_EQUIVALENCE_FAILED`, `AUTHORITY_SWITCH_FAILED`, `SERVICE_RESTART_FAILED`, `CENTRAL_STORE_NOT_IN_USE`, `POST_CUTOVER_READINESS_FAILED`, `THAW_FAILED`, `MANAGED_E2E_FAILED`, and `ROLLBACK_FAILED`. These are fail-closed and never reveal secrets.

## Phase boundaries and extraction order

| Increment | Name | Authorization |
| --- | --- | --- |
| Phase 2 / Increment 1 | Installation Data-Root Contract and Central-Store Migration Guardrails | Complete as documentation/control only |
| Phase 2 / Increment 2 | Central-Store Migration Tooling + Dry-Run Qualification | Complete / dry-run qualified |
| Phase 2 / Increment 3 | Controlled Central-Store Cutover | Authorized / not implemented |
| Phase 2 / Increment 4 | Post-Cutover Qualification + Central-Store Active Baseline | Not authorized / not implemented |

Macro-order is mandatory: Phase 2 central-store/project-scope migration, then Phase 3 physical extraction/package, then Phase 4 consumer cutover, then Phase 5 legacy removal. Phase 3 begins only after Phase 2 qualifies a stable installation-owned central store and authority/rollback state. Code ownership migration is distinct from data ownership migration.

Phase 3 uses the frozen extraction manifest to copy/move EP product source, tests, documentation, workflows and release assets with history/provenance preserved. **REIMPLEMENTATION FROM SCRATCH IS FORBIDDEN.** Phase 3 may not begin until the central store is authoritative, rollback/state semantics are known, all services consistently bind to the installation-owned store, and the clean central-store Managed E2E is qualified. Phase 3 must not silently perform another store migration.

## Increment 2 dry-run qualification

Phase 2 / Increment 2 is **IMPLEMENTED / DRY-RUN QUALIFIED**. Its real
read-only preflight on 2026-08-31 discovered exactly one current schema-40
legacy source, passed SQLite integrity, resolved the canonical macOS target,
and found it absent. It reported one registered project, two consumer
registrations and four credential verifier scopes without printing secret
material. The source remained byte- and metadata-identical.

Migration is currently ineligible by design: the watcher and dashboard locks
are active and admission freeze is `FREEZE_NOT_ACTIVE`. Increment 2 did not
stop services, freeze admission, create the target, copy a database, or change
authority. **Phase 2 / Increment 3 — Controlled Central-Store Cutover** is
next and now **AUTHORIZED / NOT IMPLEMENTED** by ADR-0024. This authorization does not activate the freeze, stop a service, create/copy a target, switch authority, or execute an E2E.
