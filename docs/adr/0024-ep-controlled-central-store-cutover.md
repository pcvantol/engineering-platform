# ADR-0024 — EP controlled central-store cutover

**Status:** Superseded for migration `41feb31e-2e25-42c4-bca1-bbfc97dde6f4` by ADR-0026
**Date:** 2026-08-31

## Context

**Supersession note:** ADR-0026 retires this incident's legacy-to-CENTRAL
procedure in favor of a clean standalone store. This historical procedure is
preserved as Phase-2 control evidence and does not authorize an operation.

ADR-0019 and ADR-0023 establish one installation-owned Engineering Platform
(EP) store and the read-only schema-40 migration controls. Increment 2 proved
one healthy legacy source, an absent central target, one project, two consumer
registrations and four verifier scopes. It deliberately did not freeze
admission, stop services, create/copy a store, or change authority.

The authority move must preserve evidence and one-writer safety without
changing schema, service identities, consumer credentials, project semantics,
or EP package location.

## Decision

Authorize the future deterministic, operator-only Increment-3 procedure in
[EP central-store migration guardrails](../engineering/EP_CENTRAL_STORE_MIGRATION_GUARDRAILS.md).
It keeps SQLite schema `40` and moves only storage authority from the current
DJConnect-hosted store to `<user-data-root>/engineering.db`.

The cutover has two bounded stages:

1. **Stage A — technical qualification under durable admission freeze.** A
   verified central copy is switched to authority and receives read-only
   qualification only. While no central production write is accepted, the
   untouched legacy source is a direct rollback candidate. This is the only
   period denoted `LEGACY_ROLLBACK_COMPATIBLE`.
2. **Stage B — proof of central writes.** The operator thaws specifically for
   one clean documentation-only Managed E2E. Its new run/evidence is written
   only to central. On success the state becomes
   `CENTRAL_STORE_ACTIVE_POST_WRITE`; direct legacy rollback is retired and
   any later return to legacy requires a separately authorized reverse
   migration. The legacy database remains preserved offline; it is not
   deleted or destructively renamed.

The authority resolver is an installation-controlled, atomically-written
`<user-data-root>/runtime/store-authority.json` pointer. It identifies the
authoritative path, authority generation, expected schema and verified store
fingerprint. Before a pointer exists, the current legacy resolver is the only
accepted authority. Once an authority pointer exists, every EP process must
resolve exactly that validated path; an absent, unreadable, mismatched, or
ambiguous pointer is fail-closed. The resolver must never choose a database by
mtime, existence order, prompt text, CWD, or repository-location preference.

Admission freeze is durable authority-store metadata at
`engineering_metadata` key `admission_freeze.v1`, containing only state,
reason, owner/operator identity and timestamps. The migration controller
carries this metadata in the verified snapshot. Only explicit operator control
tooling may mutate it; prompts, providers, watcher input and Local Consumer
API requests cannot freeze, thaw, cut over, roll back, or retire legacy data.

This ADR authorizes no runtime implementation, production action, schema 41,
service label/name/installation identity change, Keychain operation, standalone
repository, or Phase-3 physical extraction.

## Consequences

- At all times exactly one writable EP authority exists.
- Increment 3 implementation must provide an auditable state machine,
  durable receipt, safe stop/restart gates, explicit status/control surface,
  and the tests specified by the guardrails.
- A central post-write failure cannot silently discard new evidence by pointing
  services at stale legacy data.
- Phase 3 remains blocked until central authority, consistent service binding,
  and the clean Managed E2E are qualified.

## Alternatives considered

1. Keep legacy direct rollback after central writes. Rejected: it can lose new
   execution evidence.
2. Use a transient environment-variable freeze. Rejected: it is lost on
   watcher/host restart and cannot be audited.
3. Allow a provider to drive the transition. Rejected: authority movement must
   be deterministic operator tooling, not provider execution.

## Affected repositories

- `pcvantol/djconnect` until Phase 3 is qualified
- future `pcvantol/engineering-platform`
- Forge/Workspace/DJConnect consumer adapters

## Related documents

- [ADR-0019](0019-engineering-platform-central-installation-store.md)
- [ADR-0023](0023-ep-central-store-migration-guardrails.md)
- [Engineering Platform extraction and migration plan](../development/ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md)
- [Central-store migration guardrails](../engineering/EP_CENTRAL_STORE_MIGRATION_GUARDRAILS.md)
