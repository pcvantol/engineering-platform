# EP subagent orchestration and efficiency

## Scope and authority

Increment: `EP_SUBAGENT_ORCHESTRATION_AND_EFFICIENCY_V1`.
This is a retained source audit and a documented target for later engineering,
not a runtime change. It is a proposal until merged into owning EP `main`;
merging documents establishes documentary authority, not implementation,
qualification, installation, execution, publication or policy activation.
All implementation nodes in the [scoped roadmap](../development/SUBAGENT_ORCHESTRATION_V1_ROADMAP.md)
and [documentary DAG](../development/SUBAGENT_ORCHESTRATION_V1_DAG.json) are PLANNED.
This documentation increment is `NO_BUMP`.

The [Execution Host architecture](EXECUTION_HOST_ARCHITECTURE.md) retains
lifecycle ownership. [Effective assurance profiles](POLICY_GOVERNANCE_AND_ASSURANCE_PROFILES.md)
retain policy resolution, reviewer independence, finding disposition and repair
semantics. Reuse those contracts and CENTRAL services; do not add another
orchestrator, shared database, arbitrary-shell API or policy engine.

EP owns HOW: provider dispatch, validation, review, bounded repair, Git/GitHub
mutation, leases/capacity and canonical execution evidence. Forge owns WHY/WHAT,
Mission/Action derivation and hard inter-Action dependencies. Workspace projects
human decisions and evidence without execution authority. Forge Platform owns
composition/distribution/install orchestration, not EP execution. No peer SQL.

## Evidence baseline and limits

Original source audit: `pcvantol/engineering-platform` at
`62eb6c4631cc23b9e4d2a53043216be6f20bfaae`, 2026-09-09.
The earlier reconciliation to `3161a4ea5a3e1109c1dc27deb74deaa239c875e9`
added three commits without changing the investigated modules.
For this documentation increment, fresh remote `main` resolved to
`1a0ec0518f9009aff92d33d2ff96e695aec70bb8` on 2026-09-09.
The [six-commit comparison][comparison] likewise changes none of the audited
execution-host, capability-review, context, memory or provider-usage modules.
This is a source-freshness reconciliation, not a rerun of installed qualification.
A pre-publication refresh reached `0c282bacc40731221da267bcff289e0687ca945c`;
its two additional commits change release/install work and the main roadmap,
not the audited modules. Those newer roadmap entries are preserved.

Evidence labels used below:

- `SOURCE`: inspected implementation at the pinned audit revision, reconciled
  against the documentation baseline. A finding is not a qualified fix.
- `ISOLATED_DIAGNOSTIC`: copied relevant function bodies with minimal scaffolding.
- `REDUCED_HARNESS`: controlled interleaving of the relevant shared-state pattern,
  not execution of the original production adapter.
- `INFERENCE`: a conclusion about possible impact, not observed incidence.
- `TARGET`: a future contract requiring implementation and qualification.

Retained [diagnostics and limitations](evidence/subagent-audit-2026-09-09/README.md)
include scripts and original JSON outputs. The imported archive's SHA-256 is
`7ee5de8a56e69f94debb58c00f0f128d2bcdad513cf0527d07762522459f2385`.
Its five files are retained byte-for-byte; their original revision and scope
are not relabelled as newer qualification. These fixtures deliberately reproduce
bad behavior and are forensic examples, not product fallback code or acceptance
tests expected to pass after a repair. Future regressions must exercise actual
production modules and assert the corrected behavior.

No installed EP, CENTRAL dataset, real-provider quality benchmark, complete
repository suite or production cost/latency sample was inspected for this audit.
There is no claimed production incidence, quantified saving or model superiority.
The local publication-repair candidate `3ec4f898d6cae070c15feaef3320d98fc369561c`
is not part of this source evidence and must not be treated as merged or installed.

## Current source behavior

The host runs optional capability reviewers through separate read-only
`CodexCliClient.review` invocations, using `run_reviews` in parallel. This is
real role-separated invocation orchestration, not merely role labels in one
conversation. These call paths do not establish an EP-owned tree of native
nested Codex child-agents with individual assignments, budgets and reconciliation.
Provider-internal delegation or external CLI configuration was not inspected.
See [capability review][capability], [executor][executor] and [host][host].

