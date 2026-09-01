# Standalone EP zero-loss capability audit (B8E)

**Terminal classification:** `B8E_REPAIR_PLAN_REQUIRED`

The canonical detailed matrix is [STANDALONE_ZERO_LOSS_CAPABILITY_MATRIX.json](STANDALONE_ZERO_LOSS_CAPABILITY_MATRIX.json).

## Baseline and method

Historical authority is `pcvantol/djconnect` at source SHA
`3668eb77fc89418003ae60eeb72c8391e90c3055`; extracted history baseline is
`d4d538559796f64f1ffa5136698dd207589a4ae0`. The manifest is version 2 at
`docs/engineering/extraction/EP_2X_EXTRACTION_MANIFEST.json` and the independent
receipt is `docs/provenance/PHASE_3_INCREMENT_1_EQUIVALENCE_BASELINE.json`.

The audit compared manifest-owned source, tests, docs, entrypoints and dashboard
assets to `origin/main` at `f84a0a610a2826902f75a583a8d609c855e0f244`. Read-only
installed-runtime inspection reported a running, installed, `empty-valid`
CENTRAL. No execution was submitted or dispatched. Matrix validation is
deterministic via `scripts/engineering/validate_zero_loss_capability_audit.py`.

## Findings

| Area | Status | Evidence-based conclusion |
| --- | --- | --- |
| HTTP/CLI ingress and scoped admission | PRESERVED_AND_LIVE | CENTRAL accepts a normalized submission with project/repository scope. |
| Legacy-file transport | PRESERVED_NOT_WIRED | Historic polling/archival adapter has no installed standalone route. |
| Topology and Agent trust | PRESERVED_AND_LIVE | Server and Agent pair/register/attach; B8C-R owns final multi-project proof. |
| CENTRAL→Agent→provider dispatch | MISSING | Agent has no receive/run execution command. This is CRITICAL_PRE_B9. |
| FIFO/mutation/lease/retry/recovery | PRESERVED_NOT_WIRED | Extracted implementation is checkout-bound, not connected to CENTRAL. |
| Finalization/reports/receipts | PRESERVED_NOT_WIRED | No standalone execution reaches terminal lifecycle. |
| Prompt History | PARTIALLY_PRESERVED | CENTRAL records submission digest only; execution/result/detail linkage is absent. |
| Console | PARTIALLY_PRESERVED | Installed Console is a project/topology JSON shell, not dashboard parity. |
| Console localisation/mobile | PRESERVED_NOT_WIRED | Historic five-language assets are not served by CENTRAL. |
| Installation lifecycle | PARTIALLY_PRESERVED | Server/Agent commands qualify; repair/update/uninstall retention parity does not. |

No capability is silently retired. The checkout-bound watcher transport is
retired, but its admission, queue, lease, retry/recovery, Prompt History,
telemetry and finalization responsibilities are separately classified.

## Console and assurance audit

Historic Console surfaces included queue ordering/actions, current execution,
run history, Prompt History/details, reports/receipts, provider/model usage,
timing/telemetry, host/validation/qualification state, recovery,
configuration/logs/health/update state, responsive presentation and
localisation. The matrix classifies these individually; the raw topology page
is not treated as an equivalent replacement.

Existing unit/browser/CI tests preserve extracted implementation and foundation
controls. They do not prove live dispatch, full Console, terminal evidence or
update lifecycle. Those are missing assurance, not a passing replacement.

## Repair DAG

```text
B8D.1 + B8C-R -> B8F-B dispatch/queue/recovery -> B8F-C finalization/evidence
                    |                              -> B8F-D telemetry/usage
                    -> B8F-A Console wiring
B8F-E installer lifecycle (parallel; blocks cutover)
```

| Lane | Acceptance evidence |
| --- | --- |
| B8F-A | Installed multi-project Console/browser proof of retained views/actions and five locales. |
| B8F-B | Authenticated CENTRAL→Agent dispatch, Agent worktree/provider execution, CENTRAL queue/lease/retry; two-project isolation canary. |
| B8F-C | Validation, PR/review/merge wait, reports, receipts, Prompt History and exactly-once terminal proof. |
| B8F-D | Project-scoped timing, provider/model usage, telemetry and export projections from lifecycle evidence. |
| B8F-E | Install/lifecycle, repair/upgrade/uninstall, retention and artifact-provenance qualification. |

B8F-B is critical pre-B9. B8E can become `B8E_ZERO_LOSS_PASS` only after every
blocking matrix gap is repaired or qualifiedly reclassified.

## Pre-B9 rule

```text
B8C_PASS + B8D_PASS + B8E_ZERO_LOSS_PASS + execution protocol ready -> B9
```

Historical receipts remain immutable; this audit records present capability reachability.
