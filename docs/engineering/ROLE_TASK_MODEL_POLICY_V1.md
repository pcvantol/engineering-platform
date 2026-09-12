# EP role/task model policy V1

**Increment:** `EP_ROLE_TASK_MODEL_POLICY_V1`. **Owner:** Engineering Platform.
**Parent:** `SA-ROLE` / finding `SA-F09` in the
[subagent roadmap](../development/SUBAGENT_ORCHESTRATION_V1_ROADMAP.md).
**Status:** design only; implementation, activation and installed qualification
are **PLANNED**. Canonical after protected merge; **NO_BUMP**.

The owner requested an architecture/design increment for selecting different
AI providers, models and reasoning effort by engineering task and agent role,
including operational configuration. This record adds no live settings, model
purchase, provider invocation, credential, Mission, run or retry authority.
The [scoped roadmap](../development/ROLE_TASK_MODEL_POLICY_V1_ROADMAP.md) and
[non-executable DAG](../development/ROLE_TASK_MODEL_POLICY_V1_DAG.json) refine
SA-ROLE rather than establish another orchestrator or policy platform.

## 1. Current evidence versus target

Source pin: `pcvantol/engineering-platform`
`4a6dace73ccc15146a9afe5db356957d8af4bbbb`, inspected 2026-09-12 UTC.
These are source observations, not installed-service or model-quality tests.

| Observed source | Meaning / retained boundary |
| --- | --- |
| [execution_executor.py](../../src/engineering_platform/execution_executor.py), `CodexCliClient.review/invoke/validate` | Inspected command assembly uses Codex, role prompts and sandboxing but no explicit per-role model/effort arguments. Metadata/usage helpers do not prove complete attribution. |
| [capability_review.py](../../src/engineering_platform/capability_review.py) | Separate quality/security labels plus twelve optional specialists. Existing role selection is not model selection; keyword-selection repair remains SA-SEL. |
| [providers.py](../../src/engineering_platform/providers.py) | Codex resolves EP's managed launcher. A deterministic validation executor exists. Provider status grants neither execution nor network authority. |
| [codex_observability.py](../../src/engineering_platform/codex_observability.py) | Explicit runtime metadata/usage is a foundation. Requested values, provider observations and model self-description are not interchangeable. |
| [execution_timeout_policy.py](../../src/engineering_platform/execution_timeout_policy.py) | Host-owned immutable per-action deadlines, not editable dashboard preferences. Routing cannot raise or disable them. |
| [policy architecture](POLICY_GOVERNANCE_AND_ASSURANCE_PROFILES.md) | EP owns effective profiles, admission snapshots and the shared repair budget. Reuse this authority and CENTRAL. |

SA-F09 stays historical and OPEN until implementation and qualification exist.
This inspection proves no particular model better, cheaper, available on an
account or independent of another reviewer. No commercial model ID, price or
unsupported CLI flag is selected by this design.

## 2. Ownership and delivery boundary

**SA-SEL** chooses relevant optional specialists and their capacity allowance.
**SA-ROLE** binds an already selected task/role to an eligible provider/model/
effort profile. **The existing EP host** authorizes and dispatches that invocation.
These are separate decisions; none creates a Forge Action or another scheduler.

EP owns execution/model policy, provider installations, assurance, leases,
consumption and evidence. Forge supplies supported constraints in an immutable
request; it cannot override EP ceilings or control an EP provider directly.
Forge's own planning policy remains Forge-owned and may use different models
on another machine. Workspace retains human project/governance UX; the EP
Operations Console administers the same EP services. Forge Platform retains
deployment authority. No shared policy DB, direct peer SQL or credential broker.

The first shipping slice supports different qualified models/efforts through the
existing EP-managed Codex adapter/session. No separate API subscription is required
or silently selected. Other CLI providers, metered APIs and local-model services
are extension classes only, each requiring a real qualified adapter, approved
profile and execution/data/auth/cost contract. A text-generation API alone does
not provide tool-use or repository execution. Unsupported integrations remain
explicitly unavailable, not usable because a provider name exists in a dropdown.

