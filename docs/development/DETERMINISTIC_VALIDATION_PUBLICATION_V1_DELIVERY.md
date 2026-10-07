# Deterministic validation and publication delivery

Assignment: `L2-EP-DETERMINISTIC-VALIDATION-PUBLICATION-V1-20261007`.
Directive: `L234-NEXT-BACKLOG-DELIVERY-V1-20261007`.
Lane revision: **r32**, plan revision **5**, sole writer **LANE_2_WORK**.
Base: `a5924d614eeb07cc3a78bb3a351bcea14aa59232` (EP 2.3.111).
Branch: `codex/ep-deterministic-validation-publication-v1`.
State: **QUALIFIED IMPLEMENTATION MAIN / ORDINARY FINALIZATION**.
Package: **2.3.112**, finalization **NO_BUMP**.

## Code, evidence and missing delta at pickup

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

Independent review wave 1 on `f8ce16d91e2b46f02dff9a25c1e792dd55ac8c75`
found a context parser P1 (tabs/fenced samples) and a ledger-consumer P2
(double counting and unknown-to-zero). Correction round **1** retains heading
levels from the parser, preserves fenced material, explicitly includes mandatory
review rubrics in the delivered prompt, and shares canonical turn folding across
usage, terminal host evidence, cumulative activity and the read contract.
All **51** focused context/ledger/consumer regressions pass. Security's first
wave was interrupted by model capacity and is not a PASS. Fresh independent
exact-head reviews and complete qualification remain required. The closed r31
consumption stays three; this assignment has consumed one corrective round.

Independent wave 2 on `e2c956e9c864985b52b150ed099ccd9906e6a217`
confirmed the first fixes and found two remaining gaps: unrecognised headings
could still lose obligations, and non-object terminal churn could crash shared
readers. Correction round **2** preserves all unknown sections and ambiguous
markup; only the explicitly classified optional-history vocabulary permits
omission. Both decoded binding halves must be objects before canonical folding;
corrupt halves remain distinct unavailable observations. **58** focused actual
context, adapter, storage and consumer regressions pass. This assignment has
consumed **two** corrective rounds; r31 remains closed with three unchanged.
Exact-head independent reviews, complete source/installed qualification and
protected delivery are still required; this is not a terminal qualification.

Quality wave 3 on `d713f91f9bfbfe19a33fba7d43aa0dc8546aa9ba`
identified remaining setext/empty-heading/literal-hash ambiguities. The final
correction round **3** preserves the **entire already approved objective** for
every role. Textual history omission is removed from this safety subset;
Markdown cannot prove an instruction optional. Correct role/rubric delivery
and explicit nominal-budget overflow remain. Historical benchmark counters
are preserved and make no token-reduction claim. **59** focused real prompt,
adapter, storage and consumer regressions pass. Fresh complete reviews and
qualification are required on this final candidate. This assignment has
consumed **three** corrective rounds; no further correction budget is implied.
Closed r31's three rounds remain separate and unchanged.

Use existing controls, ledger, adoption, publication adapter and recovery.
Qualification uses local synthetic repositories and temporary storage; only
external model/GitHub transport doubles. No paid provider, live publication
canary, production data, new live EP, signing or operational installation.
L3/L4 JOIN is independent of L2. Historical predecessor remains closed.

Required completion: full owning regressions and strict changed-file line
coverage >80.2%; exact-head independent Quality/Security; protected delivery
and ordinary finalization; exact final-main noneditable wheel outside the
checkout; linked positive, negative and real process-restart receipts;
sanitized terminal register and cleanup. This document closes the selected SA-VAL/SA-PUB implementation subset and its
necessary prerequisites; it does not close the full roadmap family.


## Protected implementation and installed qualification

Implementation [PR #344](https://github.com/pcvantol/engineering-platform/pull/344)
merged through protected squash as `a4b192e90f9e5658b610b10a53b5a05b09b6e455`.
Its tree `55eb4543d67b866a22d91c655898c171e2da2ef9` equals the independently
Quality/Security-reviewed `dacf51c03ab0e4d1a83e90a126b6394680fd183c` candidate.
[Exact independent reviews](https://github.com/pcvantol/engineering-platform/pull/344#issuecomment-6040236862)
and [source/coverage/candidate installed evidence](https://github.com/pcvantol/engineering-platform/pull/344#issuecomment-6040405874)
are separate from final-main qualification.

Full source: **2,263 tests PASS, one existing skip**, Python3.14.8. All **163**
modules meet combined coverage; aggregate **84.8850987781%**, minimum module
**80.3203661327%**. All **nine** changed Python production files have strictly
more than80.2% executable-line coverage, minimum **82.7613727055%**. No waiver
or exclusion was introduced. Four browser shards/localization, CodeQL,
dependency/static security, trusted delivery, canonical versioning and the
installed CI matrix all passed. Actual Owner Authorization status was
SUCCESS/Not required for LOW_RISK or NORMAL_RISK; no admin bypass.

[Fresh noneditable protected-main qualification](https://github.com/pcvantol/engineering-platform/pull/344#issuecomment-6040754336) outside checkout: **108 tests
PASS**, with real local Git/CENTRAL/controls and two `os._exit(73)` process
crashes, true lease expiry, separate Q/S and one exact publication readback.
All **194** committed test files were Git-blob verified, **190** installed
package files matched wheel/source before and after tests, and **15** version
projections passed. Implementation-main wheel SHA256:
`0619f6dab614c2e81d537002f9bb1209990b6addbd797cf48067e66cdbfb825d`.
Mechanical SA-VAL/SA-PUB invoke no model; the substantive independent reviews
remain genuine separate turns. The approved objective stays complete: no
context/token-reduction or commercial-model-quality claim.

Candidate TDE policy assessment is **FAIL**, repository qualification **FAILED**,
`repository-qualification.cdc836cdd513954765e5e237`, with unavailable coverage.
This is the existing nonblocking NFR-TDE-001 boundary, not policy PASS inferred
from workflow success. Final-main TDE readback remains explicit in the final
receipt. Hermetic validators are qualified; full host-loopback EP/Forge
validators and native/signing holds remain unsupported/unqualified.

Ordinary NO_BUMP finalization changes documentation only. Exact final-main
noneditable qualification, sanitized linked process captures and cleanup are
recorded after its protected merge on the finalization PR and owning #142.
No operational runtime/public release or next-family pickup follows closure.
SA-Q/SEL/LOOP/ROLE/PAR and broader SA-OBS utility/churn claims remain unqualified.
