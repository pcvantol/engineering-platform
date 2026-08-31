# Engineering Platform 2.x extraction baseline

**Status:** Phase 0 complete; Phase 1 complete / qualified; schema 40 active

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

The audit independently discovers 294 candidate files from EP runtime,
Operations Console assets/tests, EP documentation/ADR, extraction tooling,
onboarding/runner adapters and every workflow. The manifest does not define
this discovery set. It freezes discovery digest
`1d5583613ff90b42ba0d829dc643e0225e9a3440f132d2667c927f620908b224` and
semantic-manifest digest
`6829c770ec6d5da77b39647cb5929504cced79dd981e874c13d72400fe352773`.

Rules resolve by **most-specific path wins**. Equal-specificity overlap is an
error. Every candidate must resolve exactly once; an added candidate changes
the digest and fails the audit until consciously reconciled.

| Control | Result |
| --- | --- |
| Candidate universe / exactly once | `294 / 294` |
| Unclassified / ambiguous | `0 / 0` |
| Operations Console candidates / classified | `17 / 17` |
| EP product source / Python inspected | `99 / 80` |
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
records the implemented and post-merge-qualified Increment 2 loopback runtime
with schema 39 active. Increment 3 is complete / qualified: its EP-owned
registration lifecycle, consumer-side Keychain adapter and schema-40 activation
are current repository truth. It adds no consumer cutover or Local API mutation.

```text
PASS — PHASE 0 COMPLETE
```

No product/runtime behavior, extraction, standalone repository, namespace
rename, SQLite migration, data-root change, active-writer change, launch
service change, Inbox routing change, project identity migration, Local
Consumer API, credentials, lifecycle, lease, queue, merge or Finalization
authority changed in this increment.
