# Deterministic validation and publication delivery

Assignment: `L2-EP-DETERMINISTIC-VALIDATION-PUBLICATION-V1-20261007`.
Directive: `L234-NEXT-BACKLOG-DELIVERY-V1-20261007`.
Lane revision: **r32**, plan revision **1**, sole writer **LANE_2_WORK**.
Base: `a5924d614eeb07cc3a78bb3a351bcea14aa59232` (EP 2.3.111).
Branch: `codex/ep-deterministic-validation-publication-v1`.
State: **IMPLEMENTING / NOT QUALIFIED**.
Candidate package: **2.3.112**.

## Code, evidence and missing delta

| Selected boundary | Existing production path | Required delta |
| --- | --- | --- |
| SA-VAL | ExecutionHost executes selected candidate-bound controls and persists terminal receipts | Remove the additional model assessment; deny missing, stale or mismatched receipts and recheck candidate without provider dispatch |
| SA-PUB | managed_publication durable intent, compare-and-set, exact readback before create and after uncertainty | Reuse qualified MPR; prove linked deterministic validation plus independent Q/S through installed runner and crash recovery |
| SA-CTX prerequisite | provider_context projects downstream mandatory sections | Regression demonstrates later safety/acceptance sections silently omitted after role-budget overflow; retain complete mandatory context with explicit overflow telemetry |
| SA-ISO prerequisite | CodexCliClient returns copied review observations; assurance is serial | Reset every observation on entry/error and isolate invocation ownership and callbacks; no parallel assurance or source writers |
| SA-OBS prerequisite | existing durable provider invocation ledger and command/control receipts | Bind mandatory Q/S invocations to exact candidate/profile and preallocated identity; persist errors and unknown measurements without zero substitution |

## Admission and qualification boundaries

One clean checkout and worktree at pickup, no own active source/test process,
no Git operation lock or open EP PR. Two historical stashes, advisory browser
lock, r30/native holds and closed r31 consumption (three corrective rounds)
remain preserved. Remote main independently verified through GitHub API;
the waiting own fetch was stopped without changing refs.

First material step: actual provider_context regression on Python 3.14.8:
four failures, one per downstream role, because safety context is missing.
The fix retains complete mandatory sections, including neutral nested headings,
routes mandatory quality/security roles explicitly and measures overflow.

Actual transport-adapter and adoption regressions: **35 tests PASS**, including
two separate `os._exit(73)` process boundaries (assurance dispatch and accepted
first publication). Recovery waits for the real 90-second lease expiry. The
same candidate and terminal control receipts survive; only separate Q/S turns
are dispatched, publication's exclusive external receipt prevents another
create, and repair consumption remains unchanged. Source/regression evidence
is not a final-main installed claim. Ledger dispatch and terminal events share
one canonical identity and are counted as one turn; interrupted observations
retain unavailable usage/duration.

Use existing controls, ledger, adoption, publication adapter and recovery.
Qualification uses local synthetic repositories and temporary storage; only
external model/GitHub transport doubles. No paid provider, live publication
canary, production data, new live EP, signing or operational installation.
L3/L4 JOIN is independent of L2. Historical predecessor remains closed.

Required completion: full owning regressions and strict changed-file line
coverage >80.2%; exact-head independent Quality/Security; protected delivery
and ordinary finalization; exact final-main noneditable wheel outside the
checkout; linked positive, negative and real process-restart receipts;
sanitized terminal register and cleanup. This document records pickup, not
qualification of any roadmap family.
