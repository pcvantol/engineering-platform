# Phase 3 post-extraction evolution provenance

The Engineering Platform implementation was history-preservingly extracted,
and every subsequent divergence is governed and traceable.

Extraction equivalence is not permanent current-head hash equality.  The
immutable Stage 1 record proves the historical DJConnect source at
`3668eb77fc89418003ae60eeb72c8391e90c3055` against extraction target
`d4d538559796f64f1ffa5136698dd207589a4ae0`.  Its input baseline and rendered
receipt are retained byte-for-byte as historical evidence.

Stage 2 reads `PHASE_3_POST_EXTRACTION_EVOLUTION_LEDGER.json` (schema v2). Its
bounded DAG has immutable baseline nodes, explicit intermediate/current/retired
nodes, and typed responsibility-flow edges (`MODIFY`, `MOVE`, `RENAME`,
`SPLIT`, `MERGE`, `REPLACE`, `RETIRE`). Cycles, duplicate edges, invented
responsibilities, orphan current nodes, dangling intermediates, and nonterminal
responsibility flows fail closed. Explicit SHARED ownership is the sole way a
responsibility may reach more than one current implementation.

Each current
historical target is either unchanged or has a receipt containing its baseline
and current content hashes, path/disposition, responsibility coverage, reason,
PR identity and governed commit chain.  The verifier checks the declared
commits exist and are reachable from the standalone lineage anchor to the
checked-out current head.  A working-tree mutation, disappearance, rename or
replacement that is not covered by such a receipt fails closed.

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
