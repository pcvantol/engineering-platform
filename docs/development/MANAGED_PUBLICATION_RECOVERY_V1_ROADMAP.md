# Managed publication recovery V1 roadmap

**Increment:** `EP_MANAGED_PUBLICATION_RECOVERY_V1`
**Status:** QUALIFIED_ON_MAIN / integrated source and installed qualification complete
**Version effect:** 2.3.109
**Execution authority:** [owner assignment](https://github.com/pcvantol/forge/issues/142#issuecomment-6011925204), `L2-EP-MANAGED-PUBLICATION-RECOVERY-V1-20261006`

This roadmap normalizes the still-relevant residual scope from closed, unmerged
PR #175 (`Draft: managed post-assurance publication closure`) into the delivered
current-main capability. It does **not** reopen, merge, cherry-pick, or otherwise reactivate
that historical draft. Current `main` is authoritative.

PR #175 was closed without merge on 2026-09-22 after later EP releases had
superseded its old source baseline. One important subset was independently
reimplemented and qualified in PR #178: strict current validation evidence must
precede first Managed publication, and Quality/Security cannot compensate for
missing or failed required validation. The remaining semantics below are not
therefore assumed delivered merely because EP 2.3.101 exists.

The documentary DAG is
[MANAGED_PUBLICATION_RECOVERY_V1_DAG.json](MANAGED_PUBLICATION_RECOVERY_V1_DAG.json).

## Residual capability map

| Node | Status | Bounded result | Acceptance |
| --- | --- | --- | --- |
| `MPR-0` | DOCUMENTED | Reconcile historical #175 scope against current main and explicitly retire the old draft as an implementation source | #175 remains historical/unmerged; PR #178's strict-current-validation delivery is not duplicated; all remaining work has a current owner and acceptance |
| `MPR-ADOPT` | QUALIFIED_ON_MAIN | Typed adoption of an exact existing Managed candidate | One explicitly authorized `branch + candidate SHA` is adopted without rerunning initial implementation; current validation and independent Quality/Security run against that exact candidate; run-wide repair lineage/budget is preserved; stale/foreign/mutated candidate fails closed |
| `MPR-PUBREC` | QUALIFIED_ON_MAIN | Deterministic, duplicate-safe first-PR publication and recovery | Before create/retry the host performs exact GitHub readback; one matching open draft with expected base/head SHA is reconciled; mismatch/ambiguity blocks; uncertain acknowledgement/crash/network loss can resume without a second PR or replaying implementation; unchanged reviewed SHA remains mandatory |
| `MPR-QBUILD` | QUALIFIED_ON_MAIN | Qualification artifacts are built from exact committed source | Wheel/sdist qualification materializes exact committed `HEAD` in an isolated source tree; tracked dirty state fails before build; ignored/untracked residue cannot silently enter qualification bytes; deterministic and transport qualification use that exact build path |
| `MPR-Q` | QUALIFIED_ON_MAIN | Integrated source + installed-artifact qualification of the three residual capabilities | Current-main implementation proves adopted-candidate continuation, publication recovery/idempotence and committed-source build hardening together; source and installed-wheel evidence are distinct; no historical #175 test result is promoted to current PASS |

## MPR-ADOPT — existing Managed candidate adoption

The capability is for a candidate that already exists and is intentionally
selected for continuation. It must not be inferred from free-form prompt text.

The implemented typed contract binds:

- repository/project identity;
- exact branch;
- exact 40-character candidate commit SHA;
- explicit owner authorization for adoption;
- existing run/repair lineage where applicable;
- current validation profile and repair ordinal.

Acceptance requires:

1. no ordinary implementation provider turn is replayed merely to "adopt" the
   candidate;
2. repository preflight proves the exact branch/SHA and clean bounded checkout;
3. the candidate passes current required validation controls;
4. independent Quality and Security review the same candidate/profile;
5. an accepted repair keeps the existing run-wide repair budget and returns to
   validation plus both reviews;
6. publication authority is still granted only by the host publication gate;
7. restart/recovery cannot silently replace the candidate or create a new
   lineage.

This capability is separate from legacy *installation artifact* adoption. It
concerns Managed engineering candidate lifecycle, not EP package installation.

## MPR-PUBREC — deterministic first-publication recovery

Current main enforces post-validation/post-assurance publication ordering and
the qualified recovery/idempotence boundary described below.

Before any first-draft create attempt, and again after any uncertain result, the
host must read GitHub for the checkpointed branch and classify:

- no matching PR: create may be eligible;
- exactly one open draft with expected repository/base/head SHA: reconcile and
  continue without another create/provider turn;
- closed/merged/wrong-base/wrong-head/non-draft/duplicate result: fail closed
  under an explicit recovery/strategy decision.

An exception, timeout, process crash or lost acknowledgement after the remote
side may have accepted creation is **not** permission to call create again.
Recovery first performs exact readback.

Qualification must cover at least:

- crash before create;
- crash after remote create but before local receipt;
- network timeout with accepted remote create;
- repeated resume;
- exact existing draft;
- wrong head SHA;
- wrong base branch;
- duplicate matching candidates;
- historical closed/merged PR;
- candidate mutation between assurance and publication.

No second pull request, implementation replay or repair-budget reset is allowed
as a recovery shortcut.

## MPR-QBUILD — exact committed-source qualification build

Historical #175 work hardened qualification by building from a temporary
materialization of committed `HEAD` instead of consuming the mutable checkout
directly. PR #338 independently implements and qualifies that property against the
current release/build architecture; the old helper was not cherry-picked.

Acceptance:

- reject tracked uncommitted changes before producing qualification artifacts;
- materialize only committed source into an isolated temporary source root;
- verify its projected product version matches the selected candidate;
- build wheel and optional sdist from that isolated tree;
- deterministic/transport installed qualification consumes the same supported
  build entrypoint;
- build caches, ignored directories and untracked files from the developer
  checkout cannot change qualified candidate contents;
- retain the existing protected release pipeline and artifact provenance as
  separate publication authority.

## Relationship to SA-PUB

The Subagent Orchestration roadmap's external
`SA-PUBLICATION-CONTRACT` evidence gate is satisfied only by qualified
`MPR-Q` evidence for the applicable source/artifact. `SA-PUB` may reuse the
qualified product contract; it must not reimplement this recovery/adoption
programme.

This backlog does not make the complete subagent family a prerequisite for the
current EP Server system/multi-instance productization or for every future
canary. A concrete workflow that needs these semantics must consume them once
qualified.

## Historical registration boundaries

The original documentary registration did not authorize the following actions.
The linked owner assignment subsequently selected the bounded development,
protected delivery and isolated qualification recorded below; operational
installation, release and live publication canaries remain outside this delivery.

The original registration did not:

- reopen or merge PR #175;
- reserve its old branch or version 2.3.3;
- mutate CENTRAL, runtime, provider credentials or installed EP;
- alter the active LANE_2 system/multi-instance assignment;
- authorize a real GitHub PR creation run;
- authorize release/publication;
- reset a Managed repair budget;
- replace current strict validation/Q/S semantics;
- make historical #175 source/tests current qualification evidence.

The implementation was reconstructed from current `main`; historical draft
evidence was not promoted to a current implementation or qualification result.

## Completed delivery evidence — 2026-10-06

The [Managed candidate and publication contract](../engineering/MANAGED_CANDIDATE_PUBLICATION_CONTRACT.md)
is delivered in [PR #338](https://github.com/pcvantol/engineering-platform/pull/338),
EP **2.3.109**, protected main `66433d7a2260397ec438a3052acae8ef50a84022`.
Its tree `82fdae186b4fbba572a2fe0565eafd23f45385b1` is identical to the
independently reviewed candidate `862d8f0d70e760a2b61f9bfe7d5410bff0a73c6f`.
The [protected-main installed receipt](https://github.com/pcvantol/engineering-platform/pull/338#issuecomment-6013731687)
binds the source, exact artifact digests, process behavior and test boundaries.

| Evidence | Observed result |
| --- | --- |
| Source regression, Python 3.14.8 | PASS: 2,200 discovered, 2,198 passed, two existing skips; no required MPR scenario skipped |
| Changed production executable lines | Every changed file strictly >80.2%; minimum 82.7233% |
| Existing coverage contract | All 155 shipped modules measured and per-module combined coverage >=80.2%; aggregate combined 84.4738% |
| Independent exact-head reviews | [Quality PASS](https://github.com/pcvantol/engineering-platform/pull/338#issuecomment-6013459647), 47 tests; [Security PASS](https://github.com/pcvantol/engineering-platform/pull/338#issuecomment-6013450339), 24 tests; two correction rounds consumed |
| Hosted candidate gates | [Validation run 37443655086](https://github.com/pcvantol/engineering-platform/actions/runs/37443655086) PASS, including installed matrices, coverage, all four browser shards and localization; security, version, projection and Trusted Delivery PASS |
| Exact-main installed MPR | PASS: 22 tests plus real process crash/restart, live-lease refusal, natural lease expiry and repeated resume; one GitHub create, zero initial implementation turns, preserved run/repair identity and budget |
| Exact-main installed FIE-10 | PASS: all 34 submission-service tests, including authenticated accepted-submission identity recovery; HTTP/OpenAPI/Postman PASS |
| Exact-main version readback | PASS: 15 components agree on 2.3.109 |
| TDE observation | Workflow completed; actual assessment FAIL / repository qualification FAILED, with coverage unavailable; nonblocking under existing NFR-TDE-001, never policy PASS |

The exact-main wheel SHA-256 is
`63988e900c72b9baf2267e0fa7f2a5c64f48e01002777fa71570da6d0b6da819`;
the matching sdist SHA-256 is
`b4acfde43d1131888d610c7141b0edbef97678f50b5a7f4d6636d7703119a896`.
The clean committed-source builder produced both outside the checkout. The MPR
qualifier rebuilt and byte-compared the complete wheel, installed it noneditably
under Python 3.14.8, and used fixtures copied from the same commit without source
PYTHONPATH. The qualification entrypoints are
`tools/qualification/build_platform_wheel.py` and
`tools/qualification/mpr_installed_matrix.py`.

Real product Git, CENTRAL, lifecycle, current validation, assurance dispatch,
recovery and build services supplied the evidence. Only external provider and
credentials, GitHub PR transport and GitHub Git transport to a local bare origin
used declared deterministic adapters. Temporary test stores/processes were
cleaned by the harness; no persistent EP test instance was provisioned.

`DOCUMENTED_STATUS=QUALIFIED_ON_MAIN`, `IMPLEMENTED=TRUE`, `QUALIFIED=TRUE`,
`AVAILABLE_TO_CONSUMER=TRUE` for this source/artifact contract,
`CURRENT_RECONCILED_STATUS=COMPLETE`, and
`CURRENT_STATUS_IS_EVIDENCE_RECONCILED=TRUE`. Completion evidence is the protected
merge and exact-main receipt above. This documentary finalization is NO_BUMP.

FIE-10 is the bounded producer compatibility join to L3. This receipt does not
qualify Forge's complete FCI suite or, by itself, the separate FME effect/report
positives. The owner subsequently approved that bounded extension under this
same assignment and source writer. Its separate
[FME source/main/installed qualification](../engineering/BOUNDED_EFFECT_EXECUTION_V1.md#completed-producer-delivery--2026-10-06)
and pinned HTTP captures now supply that producer contract at 2.3.110.
This does not reopen MPR or replace its historical receipts. Total assignment
correction consumption is three after the FME extension, without replenishment.
The GP assignment and PR #175 stay closed. No subsequent Mission, subagent
programme, release or operational installation is started by this closure.