## 3. Logical records and identity

These TARGET records refine existing EP configuration, effective profiles and
invocation/evidence storage. They are not new SQL tables, implemented classes or
active wire schemas. Reuse owning records; version a genuinely missing seam in
its later implementation rather than creating a second authoritative copy.

| Record | Required logical fields |
| --- | --- |
| `ModelCapabilityObservation` | provider/adapter version, installation/host, opaque account reference, model ID/alias, tools/sandbox/output/context/effort capabilities, supported metadata/usage signals, source/time/expiry and UNKNOWN/unsupported states |
| `RoleModelProfile` | immutable ID/revision/digest; provider/account; model selection mode/reference; supported effort/default; task/risk applicability; rubric/output refs; complete mandatory context; data boundary; finite ceilings; ordered qualified alternates; qualification refs |
| `RoleTaskAssignment` | activation/source revision; project/repository scope; canonical phase, registered role, task/risk predicates; unique priority within scope; profile ref and permitted overrides |
| `EffectiveRoleModelPlan` | admitted assignment/profile set and alternatives, contributing policy revisions, requirement intersection/conflicts and digest, linked to the existing effective run profile |
| `InvocationSelection` | invocation/attempt/run/Action/repair ordinal; phase/role/task; candidate SHA; profile/rubric/context digests; selected profile; requested model/effort; capability evidence; reason and remaining allowance before handoff |
| `InvocationObservation` | provider-owned outcome; separately observed model/effort or NOT_REPORTED; identity-match classification; usage/timing/enforcement sources; original/retry/supersession links |

An explicit model ID can be a vendor alias, not immutable model weights. Preserve
that distinction. `PROVIDER_DEFAULT` must be explicitly approved and qualified;
it is not absent configuration. Where exact observed identity is required but
not provable, deny admission or retain required assurance as UNRESOLVED. Never
copy requested values into observed fields. Free-form agent claims are not
runtime attestation; validate versioned provider-owned event envelopes.

No authfile scraping or secret enumeration for discovery. Supported non-generating
model/session reads establish only what they actually observe. An unsupported
model list stays UNKNOWN, not proof against a separately qualified explicit
binding. A generative probe requires separate authority/budget, never page refresh.

## 4. Task/role matrix

Task keys below are documentary classes to map to actual host phases and registered
roles in RMP-CONTRACT; they do not add lifecycle phases. Profile names describe
purpose, not a ranking of named models. Before activation each must bind an actual
supported model/effort and qualification; unresolved placeholders are not runnable.

| Task key | Role / mode | Profile intent and invariant |
| --- | --- | --- |
| IMPLEMENTATION_CODE | implementer / bounded writer | `implementation_balanced`: qualified code/tool use; approved deeper-analysis profile for evidenced complexity/risk, never a second writer |
| IMPLEMENTATION_DOCUMENTATION | implementer / bounded writer | `documentation_efficient`: smaller/faster candidate only after docs-scope qualification; file extension alone cannot downgrade code/config risk |
| REPAIR_CORRECTIVE | implementer / bounded writer | `repair_diagnostic`: compatible initial binding, preapproved escalation only; same budget and full revalidation/re-review |
| QUALITY_REVIEW | quality / read-only | `quality_correctness`: correctness, edge cases, architecture/maintainability and relevant tests against every applicable criterion |
| SECURITY_REVIEW | security / read-only | `security_boundaries`: trust, authorization, credentials/data and abuse cases; no cheaper substitute unless independently qualified for this role |
| SPECIALIST_REVIEW | selected registered specialist / read-only | `specialist_<role>`: domain rubric and real consumer; selection/capacity stays SA-SEL; advisory is not mandatory PASS |
| FAILURE_DIAGNOSIS | existing diagnostic responsibility / read-only | `diagnosis`: bounded interpretation; no self-authorized correction or retry |
| FINALIZATION_REASONING | existing finalization responsibility / phase-scoped | `finalization_reasoning`: unresolved engineering reasoning only, preserving existing host write gates |
| VALIDATION_CONTROL | deterministic host / no LLM target | Known commands and authoritative control receipts; SA-VAL owns general integration |
| PUBLICATION_CONTROL | deterministic host / no LLM target | Known post-assurance first-PR operation; SA-PUB retains its independent qualification gate |
| RECONCILIATION_CONTROL | deterministic host / no LLM target | Known identity/receipt transitions; ambiguous diagnosis is separate, never a forced-success model call |