For a successful new Managed implementation without repair/recovery, the source
route has N optional specialist invocations plus implementation, local validation,
Quality, Security and a separate first-PR publication invocation: N + 5 provider
invocations before the first draft PR. This is a structural call-path inference,
not a production measurement or a guarantee that every execution follows it.
Finalization, reconciliation and corrective/recovery work can add invocations.

Retain the good boundaries: provider-free admission and passive observation;
one mutating scope owner; fresh minimal shared repository facts; independent
read-only mandatory reviews; candidate/profile checks; structured unresolved
states; and the one persistent run-wide repair budget. The source also has a
provider-free `VALIDATION_ONLY` executor. These are foundations to reuse, not
reasons to replace the lifecycle.

## Finding register

Each finding remains OPEN for later implementation/qualification. Priority is
engineering triage, not a security severity or authority grant. Node IDs refer
to the scoped roadmap; they do not create executable Actions.

### SA-F01 — mandatory context can be omitted

`SOURCE` + `ISOLATED_DIAGNOSTIC`; priority HIGH; node `SA-CTX`.
[provider_context.py::project_context][context] selects mandatory sections but
then skips a later selected section when the byte budget would be exceeded.
The skipped section is counted as low-priority omission even if it is Safety
or Acceptance. The retained diagnostic produces 18,002 projected bytes,
two omissions, and both `safety_preserved=false` and `acceptance_preserved=false`.
[reviewer_prompt][capability] also projects Quality/Security through
`SPECIALIST_REVIEW` rather than the separate Quality role budget.

`INFERENCE`: a review can carry the digest of the full criteria while receiving
an incomplete projection. Digest equality alone does not prove complete input.
Do not label this a live exploit or claim all real prompts are affected.

### SA-F02 — optional specialist output lacks a delivery consumer

