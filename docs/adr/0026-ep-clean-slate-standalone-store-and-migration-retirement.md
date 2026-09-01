# ADR-0026 — EP clean-slate standalone store and migration retirement

**Status:** Accepted
**Date:** 2026-08-31

## Context

Phase 1 established the standalone consumer/API/authentication boundary. Phase
2 established the portable installation data-root, single-writer central-store
architecture, cutover controls, hermetic authority isolation and generic
forensic tooling. The attempted migration
`41feb31e-2e25-42c4-bca1-bbfc97dde6f4` exposed broad test-harness
contamination in its CENTRAL target, including a non-product schema marker 41.

The forensic delta exporter and attribution reports are deterministic evidence.
They prove broad test state, while proving only one CENTRAL-only legitimate
business record: `human-ingress-c31c33fc03e78a585c259430368d0543bdb9a5a622690790`,
at `RECEIVED_ONLY` with no run, provider invocation, qualification,
finalization, reconciliation, or Prompt History descendant. No legitimate
CENTRAL-only execution/evidence lifecycle has been positively proven.

Standalone EP has not become an external-consumer authority. Reconstructing
or migrating historical contaminated state would add risk without enough
product value.

## Decision

The future standalone `pcvantol/engineering-platform` installation starts with
a **new, clean, installation-owned database**. No DJConnect-hosted database
file, credential, registration, project-scope row, or historical execution
state is copied into it.

The current migration is terminally classified
`RETIRED_FOR_CLEAN_SLATE_EXTRACTION`. This is neither a successful cutover nor
a rollback. Its history remains immutable forensic/migration evidence. It must
not proceed to Stage A, thaw into normal CENTRAL operation,
contaminated-prewrite recovery, reverse reconciliation, or further attribution
as an extraction-eligibility condition.

The remaining forensic unknowns are not a Phase-3 extraction blocker. Further
attribution is optional diagnostic work and does not authorize recovery.

### Historical evidence retention

Retain read-only, fingerprint-bound incident evidence outside the future
standalone runtime authority: pristine LEGACY schema-40 and contaminated
CENTRAL database files; migration receipts; authority-pointer history;
`QUIESCENT_SOURCE_BASELINE`; backup/equivalence evidence; forensic-delta;
attribution V1/V2; and related ADRs/reports. Preserve original paths or an
approved archival location, SHA-256 fingerprints, receipt/migration ID,
capture date and access provenance. Nothing in this ADR deletes either
database or turns an archive into a runtime seed.

The known HUMAN ingress has a bounded later disposition: an explicit operator
decision may either replay/import it into the clean standalone store with its
source provenance, or archive it as intentionally not resumed. This ADR
neither replays nor discards it.

### Store and schema model

LEGACY remains suspended throughout the current clean-slate MVP. It is
historical evidence, never a standalone seed or temporary development-execution
authority. Until standalone qualification is complete, development execution
uses native Codex CLI directly, outside EP; this does not create a temporary EP
runtime, queue, writer, or authority handoff. Contaminated CENTRAL is
forensic/non-seed state, is not official product schema 41, and must never be
treated as a valid schema-41 product store.

Official product schema **41** is defined from the canonical schema-40 product
definitions, not by inspection or adoption of CENTRAL. It provides immutable
control provenance for credential lifecycle, consumer-registration lifecycle
and authority-relevant project-scope mutations. Standalone EP supports both a
fresh schema-41 bootstrap and, separately, a legitimate clean schema-40 to
official-schema-41 compatibility upgrade. The current contaminated database is
not a fixture or input for either path.

Consumer/project bootstrap is fresh: consumers register again with canonical
project IDs and mutable metadata, receive newly issued credentials held only in
OS-native secret storage, and old DJConnect-hosted credentials are retired by
a later consumer-cutover plan. No secret or contaminated project-scope copying
is permitted.

### Phases and operations

Phase 2 is **CLOSED / RETIRED CLEAN-SLATE DECISION**. Its durable deliverables
remain the installation data-root, central-store/single-writer architecture,
migration and recovery controls, service quiescence, hermetic authority,
forensic delta/attribution tooling, and consumer API/auth/registration
foundation. The attempted live migration is retired, not erased.

Phase 3 is authorized to begin after documentation/governance reconciliation:
**HISTORY-PRESERVING PHYSICAL EXTRACTION + CLEAN STANDALONE STORE**. It is not
a rewrite. It moves the audited EP runtime, Execution Host, watcher, storage,
provider/recovery stack, Local Consumer API, credential/registration authority,
dashboard/reporting, qualification/evidence tooling, Prompt History, forensic
tooling, EP tests/docs/workflows/release assets with filtered-history
provenance and equivalence checks. DJConnect history is never rewritten.

The required order is:

```text
physical code extraction
→ standalone package/import qualification
→ official schema-41 fresh-store bootstrap
→ standalone EP Server + Project Agent qualification
→ fresh DJConnect project attachment
→ first governed execution
→ STANDALONE_EP_VERIFIED clean CENTRAL activation
→ later consumer registration/cutover
```

No standalone production authority or service-label transition occurs before
that ordering reaches its relevant gate. Existing `com.djconnect.*` identities
remain unchanged until a separately controlled standalone package/service
transition. Phase 4 remains consumer cutover; Phase 5 remains legacy runtime
removal after standalone package, store and consumers qualify. Archive
retention/deletion is a separate decision.

Until `STANDALONE_EP_VERIFIED`, keep the current contaminated CENTRAL runtime
frozen/limited and LEGACY suspended; do not normalize, thaw, restart, or use
either for development execution. No legacy database migration, legacy state
copy, live service restart, authority-pointer action, database mutation,
recovery, or schema migration is authorized by this ADR. Agents, hosts,
projects, queues, executions and credentials are registered/provisioned afresh
in clean CENTRAL. `DEVELOPMENT_HOST_MATCH` remains limited to the documented
known host-drift condition and cannot waive another qualification gate.

## Consequences

- Incident-specific recovery expansion, reverse reconciliation and generated-ID
  classifier repair are no longer migration prerequisites. Existing merged code
  and evidence remain preserved.
- The hermetic harness, forensic delta exporter and provenance attribution are
  retained as generic EP infrastructure and move with Phase 3.
- ADR-0024's current legacy-to-CENTRAL cutover procedure is superseded for this
  migration. ADR-0025's current-incident recovery sequence is superseded;
  its official schema-41 control-provenance design remains the target product
  schema decision.
- Physical extraction, fresh-store creation, service changes and consumer
  cutover remain separate implementation decisions.

## Related documents

- [ADR-0019](0019-engineering-platform-central-installation-store.md)
- [ADR-0023](0023-ep-central-store-migration-guardrails.md)
- [ADR-0024](0024-ep-controlled-central-store-cutover.md)
- [ADR-0025](0025-ep-control-provenance-and-baseline-delta-recovery.md)
- [EP extraction and migration plan](../development/ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md)
