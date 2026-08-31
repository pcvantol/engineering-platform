# ADR-0023 — EP central-store migration guardrails

**Status:** Accepted for Phase 2 / Increment 1 control contract
**Date:** 2026-08-31

## Context

ADR-0019 establishes the future single installation-owned Engineering Platform store. The current DJConnect-hosted schema-40 database remains the qualified sole runtime authority. Moving it without deterministic discovery, quiescence, backup, integrity and handoff controls would risk divergent writers or lost execution evidence.

## Decision

EP will resolve its future installation data root through `user_data_dir("Engineering Platform")` and place one central SQLite store at `engineering.db` directly beneath it. A future migration must apply the fail-closed discovery, cardinality, preflight, backup, SQLite backup, integrity, one-writer handoff and rollback-state rules in [EP central-store migration guardrails](../engineering/EP_CENTRAL_STORE_MIGRATION_GUARDRAILS.md).

This authorizes no database relocation, target-store creation, schema activation, service identity change, writer handoff, package extraction or consumer cutover. `LEGACY_ROLLBACK_RETIRED` is deferred.

## Consequences

- The existing schema-40 store remains sole authority throughout Increment 1.
- A later migration can fail before mutation when source, target, or runtime authority cannot be proven safe.
- Phase 3 physical extraction is blocked until a qualified central-store and rollback-compatible authority baseline exists.

## Alternatives considered

1. Copy the repository-local database during extraction. Rejected: it mixes code movement with data authority change.
2. Discover any `engineering.db` on disk. Rejected: it can choose unrelated evidence.
3. Run legacy and central stores concurrently. Rejected: EP has one lifecycle and SQLite writer authority.

## Affected repositories

- `pcvantol/djconnect` until physical extraction is qualified
- future `pcvantol/engineering-platform`
- Forge/Workspace/DJConnect consumer adapters

## Related documents

- [ADR-0019](0019-engineering-platform-central-installation-store.md)
- [Engineering Platform extraction and migration plan](../development/ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md)
- [Central-store migration guardrails](../engineering/EP_CENTRAL_STORE_MIGRATION_GUARDRAILS.md)
