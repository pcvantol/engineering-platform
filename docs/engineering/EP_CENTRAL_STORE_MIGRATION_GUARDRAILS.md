# Engineering Platform central-store migration guardrails

**Status:** Phase 2 / Increment 1 — control contract; no migration authorized
**Date:** 2026-08-31
**Authority:** [ADR-0023](../adr/0023-ep-central-store-migration-guardrails.md), [ADR-0019](../adr/0019-engineering-platform-central-installation-store.md)

## Scope and invariant

This document makes a future Engineering Platform (EP) central-store migration deterministic and fail closed. It does not move a database, create a target store, change a service identity, activate schema 41, or start a second EP writer. Until a later qualified cutover, the DJConnect-hosted schema-40 store is the sole lifecycle, watcher, provider, recovery, qualification and SQLite writer authority.

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

Increment 2 may implement a read-only `central-store-preflight` command. It reports source path/schema, target root/path/state, candidate counts, quiescence, backup readiness, integrity readiness, project-scope summary and eligibility. It opens stores read-only, creates neither directory nor database, takes no lock, checkpoints no WAL, starts/stops no service, and makes no SQLite or filesystem mutation.

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

`LEGACY_ROLLBACK_COMPATIBLE` means central is current authority; legacy and backup are retained offline/read-only; no post-cutover legacy write occurred; migration receipt/source-target fingerprints/qualification evidence remain available; and the legacy-compatible service package/configuration remains retained. It never means two active stores.

Rollback requires an explicit decision: stop central services; prove central quiescence and source/receipt continuity; prove no legacy divergence; designate legacy authority; restart only against legacy; then run doctor and MATCH qualification. This increment does not implement it.

`LEGACY_ROLLBACK_RETIRED` is not authorized. It needs a later approved rollback-compatible observation window, central-store operational and restore evidence, qualified project-scope/consumer integrations, no unresolved recovery, and an approved retention/disposition decision. No legacy store is deleted now.

## Stable diagnostics

`LEGACY_STORE_NOT_FOUND`, `LEGACY_STORE_AMBIGUOUS`, `TARGET_STORE_CONFLICT`, `ACTIVE_EXECUTION`, `ACTIVE_LEASE`, `SOURCE_SCHEMA_MISMATCH`, `SOURCE_INTEGRITY_FAILED`, `BACKUP_FAILED`, `COPY_FAILED`, `TARGET_INTEGRITY_FAILED`, `EVIDENCE_MISMATCH`, and `AUTHORITY_HANDOFF_NOT_SAFE` are stable future fail-closed diagnostics.

## Phase boundaries and extraction order

| Increment | Name | Authorization |
| --- | --- | --- |
| Phase 2 / Increment 1 | Installation Data-Root Contract and Central-Store Migration Guardrails | Complete as documentation/control only |
| Phase 2 / Increment 2 | Central-Store Migration Tooling + Dry-Run Qualification | Not authorized here |
| Phase 2 / Increment 3 | Controlled Central-Store Cutover | Not authorized here |
| Phase 2 / Increment 4 | Post-Cutover Qualification + Rollback-Compatible Baseline | Not authorized here |

Macro-order is mandatory: Phase 2 central-store/project-scope migration, then Phase 3 physical extraction/package, then Phase 4 consumer cutover, then Phase 5 legacy removal. Phase 3 begins only after Phase 2 qualifies a stable installation-owned central store and authority/rollback state. Code ownership migration is distinct from data ownership migration.

Phase 3 uses the frozen extraction manifest to copy/move EP product source, tests, documentation, workflows and release assets with history/provenance preserved. **REIMPLEMENTATION FROM SCRATCH IS FORBIDDEN.** Phase 3 must not silently perform another store migration.
