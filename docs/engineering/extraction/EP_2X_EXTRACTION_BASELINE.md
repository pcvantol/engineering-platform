# Engineering Platform 2.x extraction baseline

**Status:** Phase 0 complete; Phase 1 / Increments 1-2 implemented; schema 39 activation pending

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

**BASELINE_TAG_SATISFIED.** The immutable baseline commit is tagged as
`internal-ha-9fa2a2526e27d01cc7089d804b35c2a9c7ef435c`. It is the approved
rollback reference required by the Phase-0 roadmap.

## Candidate universe and precedence

The audit independently discovers 288 candidate files from EP runtime,
Operations Console assets/tests, EP documentation/ADR, extraction tooling,
onboarding/runner adapters and every workflow. The manifest does not define
this discovery set. It freezes discovery digest
`3fc84651e82f86f9aa715d19353872d3bcc30d42422f7d52e7e1967e103ebce6` and
semantic-manifest digest
`4dc3d56a05333888015e03ed731be3e810b35a7afe38f3c4fad03f20ab4c78ee`.

Rules resolve by **most-specific path wins**. Equal-specificity overlap is an
error. Every candidate must resolve exactly once; an added candidate changes
the digest and fails the audit until consciously reconciled.

| Control | Result |
| --- | --- |
| Candidate universe / exactly once | `288 / 288` |
| Unclassified / ambiguous | `0 / 0` |
| Operations Console candidates / classified | `17 / 17` |
| EP product source / Python inspected | `95 / 76` |
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

The Phase-0 manifest, baseline tag and deterministic audit controls are all
satisfied. **Phase 1 / Increment 1 — Local Consumer API Contract Foundation**
is complete. It remained contract-only: it introduces neither a live transport
nor authentication, credential, storage or consumer cutover runtime. ADR-0021
now records the implemented Increment 2 loopback runtime; schema 39 activation
remains a separately governed post-merge operation.

```text
PASS — PHASE 0 COMPLETE
```

No product/runtime behavior, extraction, standalone repository, namespace
rename, SQLite migration, data-root change, active-writer change, launch
service change, Inbox routing change, project identity migration, Local
Consumer API, credentials, lifecycle, lease, queue, merge or Finalization
authority changed in this increment.
