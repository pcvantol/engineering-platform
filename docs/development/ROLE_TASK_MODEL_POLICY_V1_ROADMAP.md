# EP role/task model policy — SA-ROLE implementation roadmap

**Increment:** `EP_ROLE_TASK_MODEL_POLICY_V1`. **Status:** PLANNED.
This is a decomposition of existing **SA-ROLE**, not an additional execution
programme or a new first-canary gate. The [architecture](../engineering/ROLE_TASK_MODEL_POLICY_V1.md)
and [documentary DAG](ROLE_TASK_MODEL_POLICY_V1_DAG.json) define the task/role
matrix, effective provider/model/effort policy, fallback and admin contract.
The [parent roadmap](SUBAGENT_ORCHESTRATION_V1_ROADMAP.md) retains every existing
node and dependency; SA-F09 is not closed by documenting its solution.

## Delivery sequence


| Node | Delivery | Internal dependencies |
| --- | --- | --- |
| RMP-CONTRACT | Task/role matrix and versioned rubrics | none |
| RMP-CATALOG | Qualified provider/model capability catalogue | RMP-CONTRACT |
| RMP-RESOLVE | Effective policy and deterministic selection | RMP-CATALOG |
| RMP-DISPATCH | Per-invocation model and effort application | RMP-RESOLVE |
| RMP-EVIDENCE | Requested-versus-observed ledger/readback | RMP-DISPATCH |
| RMP-CONSOLE | Task/role administration and explanation | RMP-RESOLVE |
| RMP-Q | Installed and comparative role qualification | RMP-EVIDENCE, RMP-CONSOLE |

```text
RMP-CONTRACT -> RMP-CATALOG -> RMP-RESOLVE -> RMP-DISPATCH -> RMP-EVIDENCE --+
                                  |                                    |
                                  +-------> RMP-CONSOLE ----------------+-> RMP-Q
```

RMP-RESOLVE additionally requires SA-CTX, SA-OBS and RMP-EFFECTIVE-POLICY.
SA-OBS retains its SA-ISO predecessor. RMP-EFFECTIVE-POLICY is a narrow evidence
gate for the needed POL-E service subset; it neither claims that subset is
already qualified nor requires the full cross-product policy programme/UI.
The parent SA-ROLE completes only when RMP-Q and its original dependencies
are satisfied. No child depends on SA-ROLE/SA-Q, avoiding a decomposition cycle.
Existing SA-VAL/SA-PUB/SA-SEL/SA-PAR gates are consumed where their paths are
claimed, not reimplemented or required wholesale before model routing.

First qualify multiple model/effort profiles on the SAME managed Codex adapter
and existing session mode. This first slice does not purchase/require API tokens
or pretend other vendors have qualified execution adapters. Later CLI/API/local
families reuse the same contract but each needs explicit scope/auth/cost and
adapter qualification. Concrete model names and supported effort values are
selected from actual capability evidence at activation, not invented in docs.

## Mandatory qualification families


| ID | Required behavior |
| --- | --- |
| RMT-01 | Deterministic task/role/risk selection; equal-priority conflict and prompt-supplied model override rejected |
| RMT-02 | Two different model/effort bindings through the same adapter; per-call arguments and observed metadata remain distinct |
| RMT-03 | Unsupported model/tool/sandbox/effort/context/qualification denies dispatch rather than weakening requirements |
| RMT-04 | Override intersection and stale activation rejected; edits do not rewrite an active run snapshot |
| RMT-05 | Explicit provider-default and absent observations remain labelled; strict observed-identity requirements fail closed |
| RMT-06 | Independent full Q/S rubric/context and candidate matching; cheap model cannot drop criteria or approve its own repair |
| RMT-07 | Controlled concurrent adapter interleavings do not mix models/results/usage; no implicit SA-PAR enablement |
| RMT-08 | Finite pre-dispatch fallback: approved alternate succeeds, cycle/unauthorized alternate and exhausted capacity denied |
| RMT-09 | Timeout/ambiguous handoff never automatically switches provider; late superseded result cannot materialize twice |
| RMT-10 | Repair across roles/models/SHA/PR/restart retains one runwide three-round ceiling and existing timeout limits |
| RMT-11 | Qualified deterministic control paths use zero model calls; missing SA-VAL/SA-PUB integration is not mocked into success |
| RMT-12 | Failure/cancel/replay usage attributed once; unknown usage/cost/actual model never becomes zero or requested value |
| RMT-13 | Console authority, concurrent edits, five locales, redaction and preview/readback; refresh creates zero generations |
| RMT-14 | Installed identity, persisted policy and restart evidence; unqualified/failed comparative corpus blocks binding activation |
| RMT-15 | Versioned producer evidence compatibility; no silent v1.2 schema break or consumer authority expansion |
| RMT-16 | Account/data/billing boundary enforced; unavailable subscription never silently selects a metered API |

RMP-DISPATCH may deliver a bounded headless pilot before the admin UI if its own
applicable runtime/installed/model qualification is complete. Do not mark full
SA-ROLE or RMP-Q complete from that pilot; the complete task/role matrix, Console,
observation provenance and comparative evidence remain required.

RMP-CONSOLE reuses the EP design system, existing API/services, en/nl/de/fr/es and
current UI/security/Playwright requirements. Configuration preview is read-only;
activation requires current authority and expected revision. The existing timeout
policy is displayed as a fixed ceiling, not a new editable phase-timeout form.

RMP-Q uses the real policy/host/adapter in deterministic CI and installed
qualification outside a checkout. External fixture models prove routing behavior,
not the quality/availability of commercial models. Real allocation qualification
uses a versioned comparative corpus, predeclared quality floors and measured
usage under separate explicit authority. No budget increase, metrics fabrication,
mandatory-review weakening or retry-until-green. A required missing capability
is a gap, not an expected-failure counted as completion.

## Pickup and closure

Refresh EP main/open PRs and reconcile SA-F09 plus existing provider/profile/
ledger code before implementation. Reuse prior qualified evidence where exact
source/profile/adapter bindings remain valid. Keep source, installed, active
policy and measured quality distinct. Do not mutate current runs, grants,
credentials, default account/model, PR175 or the Forge canary for this plan.

Documentation completion is NO_BUMP and may add offline documentary guards only.
It does not implement any RMP runtime node or alter executable programme DAGs.
The canonical EP roadmap already reaches this plan through its subagent section
and SA-ROLE; the policy roadmap links the same family instead of duplicating it.

## Adaptive Action sizing consumer contract

The [execution-envelope/fit design](../engineering/ACTION_EXECUTION_ENVELOPE_V1.md)
and [three-node owning DAG](action-execution-envelope-v1.json) provide EP's part
of ADAPTIVE_ACTION_SIZING_V1. AS-E-OFFER/FIT/Q reuse qualified RMP catalogue,
effective profile, SA context/observability and admission subsets; they do not
replace or add a backward dependency to any existing RMP node. Forge decides
Action decomposition; EP checks ALL required execution/review profiles and
preserves profile/fit/admission/observed identity. Fit is non-generating, not
permission, reservation or a new run. All new implementation stays PLANNED.

No mandatory-context truncation, stale-profile fallback, hidden paid API usage,
raised timeout or reset corrective lineage. Shared AS scenarios qualify EP's
real adapter/admission path; Forge mocks alone cannot prove EP enforcement.
No first-canary or full-Console/installer dependency is introduced.