`SOURCE`; priority HIGH for efficiency; node `SA-LOOP`.
The [host's capability-review path][host] persists reviewer records and deliberately
does not inject their recommendations into the implementation context.
[reconciled_recommendations][capability] exists but is not called in that path.
A recorded advisory contribution can be useful for human inspection, but an
automatic advice -> disposition -> change -> verified outcome chain is absent
from this path. Do not infer that every advisory review has zero value.

The current no-conclusion-sharing boundary remains effective. A future typed
finding consumer is an explicit contract evolution, not permission to silently
restore raw reviewer reasoning injection.

### SA-F03 — coarse selection and unbounded-by-policy reviewer fan-out

`SOURCE`; priority MEDIUM; node `SA-SEL`.
[select_reviewers][capability] matches objective/filename substrings: `.md` selects
Documentation, `.yaml` can select ESPHome and `coordinator` can select Home
Assistant. Those markers alone do not prove the corresponding work is present.
`ThreadPoolExecutor(max_workers=len(selections))` starts up to all selected roles
from the finite twelve-role optional registry, with no wave-level concurrency
budget in that function. This is not an infinitely unbounded pool. Historical
confidence changes displayed confidence, not the selection threshold/order.
A separate admission quota reserve is not a reviewer-wave capacity scheduler.

### SA-F04 — shared mutable reviewer telemetry can cross invocations

`SOURCE` + `REDUCED_HARNESS`; priority HIGH; node `SA-ISO`.
The [host][host] supplies one client to concurrent optional reviews.
[CodexCliClient.review][executor] resets/writes shared `self.last_*` fields before
copying them into its result. A competing invocation can overwrite those fields.
The retained harness keeps A's content but attributes B's token value 202 to A,
whose expected value is 101. It illustrates the race, not production incidence
or a run of the original adapter. It does not establish cross-review content
leakage. Mandatory Quality/Security currently run sequentially specifically to
avoid this shared-telemetry problem.

### SA-F05 — mandatory assurance is not fully joined to usage evidence

`SOURCE`; priority HIGH for measurement; node `SA-OBS`.
Optional reviews explicitly call `_persist_provider_invocation` with per-result
usage. The [host's `_run_quality_assurance`][host] stores review identities,
timestamps, findings and status but does not make the equivalent per-review
usage-ledger call. This call-path gap prevents treating the ordinary ledger as
proven complete accounting for mandatory Quality/Security. It does not establish
that every possible external diagnostic lacks that information.

### SA-F06 — deterministic validation/publication still use provider turns

`SOURCE`; priority HIGH for efficiency; nodes `SA-VAL`, `SA-PUB`.
The [Managed local-validation gate][host] already resolves a validation profile,
then asks an LLM to execute/report its controls. The provider-free required-control
executor already exists for `VALIDATION_ONLY`. First draft-PR publication also
uses another provider invocation after branch, candidate and allowed operation
are fixed. Reusing deterministic adapters can remove these two orchestration
turns in eligible paths; no measured token/time saving is claimed. Diagnosis,
repair and substantive review remain reasoning tasks.

### SA-F07 — reviewer utility metrics do not prove actual adoption

`SOURCE`; priority MEDIUM; nodes `SA-OBS`, `SA-LOOP`.
[records_for_storage][capability] counts returned recommendations as accepted for
non-failed reviews and records zero rejected recommendations. [Engineering Memory][memory]
uses those counters; its average duration is zero and confidence mainly reflects
successful reviewer completion. These are not verified usefulness, accepted
findings or prevented defects. Do not rewrite historical counters into invented
acceptance evidence when correcting the semantics.

### SA-F08 — command churn can count events as separate operations

`SOURCE` + `ISOLATED_DIAGNOSTIC`; priority MEDIUM; node `SA-OBS`.
[churn_from_jsonl][usage] counts command-execution items without deduplicating
start/completion by invocation/item identity. When both events contain a command,
one real read can become two commands and one repeated read. The retained fixture
reproduces exactly that event shape. Repeated reads inferred from shell-command
fingerprints are also not exact distinct-file measurements.

### SA-F09 — role specialization and model allocation are not qualified

`SOURCE` + `INFERENCE`; priority MEDIUM; node `SA-ROLE`.
Quality and Security have separate labels/objectives but use the same generic
[reviewer prompt/schema][capability]. The inspected [CLI invocation][executor]
does not select model/reasoning effort per role. External CLI configuration and
provider-internal delegation remain unverified. Separate invocations establish
reasoning separation, not proven complementary defect detection or model diversity.
The [context benchmark][benchmark] explicitly measures structural shape, not
production cost or accuracy; retain that honest distinction.

## Target design

Everything in this section is `TARGET`, not current runtime behavior. It refines
existing effective-profile and execution contracts only through later qualified
increments. The [roadmap](../development/SUBAGENT_ORCHESTRATION_V1_ROADMAP.md)
sets exact dependencies, including the external publication-contract evidence gate.

### Complete role input — SA-CTX

Represent scope, authority, safety, acceptance and required controls as a pinned,
non-omittable contract, independently of optional history/explanatory context.
Mandatory content never disappears because an earlier section fills a byte budget.
Unknown/unstructured input must not be assumed optional. If complete required
input cannot be delivered, return an explicit overflow/unresolved state or use
an approved bounded expansion; never produce PASS for partial criteria.

Bind the source contract digest, projected-contract digest/completeness, role and
prompt/evaluator version to the invocation and candidate. Omission telemetry
counts only genuinely optional content. Give mandatory roles their actual role
projection rather than silently using the optional specialist budget. Keep exact
source content ephemeral unless an existing authorized evidence contract owns it.

### Invocation isolation and complete measurement — SA-ISO / SA-OBS

Each invocation owns its result, usage snapshots, timers, metadata, callbacks and
cancellation state. Use local immutable result data or an isolated client instance;
shared services may remain shared only when their contract is concurrency-safe.
Reset all per-attempt state, including snapshots on timeout/error. Do not fix
parallel correctness merely by claiming that a late result copy is isolated.

Allocate a canonical invocation identity before dispatch; bind it to run, role,
wave, candidate and profile. Reuse the existing provider-invocation ledger and
assurance records, not a new database. Include mandatory reviews, failures,
recovery and publication-provider work while it still exists. Append terminal
usage evidence without rewriting an immutable prior result; a compatibility
migration must preserve existing records. A missing usage sample stays unknown,
not zero. Keep authoritative observations distinct from derived estimates.

Deduplicate command events by invocation plus stable item identity. Count completed
operations once, retain partial/unknown terminals explicitly and avoid double
counting repeated output snapshots. Label command/read proxies honestly rather
than presenting a fingerprint as an exact file count. Version measurement
semantics; preserve legacy rates/counters as historical rather than backfilling
unobserved data.

### Deterministic boundaries — SA-VAL / SA-PUB

For known validation controls, invoke existing typed deterministic launchers;
retain exact control/candidate/profile identity, terminal exit codes and bounded
failure diagnostics. A successful known control needs no reasoning turn. An
ambiguous failure can enter the existing diagnosis/repair route, under the same
run and remaining allowance. Optional implementation-agent tests do not replace
the authoritative local validation gate.

First PR publication remains a separate host-owned dispatch after current local
validation, independent Quality/Security and applicable publication authority.
A future deterministic GitHub adapter creates or reconciles exactly the existing
bounded branch/PR with unchanged candidate identity. Persist dispatch intent,
handle ambiguous responses by exact identity readback, and prove restart
idempotency. No replacement PR, source mutation or test rerun inside publication.
Current validation/review/authority must be rechecked at the side-effect boundary.

`SA-PUB` cannot activate until the separately owned
`EP_MANAGED_POST_ASSURANCE_PUBLICATION_CLOSURE_V1` publication-contract evidence
is qualified for the exact relevant source/artifact. This design does not close,
reimplement or publish that repair. Pre-publication authority is not the later
PR-dependent GitHub Owner Authorization check. No expired programme grant is
revived. Host sequencing/result rejection is not a universal credential/network
barrier against arbitrary nonconforming-provider remote writes.

### Useful optional specialists — SA-SEL / SA-LOOP

Select optional roles from explicit repository capabilities, relevant components,
affected paths and task risk; objective keywords are secondary evidence, not the
sole authority. Record why each role was selected or skipped, the expected
consumer and the effective policy revision. No meaningful consumer means skip or
an explicitly requested read-only diagnostic, not automatic paid fan-out.

Bound total optional wave work and concurrency through EP capacity policy. Reserve
room for mandatory assurance and existing repair obligations; optional saturation
must not starve them. Keep one mutating owner per repository. Lack of capacity is
explicit waiting/skipping according to role necessity, never a synthetic review
PASS. Deadlines, cancellation and recovery remain host-owned; no second budget.

The future consumer accepts bounded structured findings, not private reasoning or
another reviewer's transcript. Keep original IDs, evidence references and criterion
bindings; the host records proposed/accepted/rejected/deferred/implemented/verified
as distinct dispositions with actual decision/result references. Counts of returned
recommendations are not accepted counts. Scope-expanding advice does not create
an Action, Mission, extra repair round or authority. Applicable non-blocking
residuals remain visible through the existing evidence contract.

This is an explicit evolution of today's no-conclusion-injection behavior.
Do not enable `reconciled_recommendations` as an undocumented shortcut. Independent
final reviewers must form their own verdict against the candidate and full criteria;
no mutual verdict sharing, inherited approvals or majority vote replacing required
checks. A finding consumer cannot let the implementer self-approve its correction.

### Specialization and safe parallel assurance — SA-ROLE / SA-PAR

Version the Quality and Security rubrics under the existing effective-profile
family. Quality covers applicable correctness, edge cases, maintainability and
test evidence; Security covers applicable trust/authorization/data boundaries and
abuse paths. Reuse native analyzers for mechanical rules. Record coverage and
unresolved criteria, not merely a role name and an empty findings list.

Declare any role-specific provider/model/effort policy explicitly, check capability
before dispatch and retain observed runtime settings separately from requested
settings. No assumption that another model is automatically better/independent.
Unsupported settings fail closed or use an explicitly qualified compatible fallback;
mandatory assurance requirements cannot weaken. Actual allocation changes need
comparative qualification and applicable policy authority, not documentation alone.

Only after context integrity, invocation isolation, complete accounting and bounded
capacity are qualified may Quality and Security run in parallel. Use one immutable
candidate/profile with independent invocation state and a complete-set host join.
Recheck candidate freshness; a missing, timed-out, malformed or stale result remains
UNRESOLVED. No early publication on the faster reviewer, quorum or self-approval.
The current sequential route remains valid until the replacement is qualified.

## Qualification and later pickup

The roadmap gives per-node acceptance. Required actual-production-module tests
include oversized and unstructured mandatory inputs; concurrency interleavings;
error/cancel/recovery attribution; complete mandatory invocation accounting;
idempotent event ingestion; misleading filename/keyword selection; no-consumer
skips; budget saturation; disposition provenance; deterministic no-LLM controls;
ambiguous PR creation/restart; and stale/partial mandatory-review sets.

Use comparable bounded tasks and pinned source, artifact, provider/configuration,
policy, tooling and task criteria for baseline/candidate measurements. Include
no-review, selected-review, validation-only, failure, repair and recovery paths.
Record per-role/wave invocation counts, measured wall time (including critical path,
p50/p95 over sufficient samples), token/cache usage, rate-table provenance where
applicable, coverage of unknown usage, unique valid findings and verified adoption.
Reviewer value is outcome evidence, not simply successful invocation or verbosity.
Predeclare sample sizes and acceptance/non-regression thresholds; report noise and
uncertainty. No fabricated cost reduction, zero missing costs or weaker security
coverage to make an optimization look successful.

Each implementation node needs exact-head source tests and its applicable installed
qualification before being claimed available. `SA-Q` closes the whole capability
family only after integrated measurement and non-regression evidence. It does not
block an independently qualified bounded fix from shipping. Source merge is never
installed proof. Rollouts preserve admitted in-flight policy/budget snapshots;
rollback uses the existing governed mechanism and never erases consumption.

The full family is not a first-autonomy prerequisite. A concrete context-integrity
or assurance defect affecting the selected canary requires its own bounded safety
assessment/fix; "post-autonomy optimization" is not permission to ignore it. General
native nested agents, parallel writers, a credential broker/network redesign,
full policy UI, new shared storage and new Forge planning are out of scope.
No live release/install/canary or executable programme DAG is changed here.

[comparison]: https://github.com/pcvantol/engineering-platform/compare/62eb6c4631cc23b9e4d2a53043216be6f20bfaae...1a0ec0518f9009aff92d33d2ff96e695aec70bb8
[host]: https://github.com/pcvantol/engineering-platform/blob/62eb6c4631cc23b9e4d2a53043216be6f20bfaae/src/engineering_platform/execution_host.py
[executor]: https://github.com/pcvantol/engineering-platform/blob/62eb6c4631cc23b9e4d2a53043216be6f20bfaae/src/engineering_platform/execution_executor.py
[capability]: https://github.com/pcvantol/engineering-platform/blob/62eb6c4631cc23b9e4d2a53043216be6f20bfaae/src/engineering_platform/capability_review.py
[context]: https://github.com/pcvantol/engineering-platform/blob/62eb6c4631cc23b9e4d2a53043216be6f20bfaae/src/engineering_platform/provider_context.py
[memory]: https://github.com/pcvantol/engineering-platform/blob/62eb6c4631cc23b9e4d2a53043216be6f20bfaae/src/engineering_platform/engineering_memory.py
[usage]: https://github.com/pcvantol/engineering-platform/blob/62eb6c4631cc23b9e4d2a53043216be6f20bfaae/src/engineering_platform/provider_usage.py
[benchmark]: https://github.com/pcvantol/engineering-platform/blob/62eb6c4631cc23b9e4d2a53043216be6f20bfaae/src/engineering_platform/provider_context_benchmark.py
