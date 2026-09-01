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

**Refresh record (2026-09-01):** PR #18 head is
`0c5b90cebc1ca997b7c4fb90d40ad48c97474c2f`; PR #17 (B8D.1) is open at
`0fae5bfd3b26dee9797fb548a93594da234393b0`; both target the main SHA above.

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

## Corrected recovery strategy

The B8E findings do **not** authorize an independent rewrite of the Console,
queue/recovery, finalization, telemetry, or Agent orchestration. The original
2.x program is a history-preserving extraction migration. Its recovery
principle is **preserve first; decompose second**.

**Phase P — extraction functional parity recovery** restores the extracted EP
core, from the installed standalone package, with the same Console, Engineering
Action lifecycle, Managed/Genesis semantics, queue/admission, provider/Codex,
recovery, validation/qualification, Prompt History, reporting/evidence, and
finalization behavior. The extracted implementation is the primary source of
truth; existing cohesive lifecycle logic is reconnected rather than replaced.

**Phase S — architectural seam decomposition** begins only after
`FULL_EXTRACTED_EP_CORE_VERIFIED`: first the front ingress seam (HTTP/CLI/file)
and then the physical execution seam (CENTRAL ↔ Project Agent), with parity
qualification across each decomposition. Phase S does not begin B9 and does
not authorize distributed execution implementation yet.

## Canonical recovery DAG

```text
history-preserving source extraction
              ↓
standalone package/install
              ↓
EXTRACTION FUNCTIONAL PARITY RECOVERY (Phase P)
              ↓
FULL EXTRACTED EP CORE VERIFIED
       ┌──────┴──────┐
       ↓             ↓
front ingress seam   physical execution seam (Phase S)
HTTP / CLI / file    CENTRAL ↔ Project Agent
       └──────┬──────┘
              ↓
distributed parity qualification → B9 → STANDALONE_EP_VERIFIED
```

| Phase | Scope and acceptance evidence |
| --- | --- |
| P | Reconnect the extracted functional core in the installed package without redesign: full Console, canonical lifecycle and legacy semantics, provider path, recovery, qualification, history, reports/receipts, telemetry and installation lifecycle. Installed-package end-to-end parity evidence is required. |
| S | After Phase P only, introduce ingress and physical-execution seams one at a time and prove every matrix capability retains equivalent behavior, isolation and evidence. |

The missing dispatch protocol remains a B9 blocker, but is a **Phase S**
concern, not the first recovery implementation. B8E can become
`B8E_ZERO_LOSS_PASS` only after Phase P and any subsequent qualified seam work
resolve or explicitly retire every blocking matrix gap.

## Pre-B9 rule

```text
B8C_PASS + B8D_PASS + B8E_ZERO_LOSS_PASS + execution protocol ready -> B9
```

Historical receipts remain immutable; this audit records present capability reachability.
