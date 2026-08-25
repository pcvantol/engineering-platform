# Engineering Platform 2.x extraction baseline

**Status:** Phase 0 / Increment 2 reconciliation control

**Manifest:** `EP_2X_EXTRACTION_MANIFEST.json` (version `2`)

## Canonical identity

`EXTRACTION_BASELINE_COMMIT` is the one immutable repository state whose
candidate universe and effective manifest classifications are frozen for future
drift comparisons: `9fa2a2526e27d01cc7089d804b35c2a9c7ef435c`.

| Identity | Canonical value |
| --- | --- |
| Source repository | `pcvantol/djconnect` |
| Extraction baseline commit | `9fa2a2526e27d01cc7089d804b35c2a9c7ef435c` |
| Baseline generated at | `2026-08-25T22:35:55+02:00` |
| Engineering Platform / Operations Console | `2.0.0` / `2.0.0` |
| Storage schema / Consumer Contract | `29` / `1.0` |
| Bootstrap contract | `2026.12` |

`RUN_START_COMMIT` for Increment 1 was
`05583f229ad878c5c06f264a661b4d92eb33b128`.
`IMPLEMENTATION_MERGE_COMMIT` was
`a2e38ea8f49752c15413fc30f730cd60214b3dc3`; its
`FINALIZATION_MERGE_COMMIT` was
`565c618328be1b60c102f07661433ea15536e828`.
`FINAL_RECONCILED_MAIN_COMMIT` is the baseline commit above. These commits have
different lifecycle meanings; only `EXTRACTION_BASELINE_COMMIT` is used for
future drift comparisons.

## Tag / rollback decision

**BASELINE_TAG_REQUIRES_OWNER_ACTION.** No tag points at the baseline commit.
The normal repository convention is `internal-ha-<commit>`, so the expected
tag is `internal-ha-9fa2a2526e27d01cc7089d804b35c2a9c7ef435c`. Creating and
pushing a tag is a release action and is outside this Managed transaction.
The immutable commit is a precise provisional rollback reference, but the
canonical Phase-0 roadmap requires an approved baseline tag or an explicit
governance exception; neither was supplied here.

## Candidate universe and precedence

The audit independently discovers 263 candidate files from EP runtime,
Operations Console assets/tests, EP documentation/ADR, extraction tooling,
onboarding/runner adapters and every workflow. The manifest does not define
this discovery set. It freezes discovery digest
`10daaf423079298b088bdd122a258ae6a5e01a352dbd7f766b3bf4b724aa7be1` and
semantic-manifest digest
`4dc3d56a05333888015e03ed731be3e810b35a7afe38f3c4fad03f20ab4c78ee`.

Rules resolve by **most-specific path wins**. Equal-specificity overlap is an
error. Every candidate must resolve exactly once; an added candidate changes
the digest and fails the audit until consciously reconciled.

| Control | Result |
| --- | --- |
| Candidate universe / exactly once | `263 / 263` |
| Unclassified / ambiguous | `0 / 0` |
| Operations Console candidates / classified | `17 / 17` |
| EP product source / Python inspected | `76 / 58` |
| DJConnect, Home Assistant, repository-local blocking imports | `0 / 0 / 0` |

## Reproduction

```sh
python3 scripts/engineering/audit_ep_extraction_baseline.py --check
python3 scripts/engineering/audit_ep_extraction_baseline.py --projection
python3 -m unittest tests.engineering.test_ep_extraction_baseline
```

The projection is timestamp-free. Two identical executions must have matching
semantic output. Fixtures protect duplicate paths, invalid classifications,
unsafe paths, missing paths, unclassified candidates, equal-specificity
overlap, deterministic file overrides and newly introduced EP files.

## Gate result

The manifest-completeness P0 is resolved, but the roadmap-required baseline
tag decision remains owner-gated. Therefore the only truthful result is:

```text
PARTIAL — PHASE 0 FOLLOW-UP REQUIRED
```

No product/runtime behavior, extraction, standalone repository, namespace
rename, SQLite migration, data-root change, active-writer change, launch
service change, Inbox routing change, project identity migration, Local
Consumer API, credentials, lifecycle, lease, queue, merge or Finalization
authority changed in this increment.
