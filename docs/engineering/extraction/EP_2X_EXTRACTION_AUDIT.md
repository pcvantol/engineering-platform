# Engineering Platform 2.x extraction audit

**Canonical extraction baseline:** `9fa2a2526e27d01cc7089d804b35c2a9c7ef435c`

## Completeness and exclusivity control

The audit independently walks the fixed Phase-0 roots and explicit entry-point
files, then compares their sorted digest with the manifest. A separate semantic
manifest digest binds the vocabulary and all classification rules, so a valid
but changed classification cannot silently redefine the frozen control. Manifest rules are
resolved by most-specific path. A candidate with no winning rule or more than
one equally-specific winning rule fails. Rules also fail for duplicate paths,
unsafe paths, missing required paths, invalid classifications, malformed
ownership/reason fields and invalid extraction targets.

## Control navigation

The frozen extraction control has four complementary, repository-local
surfaces. Use them together when reviewing a baseline result; none is a
substitute for another.

| Surface | Canonical location | Purpose |
| --- | --- | --- |
| Baseline description | [EP_2X_EXTRACTION_BASELINE.md](EP_2X_EXTRACTION_BASELINE.md) | Records the frozen baseline and its review boundary. |
| Classification manifest | [EP_2X_EXTRACTION_MANIFEST.json](EP_2X_EXTRACTION_MANIFEST.json) | Defines the closed candidate and ownership classification vocabulary. |
| Deterministic audit | [audit_ep_extraction_baseline.py](../../../scripts/engineering/audit_ep_extraction_baseline.py) | Recomputes the candidate, semantic and import controls from repository truth. |
| Regression coverage | [test_ep_extraction_baseline.py](../../../tests/engineering/test_ep_extraction_baseline.py) | Protects deterministic success and representative control failures. |

The migration sequence and future extraction phases remain in the canonical
[Engineering Platform extraction and migration plan](../../development/ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md).

The frozen baseline records its own candidate universe. The current result is
the deterministic projection for the checked-out repository, so it advances
when newly classified Engineering Platform candidates are added: 294
candidates, 294 classified exactly once, 0 unclassified and 0 ambiguous.
Operations Console source/assets/tests account for 17 candidates, all
classified exactly once. This includes the current dashboard presentation,
history navigation, status/configuration assets, five-language locale asset and
browser/status-store tests from the PR #940 baseline.

## Import, filesystem and entry-point coverage

The import audit is run over every effective `EP_PRODUCT_SOURCE` file: 99
files, 80 Python files and 505 static imports. It found 0 unknown imports, 0
DJConnect runtime imports, 0 Home Assistant runtime imports and 0
repository-local support imports; extraction-blocking imports are therefore 0.

Filesystem/CWD coverage is static over that same effective EP source set:
repository root resolution, `.engineering` runtime state, package/resource
paths, repository-local Inbox/report paths, launchd/user paths and temporary
test roots are classified. New candidate files cannot silently bypass that
claimed source set because the frozen candidate digest fails.

Entry points are derived from `python -m tools.engineering`, the execution-host
and dashboard launchers, dashboard commands, Inbox watcher, runner/bootstrap
scripts and workflow invocation. Each is within a candidate root and has an
effective classification; unclassified entry points: 0.

Ownership coverage is whole-set rather than sample-based: 64 EP test candidates
(including browser/contract/migration/consumer-integration tests), 24 workflow
candidates (5 EP workflow, 19 DJConnect retained), 36 EP documentation
candidates, 0 historical/generated-evidence candidates (immutable generated
run evidence is deliberately excluded from the candidate universe) and 66 consumer
adapter files. Qualification registry/assets are classified as EP release
assets. Unknown ownership: 0.

## Extraction blocker matrix

| ID | Category | Evidence | Severity | Required Phase | Blocks Package Extraction? | Resolution condition | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| EPX-P0-001 | MANIFEST_CONTROL | Candidate-universe/exclusivity gap from Increment 1 | P0 | 0 | YES | Deterministic independent discovery and exactly-once tests | RESOLVED |
| EPX-P0-002 | BASELINE_TAG | Roadmap requires approved baseline tag or exception | P0 | 0 | YES | `internal-ha-9fa2a2526e27d01cc7089d804b35c2a9c7ef435c` points at the baseline commit | RESOLVED |
| EPX-P1-001 | FILESYSTEM_COUPLING | `.engineering` remains repository-local runtime/data root | P1 | 2 | YES | Installation data-root and one-writer migration | DEFERRED_TO_PLANNED_PHASE |
| EPX-P1-002 | CWD_COUPLING | host/watcher/dashboard resolve repository-relative paths | P1 | 3 | YES | Installed package/resource and explicit project paths | DEFERRED_TO_PLANNED_PHASE |
| EPX-P1-003 | CONSUMER_COUPLING | lifecycle reads repository status/prompt/release governance | P1 | 1 | YES | Versioned consumer/service boundary | AUTHORIZED_CONTRACT_INCREMENT |
| EPX-P1-004 | RUNTIME_NAME | `tools.engineering`, dashboard name and `com.djconnect.*` labels | P1 | 3 | YES | Neutral package/command and migration adapters | DEFERRED_TO_PLANNED_PHASE |
| EPX-P2-001 | TEST_WORKFLOW_COUPLING | checkout fixtures and shared qualification workflows | P2 | 3 | NO | Installed-package fixtures and workflow split | DEFERRED_TO_PLANNED_PHASE |

## Determinism and drift proof

The audit projection is a sorted, timestamp-free semantic record. Two runs at
the same repository state produce the same digest and projection. Focused
fixtures prove failure for new EP-owned source, deleted required classified
path, classification mutation, overlap, unsafe path and candidate drift. A
new blocking import is detected because imports are recomputed over the entire
effective EP product source set; it is reported as a nonzero blocking-import
count rather than being ignored.

## Diff semantics

`RUN DELIVERY DIFF` is every file delivered by this increment.
`IMPLEMENTATION DIFF` is the bounded control/artifact change before merge.
`FINALIZATION DIFF` is the later governance reconciliation.
`ROLLING RECORD FINALIZATION DIFF` is only the four rolling records changed in
that Finalization. These scopes are intentionally not interchangeable; a
four-file rolling-record diff does not describe the full delivery.

## Phase-0 evaluation

| Exit evidence | Status |
| --- | --- |
| Baseline commit | PASS |
| Baseline tag or approved immutable rollback reference | PASS |
| Complete manifest, imports, runtime names, entry points, filesystem/CWD | PASS |
| Tests, docs, workflows, qualification assets, Operations Console | PASS |
| Determinism and drift detection | PASS |
| Open P0 blockers | `0` |
| Phase 0 Gate | PASS |

```text
PASS — PHASE 0 COMPLETE
```

**Phase 1 — Local Consumer API and credential lifecycle** is complete /
qualified: the loopback read-only service is ready, schema 40 is active, and
consumer registration plus credential lifecycle are qualified. Phase 2 /
Increment 1 defines migration guardrails only; it changes no runtime authority,
schema, consumer cutover, or mutation endpoint.
