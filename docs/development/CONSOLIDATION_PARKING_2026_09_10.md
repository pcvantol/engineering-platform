# EP consolidation and parked repair / installation work

Increment: `FOUR_REPO_CONSOLIDATION_PARKING_2026_09_10`, reconciled 2026-09-10.
Scoped record under [EP roadmap](ENGINEERING_PLATFORM_ROADMAP.md).
[Documentary DAG](CONSOLIDATION_PARKING_2026_09_10_DAG.json).

## Current disposition

Physical local repository cleanup is complete; retained remote product/source
and provenance refs remain parked. This is consolidation and parking, not
execution of the next task.
Keep PR #175 OPEN/DRAFT/PARKED and do not alter its head, mark ready, merge,
close/recreate it, dispatch reviews/runs, or change the primary installation.
This `NO_BUMP` documentation is a separate change from that repair. It becomes
canonical through its own protected merge and does not qualify #175.

PARKED is planning state, not cancellation, runtime dismissal, stopped-process
proof, grant revocation/activation or repair-budget reset. Old installer and
qualification handoffs are not automatic resumption instructions. Any later
work requires an explicitly selected bounded objective and current authority.
Missing capabilities are recorded, not implemented as a side effect.

## Evidence classification and pinned identities

SOURCE_VERIFIED: GitHub main/heads/PR metadata observed 2026-09-10.
HOSTED_QUALIFICATION: actual Actions conclusions on the exact PR head.
USER_REPORTED: the owner's two EP architecture reports and detailed closure
checklist supplied in this task. LOCAL_READBACK_VERIFIED: direct Git checkout,
worktree, branch and stash readback before the temporary documentation worktree
was created. No runtime/CENTRAL inspection or qualification was performed;
reported tests and runtime claims remain attributed rather than promoted.

- Pre-documentation main: `4145429c3447728ce8a23c055d8d0b1451107759`,
  equal locally and on `origin/main`.
- Repair branch: `codex/ep-managed-post-assurance-publication-closure-v1`.
- Remote/PR head: `9ac3bd24e6842b4a1f2da356791261c9524f4893`.
- Historical original: `7c347f887ddd01b15f70234a324c2de2fd9844a2`.
- Owner-reported patch-equivalent lineage: `4b54b41...`.
- Owner-reported pre-checkpoint candidate: `56c3b912cda7d0a9fc911c5b3a298083e44b3fa9`.
- Candidate version: owner-reported 2.3.3, one intended 2.3.2 -> 2.3.3 PATCH.

