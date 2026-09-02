# Phase 3 post-extraction evolution provenance

The Engineering Platform implementation was history-preservingly extracted,
and every subsequent divergence is governed and traceable.

Extraction equivalence is not permanent current-head hash equality.  The
immutable Stage 1 record proves the historical DJConnect source at
`3668eb77fc89418003ae60eeb72c8391e90c3055` against extraction target
`d4d538559796f64f1ffa5136698dd207589a4ae0`.  Its input baseline and rendered
receipt are retained byte-for-byte as historical evidence.

Stage 2 reads `PHASE_3_POST_EXTRACTION_EVOLUTION_LEDGER.json`.  Each current
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

Current inventory: 83 `UNCHANGED`, one `GOVERNED_MODIFICATION`, zero
`UNACCOUNTED`.  `dashboard.py` retains its responsibility at the same path and
is governed by the standalone Operations Console composition in PR #22.
`inbox_watcher.py` remains Stage-1-equivalent at its extracted standalone path;
its historical extraction adaptation is not a current standalone mutation.

Target-only modules are intentionally outside this historical mapping.  They
remain normal post-extraction product development and do not modify either
Stage 1 digest.  At final cutover, both Stage 1 and Stage 2 are required to
show that every historical EP responsibility is accounted for.

Run the control against the designated read-only historical checkout:

```sh
python3 tools/extraction/verify_phase3_equivalence.py \
  --source /private/tmp/djconnect-extraction-source-3668eb77 --target .
```