The deterministic targets do not claim current LLM-mediated paths were removed.
Quality and Security retain separate versioned rubrics, contexts and invocation
identities. They may use the same qualified model: a different name alone does
not prove independence. No private-reasoning/verdict sharing, inherited approvals
or implementer self-approval. Required context cannot be omitted to fit a cheaper
model; explicit overflow blocks a complete mandatory-review claim.

Effort is an adapter-supported enum, not a universal low/medium/high scale. Show
supported values and actual translation; a missing knob is N/A, not an inferred
equivalent. Profile names do not promise a latency or cost saving before measurement.

## 5. Deterministic selection, snapshots and dispatch

```text
host phase + registered role + validated task/risk evidence
  -> admitted EP policy + supported producer constraints
  -> candidate profiles
  -> capability/authority/data/qualification/capacity eligibility
  -> deterministic preference resolution or explicit conflict
  -> persist InvocationSelection; reserve existing allowances
  -> same EP provider boundary with per-invocation settings
  -> validate output AND observation; preserve outcome/evidence
```

V1 uses explicit rules, not an LLM choosing its own model or an online cost learner.
Versioned host facts determine risk/complexity: validated write scope, affected
capabilities, trust changes and contract size. Task prose cannot inject model flags
or label itself low risk. Recheck a changed candidate without widening authority.

Hard obligations combine by union, allowed capabilities/data scopes by intersection,
ceilings by minimum applicable limit and remaining allowance. Preference precedence:
EP default -> permitted project/repository override -> most-specific allowed
phase/task/role assignment. Equally scoped matching rules require a unique explicit
priority; ties or conflicting mandatory model constraints fail closed. No lower
scope may grant a new account, provider, network target, sandbox or exception.
Bound selectors; no arbitrary uploaded routing code.

Freeze the assignment/profile/rubric set at admission. Each invocation binds the
current candidate and repair ordinal and uses that set or a prequalified alternate.
Global edits apply to new runs by default. In-flight policy migration requires a
separate owning operation and requalification, never a browser Save. Live revocation,
credential trust, availability and expiry can still deny the next dispatch. Use
existing atomic reservation/lease/ledger mechanisms; concurrent workers cannot
spend the same remaining allowance.

Resolve arguments per invocation, not by mutating shared client state, process-wide
environment or the user's Codex config. Preserve EP-managed executable/account
identity independently of Forge. A Quality call cannot inherit another role's
model due to a race. SA-PAR still gates parallel mandatory reviews; this design
does not enable parallelism or native nested agents.

## 6. Fallback, escalation, recovery and limits

Fallback chooses an admitted qualified alternative. Escalation changes reasoning
capability for a real unresolved task. Recovery determines whether another attempt
is safe. None grants a retry, wider scope, extra spend or another corrective round.

| Observed condition | Required behavior |
| --- | --- |
| Primary unavailable before handoff | Use next eligible preapproved alternate only under current authority/capacity; persist reason first, otherwise WAIT/BLOCK |
| No qualified profile or unsupported required knob/observation | Explicit denial/unresolved; skip only genuinely optional work with a disposition, never mandatory PASS |
| Proven failure before provider execution | Existing bounded recovery may retry/change binding; keep attempt evidence and usage classification even if generation is proven zero |
| Timeout/lost response/interruption/MAY_HAVE_HAPPENED | No automatic alternate-provider call. Use owning ambiguity/readback/operator recovery, retain possible usage and reject late superseded results |
| Findings, valid failure or malformed completed result | Preserve the original; corrective work uses existing repair accounting, no reviewer shopping until PASS |
| Observed model/effort contradicts strict binding | Required assurance is invalid; reconcile possible effects/usage without blind replay or editing the observation |

