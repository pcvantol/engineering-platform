# Phase 3 post-extraction evolution provenance

The Engineering Platform implementation was history-preservingly extracted,
and every subsequent divergence is governed and traceable.

Extraction equivalence is not permanent current-head hash equality.  The
immutable Stage 1 record proves the historical DJConnect source at
`3668eb77fc89418003ae60eeb72c8391e90c3055` against extraction target
`d4d538559796f64f1ffa5136698dd207589a4ae0`.  Its input baseline and rendered
receipt are retained byte-for-byte as historical evidence.

Stage 2 reads `PHASE_3_POST_EXTRACTION_EVOLUTION_LEDGER.json` (schema v3). Its
bounded DAG has immutable baseline nodes, explicit intermediate/current/retired
nodes, and typed responsibility-flow edges (`MODIFY`, `MOVE`, `RENAME`,
`SPLIT`, `MERGE`, `REPLACE`, `RETIRE`). Cycles, duplicate edges, invented
responsibilities, orphan current nodes, dangling intermediates, and nonterminal
responsibility flows fail closed. Explicit SHARED ownership is the sole way a
responsibility may reach more than one current implementation.

Each current historical target is either unchanged or has a receipt containing
its baseline and current content hashes, path/disposition, responsibility
coverage, reason, PR identity and governed commit chain. A receipt is either
**CURRENT_PHASE_PROVENANCE**, whose declared commits must exist and remain
reachable from the standalone lineage anchor to the checked-out current head,
or an explicitly **SEALED_PREDECESSOR_PHASE_EVOLUTION**. A working-tree
mutation, disappearance, rename or replacement that is not covered by such a
receipt fails closed.

## Governed phase baselines

A **GOVERNED_PHASE** is a bounded, approved delivery phase. Its
**PHASE_EVOLUTION_COMMITS** remain auditable in its immutable receipts. At
completion it may declare one **PHASE_COMPLETION_BASELINE** and a
`COMPLETE` seal in `governed_phase_seals`. The seal names every predecessor
receipt it covers and hashes their canonical ledger entries; it also records
the approved completion baseline and approval reference. Sealing is never
inferred from a commit subject or a path name.

For a sealed predecessor, the verifier requires the seal to be intact, its
completion baseline to be reachable from the current head, the baseline to
contain the sealed destination content, and each historical evolution commit
to remain present with forward internal chronology. It does not require those
internal commits to be direct ancestors of every later phase head. This makes
sequential governed phases robust to a governed squash completion while
preserving auditability.

For the current phase, commit-chain reachability remains fail-closed. Missing
or tampered seals, absent completion baselines, ambiguous receipt ownership,
missing current-phase chains, and unaccounted paths all fail qualification.
`P-CENTRAL-CORE` is the first sealed predecessor phase: completion baseline
`a0694530ea54fac9a47e0898738105dfc719b935` seals its listed receipts without
rewriting their historical evidence.

The separate lineage anchor is required because the final historical target
commit was reconciled on a parallel extraction branch.  The verifier proves
that every mapped target blob at the anchor equals the immutable target before
using normal EP history for Stage 2; this is rebase/squash robust without
reinterpreting historical evidence.

Current inventory: 68 `UNCHANGED`, 15 `GOVERNED_MODIFICATION`, one
`INTENTIONAL_RETIREMENT`, and zero `UNACCOUNTED`. `dashboard.py` retains its
responsibility at the same path, first governed by the standalone Operations
Console composition in PR #22 and updated by the Phase-P transition baseline
in PR #23. PR #23's other retained historical targets have explicit
post-extraction lineage receipts; the historical project-local
`database_maintenance.py` module is intentionally retired. This provenance
bookkeeping describes source evolution only; it does not claim CENTRAL
migration completion.

Target-only modules are intentionally outside this historical mapping.  They
remain normal post-extraction product development and do not modify either
Stage 1 digest.  At final cutover, both Stage 1 and Stage 2 are required to
show that every historical EP responsibility is accounted for.

Run the control against the designated read-only historical checkout:

```sh
python3 tools/extraction/verify_phase3_equivalence.py \
  --source /private/tmp/djconnect-extraction-source-3668eb77 \
  --target . --qualified-revision HEAD
```

## Cutover assurance contract

`P-PROV POST_EXTRACTION_LINEAGE_READY` requires immutable Stage-1 verification,
real current-tree Stage-2 verification, complete responsibility accounting, zero
unaccounted paths/responsibilities, bidirectional lookups for a baseline-unchanged
and a governed target, and focused production-validator coverage of split, merge,
chained split-to-merge, missing/duplicate responsibility rejection, explicit
sharing, cycles, and chronology. It also requires the repository's B8E, full
suite, projection, TDE, security, and exact-head hosted qualifications.

`EXHAUSTIVE_COMBINATORIAL_E2E_CANARY_MATRIX = NOT_REQUIRED` for this cutover
gate. Such a fixture matrix would duplicate the validator's combinatorial
semantics without materially increasing assurance beyond immutable end-to-end
verification, real-tree qualification, and focused production-validator tests.
Additional fixtures remain permitted as future hardening, but are
`OUT_OF_SCOPE_FOR_CUTOVER_ASSURANCE`.
