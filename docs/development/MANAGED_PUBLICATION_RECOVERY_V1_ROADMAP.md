# Managed publication recovery V1 roadmap

**Increment:** `EP_MANAGED_PUBLICATION_RECOVERY_V1`  
**Status:** PLANNED / FUTURE_WORK  
**Version effect:** NO_BUMP documentation only  
**Execution authority:** none

This roadmap normalizes the still-relevant residual scope from closed, unmerged
PR #175 (`Draft: managed post-assurance publication closure`) into current
future work. It does **not** reopen, merge, cherry-pick, or otherwise reactivate
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
| `MPR-ADOPT` | PLANNED | Typed adoption of an exact existing Managed candidate | One explicitly authorized `branch + candidate SHA` is adopted without rerunning initial implementation; current validation and independent Quality/Security run against that exact candidate; run-wide repair lineage/budget is preserved; stale/foreign/mutated candidate fails closed |
| `MPR-PUBREC` | PLANNED | Deterministic, duplicate-safe first-PR publication and recovery | Before create/retry the host performs exact GitHub readback; one matching open draft with expected base/head SHA is reconciled; mismatch/ambiguity blocks; uncertain acknowledgement/crash/network loss can resume without a second PR or replaying implementation; unchanged reviewed SHA remains mandatory |
| `MPR-QBUILD` | PLANNED | Qualification artifacts are built from exact committed source | Wheel/sdist qualification materializes exact committed `HEAD` in an isolated source tree; tracked dirty state fails before build; ignored/untracked residue cannot silently enter qualification bytes; deterministic and transport qualification use that exact build path |
| `MPR-Q` | PLANNED | Integrated source + installed-artifact qualification of the three residual capabilities | Current-main implementation proves adopted-candidate continuation, publication recovery/idempotence and committed-source build hardening together; source and installed-wheel evidence are distinct; no historical #175 test result is promoted to current PASS |

## MPR-ADOPT — existing Managed candidate adoption

The capability is for a candidate that already exists and is intentionally
selected for continuation. It must not be inferred from free-form prompt text.

The future contract must bind at least:

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

Current main already enforces post-validation/post-assurance publication
ordering. The remaining backlog is the exact recovery/idempotence boundary.

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
directly. Current main must re-evaluate and implement that property against its
current release/build architecture rather than cherry-picking the old helper.

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

## Explicit non-goals

This roadmap does not:

- reopen or merge PR #175;
- reserve its old branch or version 2.3.3;
- mutate CENTRAL, runtime, provider credentials or installed EP;
- alter the active LANE_2 system/multi-instance assignment;
- authorize a real GitHub PR creation run;
- authorize release/publication;
- reset a Managed repair budget;
- replace current strict validation/Q/S semantics;
- make historical #175 source/tests current qualification evidence.

On pickup, reconstruct from current `main`, compare the old draft only as
forensic design evidence, and deliver each capability through normal protected
EP review, qualification and versioning.