[Heads](https://api.github.com/repos/pcvantol/engineering-platform/git/matching-refs/heads/) ·
[PR #175](https://github.com/pcvantol/engineering-platform/pull/175) ·
[original repair-base-to-candidate comparison](https://github.com/pcvantol/engineering-platform/compare/d329852b06f71a10e13b91dd1d1f87982ef6b90d...9ac3bd24e6842b4a1f2da356791261c9524f4893).
The pinned comparison is 21 commits ahead/0 behind across 28 files. This is
retained source, not a merged or installed repair. The new documentation PR is
bookkeeping, not an additional implementation lane or duplicate of #175.

## Every retained durable remote non-main ref

| Ref / exact tip | Assessment and disposition |
| --- | --- |
| codex/ep-managed-post-assurance-publication-closure-v1 / `9ac3bd24e6842b4a1f2da356791261c9524f4893` | PARKED_NOT_QUALIFIED; retain #175 and all source; do not merge or delete |
| codex/ep-operational-installation-record-v1 / `4026ad1e671ad86d23dc9f8c56b7c4d5ddeb8819` | PARKED_REMOTE test residual. #113 merged head `13900601a5ef7f84ec074410e0d51087d83e8214`; this tip adds a later guard-test commit. Compare residual assertions against future owning main before any selective delivery or retirement |
| codex/ep-version-operation-reconciliation-v1 / `63bb8f14bbbd176109f4da1d20e2d96dc3081125` | PARKED_REMOTE version-operation residual. #125 merged earlier head; #126 closed unmerged. Reconcile the prepared/applied discrepancy against actual operation evidence before any delivery or retirement |
| parking/2026-09-10/ep-local-stash-residual / `2982ff8ca42085e09eecf92de38e37bb71d64eec` | PARKED_UNREVIEWED_SOURCE / NOT_QUALIFIED / NOT_DELIVERY. Classify all preserved residual bytes against future owning main before any delivery |
| parking/2026-09-10/ep-release-2.3.1-prior-tip / `3c934ee2b1c432463c140e7dc8cee007a8a46531` | HISTORICAL_PROVENANCE_PRESERVED only; not active product work and not an execution predecessor |
| release-2.3.1 / `2ff4ee7441b8d8bade6c39e2c52dd62187af87ef` | RELEASE_HISTORY. Divergent release identity is not a feature-cleanup candidate; do not delete or rewrite published identity |

Evidence: [#113](https://github.com/pcvantol/engineering-platform/pull/113),
[post-merge test commit](https://github.com/pcvantol/engineering-platform/commit/4026ad1e671ad86d23dc9f8c56b7c4d5ddeb8819),
[#125](https://github.com/pcvantol/engineering-platform/pull/125),
[#126](https://github.com/pcvantol/engineering-platform/pull/126).
The earlier operational-record/version ancestry counts describe ancestry, not
semantic equivalence. All full tips above were fetched/read back before this
reconciliation. `release-2.3.1 = RELEASE_HISTORY`.

## Local cleanup is resolved

USER_REPORTED and LOCAL_READBACK_VERIFIED before the temporary documentation
worktree: one local `main` worktree, zero local feature branches, zero stashes
and zero unpreserved WIP. Local `main` equalled `origin/main`. The durable
residuals are remote refs, not active local development. This documentation
delivery creates one temporary isolated worktree/branch and removes it after
protected merge; it does not touch #175, the parked refs, runtime or CENTRAL.

## #175: historical parking publication is not lifecycle qualification

SOURCE_VERIFIED: #175 remains OPEN, DRAFT and PARKED_NOT_QUALIFIED at exact head
`9ac3bd24e6842b4a1f2da356791261c9524f4893`. It was created at
`2026-09-10T07:46:09Z`; its body says the candidate is paused and not qualified
through the required product route. The owner confirms it was opened on
explicit request as a visible parking place.

Therefore:

- retain #175 as the parking artifact; do not treat its creation as a new
  Managed-run publication receipt;
- current later assurance or readback cannot establish that this historical
  first-create happened after assurance;
- branch matching alone is not canonical run/legacy lineage;
- closing/recreating would not erase the event and is not authorized here;
- future existing-PR qualification and a fresh first-publication proof are
  different claims. Choose the proof strategy explicitly before any future
  publication. Do not create a second PR automatically to make a metric pass.

The original strict first-publication DoD remains unsatisfied by this PR.
Source delivery, existing-PR qualification and fresh publication-path proof
must be reported separately, not silently substituted for one another.

Explicitly: `#175 hosted/source tests != durable current Managed lifecycle
qualification`, and `#175 existing draft != proof that first draft creation
happened after assurance`.

## Technical evidence and outstanding assurance

USER_REPORTED on exact `9ac3bd24...`: 1,557 source tests PASS; exact committed
2.3.3 wheel; installed-wheel suite PASS; coverage 84.59% aggregate, 129/129
modules and minimum 80.29%; installed P-TRANSPORT matrix; deterministic
Genesis/Managed/controlled-recovery E2E; HTTP/OpenAPI/Postman; browser validation
with one permitted retry; projection/route guard/compile/version consistency
and git diff --check PASS. Original logs, full artifact digest and execution
identities were not supplied to this documenting session.

HOSTED_QUALIFICATION: all six PR workflows returned completed/success on the
pinned head: EP validation [34451629707](https://github.com/pcvantol/engineering-platform/actions/runs/34451629707),
Golden [34451629720](https://github.com/pcvantol/engineering-platform/actions/runs/34451629720),
Trusted Delivery [34451630023](https://github.com/pcvantol/engineering-platform/actions/runs/34451630023),
TDE observe [34451629654](https://github.com/pcvantol/engineering-platform/actions/runs/34451629654),
Security baseline [34451629687](https://github.com/pcvantol/engineering-platform/actions/runs/34451629687),
CodeQL [34451629754](https://github.com/pcvantol/engineering-platform/actions/runs/34451629754).
These are not independent LLM Quality/Security or a real Managed lifecycle.

Owner-reported dispositions are retained together, without collapsing them:
`SOURCE_QUALIFIED__OPERATIONAL_CLOSURE_PENDING` for technical repair;
`NOT_QUALIFIED` for owning lifecycle/merge decision;
`PARTIALLY_CLOSED` for technical remote-write prevention.

Current local-validation lifecycle evidence = UNVERIFIED;
current Quality = UNRESOLVED; current Security = UNRESOLVED;
exact candidate/profile durable binding = UNVERIFIED.
Historic Quality timeouts yielded no valid result; Security's HIGH replay/
idempotency issue was reportedly repaired in `56c3b91`. Required final closure
of QR-LOCAL-VALIDATION-PASS-001 and SEC-001 is not inferred from code/tests.
Host sequencing, phase prompts and early-result rejection are not universal
capability-level prevention of every HTTP/credential remote-write route.

## Parked decisions and documentary DAG

| Node | Disposition and retained future acceptance |
| --- | --- |
| E-LOCAL | RESOLVED — one local main worktree, zero feature branches/stashes/unpreserved WIP |
| E-TEST-RESIDUAL | PARKED_REMOTE at `4026ad1e671ad86d23dc9f8c56b7c4d5ddeb8819`; reconcile post-#113 guard tests against future owning main |
| E-VERSION-RESIDUAL | PARKED_REMOTE at `63bb8f14bbbd176109f4da1d20e2d96dc3081125`; resolve the prepared/applied discrepancy against actual operation evidence |
| E-LOCAL-STASH-RESIDUAL | PARKED_UNREVIEWED_SOURCE at `2982ff8ca42085e09eecf92de38e37bb71d64eec`; classify all preserved residual bytes against future owning main before any delivery; parking is NOT_QUALIFIED / NOT_DELIVERY |
| E-RELEASE-PRIOR-TIP | HISTORICAL_PROVENANCE_PRESERVED at `3c934ee2b1c432463c140e7dc8cee007a8a46531`; provenance only, never active product work or an execution predecessor |
| E-PR-STRATEGY | PARKED_DECISION_REQUIRED — keep #175 draft now; later select existing-PR versus fresh-publication proof without rewriting history |
| E-RUNTIME | PARKED_CAPABILITY_UNVERIFIED — prove a supported isolated candidate-wheel DEVELOPMENT route; primary 2.3.1 stays untouched |
| E-QUALIFY | PARKED — freeze exact SHA/version/profile and applicable authority; one real Managed run, durable current validation, separate independent Q/S and unchanged shared repair budget |
| E-PUBLICATION-PROOF | PARKED — host decision/readback/recovery proof under the selected strategy; #175 is not first-create-after-assurance proof |
| E-MERGE | PARKED_SEPARATE_AUTHORITY — separate level-B decision, protected checks/authorization and merge/main ancestry |
| E-PRODUCER | PARKED — later Forge readiness may test only the actually required producer capability; this closure does not decide that all of #175 or all installer work is required |
| E-LATER | PARKED — installer/provisioner/update/release productization, Project Hygiene, policy UI, Agent fleet and subagent optimization remain under existing graphs |

USER_REPORTED primary runtime is 2.3.1 and lacks the required typed adoption/
strict durable validation/readback behavior. Earlier authorization for an
isolated attempt is historical context, not permission to dispatch in this
parking task. Development credential references do not prove authenticated
admission. Do not copy primary CENTRAL/tokens, fabricate records, select test
fakes, or reset budget using a fresh data root. The reports mention earlier
review permission for `a7b2661...`; no current exact-head authority is verified
here. Revalidate actual applicable scope before future dispatch, rather than
inventing an approval or assuming a version string supplies authority.

Version route remains the existing `tools/qualification/advance_platform_build.py`
and bootstrap release cadence; do not add a generic VersionPreparationDelivery
adapter/receipt requirement solely to execute this ordinary repair. Preserve
the single intended PATCH and establish its true operation reference. New SHA
invalidates candidate-bound evidence, not a license to reset consumed repairs.

Documentary advancement of main does not authorize rebasing #175 now. At a
later explicit resumption, refreeze and requalify any changed candidate. Store
final receipt evidence outside the frozen source candidate where the owning
contract permits, so a retrospective doc commit cannot silently stale reviews.

## Existing later roadmaps retained

[Subagent design/roadmap](SUBAGENT_ORCHESTRATION_V1_ROADMAP.md),
[Project Hygiene](PROJECT_HYGIENE_V1_ROADMAP.md),
[Policy](POLICY_GOVERNANCE_V1_ROADMAP.md) and the main RL/OI installation lanes
keep their detailed nodes/criteria. The new graph is a parking/evidence overlay,
not an executable replacement or a new all-installer gate for Forge E2E.
No broad credentialbroker/network redesign, installer work or optimization is
started by this record. Cross-product pickup remains indexed by
[Forge](https://github.com/pcvantol/forge/blob/main/docs/roadmap/CONSOLIDATION_PARKING_2026_09_10.md).

No source repair, #175 head/state, primary runtime/CENTRAL, grant, budget,
release, installation, qualification or canary was changed. Physical local
consolidation is complete; every remaining durable source/provenance ref and
product decision is explicitly parked above.