Primary and alternates are a finite acyclic sequence with explicit attempt caps,
subordinate to existing stricter limits. No A -> B -> A loop, hidden quorum or
paid discovery loop. Every provider handoff counts as an invocation; every actual
corrective dispatch uses the shared allowance. Current EP permits at most three
total corrective rounds per run/continuation lineage, not per role, phase or model.
No reset via provider, SHA, PR, resume, activation or replacement run identity.

The current timeout policy stays a read-only hard ceiling. Later qualified shorter
budgets may tighten it, never raise it for a slower model. This design makes no
existing constant editable. Invocation timeout is not the run deadline or proof
of no remote work. Keep enforced admission limits, estimates and provider-reported
subscription allowance distinct. UNKNOWN usage/cost is not zero; no hard spend/
subscription cap without an actually enforceable boundary and evidence.

## 7. Operations Console and service contract

Add **Configuration -> AI task/role policies** in the existing EP Console, not
another dashboard. The same typed service can serve other authorized clients;
standalone EP does not require Forge or Workspace. Backend permission is decisive.

The matrix shows task, role, mandatory/optional, provider/account, model mode,
effort, rubric, fallback/escalation, source/override, effective revision, freshness
and qualification. Row details explain the selected/rejected alternatives. Separate
catalogue observation, adapter/session readiness, role qualification and active
policy. Missing/unavailable capabilities stay visible with reasons.

Edit -> validate -> preview redacted diff and effects on NEW runs -> required
approval -> activate with expected revision/digest -> authoritative readback.
Conflicting updates fail; writes are idempotent. Rollback appends activation history.
Active runs show their own snapshot. Save/Refresh never installs models, acquires
credentials, invokes AI or migrates running work. The existing subscription mode
stays unchanged without an explicit provider/billing decision; no API fallback.

Run/step details show requested, resolved, translated and observed settings,
observation status, invocation IDs, fallback/repair cause, usage source/coverage,
wall time, deadline and remaining allowance. Filter/export redacted history by
run/phase/role/provider/model. No raw prompts, private reasoning, authfiles or
secrets. Efficiency comparisons require comparable tasks/criteria and complete
usage; a faster incomplete review is not an improvement.

Reuse EP's design system, en/nl/de/fr/es translations, accessible modals, safe
confirmation/error patterns and browser tests. Actual new routes must join the
versioned API/OpenAPI/collection contracts; this design invents none. A needed
terminal-evidence extension is producer-versioned and consumer-qualified, not a
silent change to the current peer v1.2 contract.

## 8. Qualification and rollout

Implement the scenario families in the scoped DAG against real host/policy/adapter
modules and installed-wheel composition outside a checkout. CI may fake external
processes, not internal authorization/context/evidence gates. Exercise two distinct
model/effort bindings on one adapter, every role class, independent Q/S contexts,
no-LLM controls where qualified, and all negative paths. Keep existing coverage,
security, installed and five-language/Playwright requirements for production code.
Documentary tests alone are not execution qualification.

Before activating a real binding, qualify comparative performance on a versioned
representative defect/attack corpus with predeclared floors and permitted regression
tolerances. Include severity-weighted missed defects, false positives, unresolved
criteria, repairs, review independence, latency/failure and measured usage with
provenance. No post-hoc threshold selection or savings claim from one toy result.
Live comparison is separately authorized and budgeted; none runs in this increment.

Roll out catalogue/readback -> shadow resolution (no extra provider calls) ->
qualified same-Codex multi-model dispatch on bounded new runs -> guarded Console
activation -> integrated role-quality/evidence proof. Exact historical run snapshots
and source/installed/live availability stay separate. A config example is not a
completed migration or qualified model allocation.

Additional provider families, learned routing, speculative model races/ensembles,
new repair loops, native nested agents and general billing products are later work.
SA-VAL/SA-PUB/SA-SEL/SA-PAR retain their own scope. SA-ROLE can be qualified without
finishing SA-Q, but keeps SA-CTX/SA-OBS and the required effective-policy evidence.
No new first Forge->EP canary prerequisite and no change to parked PR #175.
