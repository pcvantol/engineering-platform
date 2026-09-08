# EP policy governance and effective assurance profiles

## Decision and scope

Increment: `POLICY_GOVERNANCE_AND_EFFECTIVE_PROFILES_V1`.
This document specifies the EP-owned target in one four-repository documentation
increment. Canonical architecture only when merged into owning `main`; otherwise
`PENDING_PR`. Implementation, schema changes, API endpoints, active configuration,
version bumps, credentials, grants and installed state are **not changed here**.

The shared semantic vocabulary is maintained at
`pcvantol/forge:docs/architecture/POLICY_GOVERNANCE_AND_EFFECTIVE_PROFILES.md`.
Its proposal is [Forge #50](https://github.com/pcvantol/forge/pull/50).
EP adopts the semantics locally; Forge does not become EP's policy authority.
This product contract does not replace generic AI-development contract ownership.

The [Execution Host architecture](EXECUTION_HOST_ARCHITECTURE.md),
[producer readback contract](EP_PRODUCER_READBACK_CONTRACT.md) and
[EP roadmap](../development/ENGINEERING_PLATFORM_ROADMAP.md) retain their
respective lifecycle, evidence and sequencing authority.

## Ownership and standalone operation

EP owns execution policy resolution/activation and enforcement: admission,
validation/assurance, provider constraints, repair, leases/capacity, finalization
and receipts. Forge sends bounded intent and requested requirements; EP must
validate and enforce them together with its own rules and available capability.
Workspace presents policy, proposals and decisions through authenticated product
interfaces. Forge Platform checks compatibility and orchestrates supported
installation operations, never direct CENTRAL SQL or a second policy engine.

EP-only operation is first-class. A standalone EP can use its approved local
profile and operator authority without Forge or Workspace being online. In a
Forge-managed run it must additionally verify the exact supplied constraints and
accepted policy binding. Missing mandatory requirements are not defaulted away.

## Catalogue the existing rules before migrating them

Source evidence baseline: EP main
`51c2def28f23a5b1942ebdcbc5a240fd98fc2f23`, observed 2026-09-08.
No installed configuration is inferred from source. #100 is a separate, moving
assurance implementation proposal; its observed open head was
`c9cbe95e5205c8d2d4aa27703c7b9b0bb80704aa`. This is not qualification evidence.

| Family | Current source/form | Target treatment |
| --- | --- | --- |
| Validation selection | `src/engineering_platform/validation_profile.py`: code-defined tiers, diff/path rules and branch-specific exception | Versioned EP/project profile references; explicit applicability/exception decisions; no branch-name-only permission |
| Validation tooling | Same module: named control registry and launchers | Project-owned tool commands/configuration are referenced and pinned; EP's own Python/Console profile is not universal policy for every project/language |
| Provider time limits | `src/engineering_platform/execution_timeout_policy.py`: named immutable timeout constants | Catalogue as bounded execution policy; configurable only within approved host/product limits |
| Mandatory review | #100 candidate `execution_host.py` and review contracts | Effective assurance definition/evaluator; do not call a hardcoded severity conditional a complete policy-management system |
| Repair allowance | Run state, recovery and host repair paths | Single durable operational counter/lineage, with applicable policy/grant ceilings; no second counter in Forge |
| Merge/protected writes | Execution lifecycle and external repository rules | Distinguish default human boundary, scoped delegation and exact-head qualification; a policy flag is not a grant |
| Coverage/security baseline | Required repository CI/tool configuration | Explicit owning source/revision and non-weakenable floor for current work; not an arbitrary dashboard slider |
| Queue dispositions | #100 CENTRAL disposition proposal and authenticated server routes | Owner/operator permissions and audited state transitions, independent from UI visibility |
| Version/build/publication | Build/release helpers plus #100 versioning proposal | Execute explicit governed version/release operations; ordinary build never allocates another release version |

Catalogue each rule as INVARIANT, GOVERNED_POLICY, OPERATIONAL_CONFIGURATION,
AUTHORIZATION_GRANT, RUNTIME_FACT or IMPLEMENTATION_LIMIT. Include definition
source, supported scopes/ranges, who may change, override rules, snapshot binding,
proof tests and migration state. Preserve hardcoded safe baselines until the
replacement is actually wired and qualified. No setting creates unsupported
parallelism, provider capability or host authority.

## Definition, source and activation

Future logical records follow the shared PolicyDefinition, PolicyAssignment,
PolicyChangeProposal, PolicyDecision, PolicyActivation, EffectivePolicy and
PolicyEvaluation semantics. Their implementation belongs to existing EP
application services and CENTRAL, not an added shared policy database.

A policy family has one declared definition source. Repository-owned rules and
native formatter/linter/analyzer configurations are changed through the governed
repository route. Installation/operator settings use the existing EP writer.
CENTRAL keeps validated activations and per-run snapshots, not an independently
editable competing copy. Workspace cannot modify the source directly.

Change flow: proposal -> typed validation -> impact -> required decisions ->
immutable revision publication -> owner-controlled activation. Use expected
revision for concurrent edits and record exact actor/scope/provenance. Evaluate
policy-changing work under the previously approved profile, never under its own
proposed weaker rules. Existing authorized routine evaluation needs no new click.
Rollback is a new audited activation, not erased history or returned budget.

For cross-product changes, accept owner-specific revisions/activation receipts
under a shared change-set identity. There is no multi-product SQL transaction.
Partial activation is visible; only work requiring the unready combination waits.

## Effective resolution and protected authority

Resolve ceilings by the minimum applicable limit and remaining authorized
allowance; combine mandatory obligations without dropping controls; intersect
allowlists/scopes; override preferences only where the family permits it.
Unknown versions, unsupported requirements and conflicts are explicit denials or
unavailable states, not silently accepted defaults. No low-scope exception can
weaken the product security invariant or grant itself extra access.

Keep these independent:

```text
policy requirements + valid scoped grant + current qualification
+ actual host/resource capability + remaining consumption allowance
```

A proposed workflow is not executable code. Control launchers remain known,
versioned repository/product contracts; management UI does not run arbitrary
shell or upload unreviewed policy evaluation code.

## Materialization, admission and snapshot evidence

Before dispatch, Forge pins its effective planning/release policy, exact Action
identity and requested execution requirements. EP admission records the accepted
request and EP effective profile revision/digest/evaluator, contributing sources,
project/repository/run scope, requirement resolution and decision evidence.
The profile is not selected from mutable latest configuration after the run starts.

The receipt/readback exposes applied-policy references and any mismatch, retaining
project scope and exact identity. Forge checks that the applied profile satisfies
the materialized requirements. Matching digests without trusted origin and
validated content do not grant execution authority.

Restart, repair and new candidate SHA preserve the admitted snapshot and consumed
budget. A new policy ordinarily applies to new work. Moving an in-flight run to
a new profile requires explicit authorized migration and appropriate requalification.
Never rewrite historical reviews to match current settings or infer unknown
legacy consumption as zero. Runtime settings and grants are not repository
architecture facts.

Revocation, expiry, emergency stop and credential trust remain live controls.
Re-evaluate these before protected side effects even when a run has a pinned
snapshot. A current denial prevents new authority use without rewriting already
proven delivery. Unavailable authority freshness must not grant a write from an
indefinitely stale cached approval.

## Policy-driven quality, security and validation

The supported structure remains one existing quality step, not a user-defined
second workflow or a post-delivery cosmetic check:

```text
implementation -> local validation -> quality + security review
 -> policy evaluation -> optional implementer repair
 -> revalidation + current-candidate re-review
 -> first Managed implementation PR / qualified Genesis local delivery
 -> later qualification/merge/finalization obligations -> terminal evidence
```

The baseline requires independent quality and security reviews for applicable
mutating work. Reviewers are technically read-only and cannot self-approve a
repair. Their recommendations are not themselves authorization. Missing,
malformed, incomplete or candidate-mismatched mandatory review is UNRESOLVED.

Effective profiles select applicable criteria, reviewer roles, tool controls,
evidence requirements and finding dispositions. Mechanical formatting, import
sorting, indentation, linting and analyzers use the repository-native pinned
configuration. Do not duplicate those rules as loosely worded AI preferences.
Acceptance criteria/DoD, correctness, test relevance, architecture, maintenance,
performance and security each require their applicable concrete evidence.

Severity, confidence, criterion violation and blocking disposition are different
fields. Do not force every finding to HIGH/blocking or treat every MEDIUM as
advisory. A required acceptance criterion can block independently of a generic
severity threshold. Style/design-pattern preference is not permission for
out-of-scope refactoring. Risk acceptance requires the actual applicable decision.

Keep all bounded structured findings; no silent truncation behind a PASS.
Overflow/incomplete evidence is explicit. Preserve original findings/reviews
append-only and append actual resolution/rejection evidence. A later omission is
not automatic resolution. Non-blocking residual findings can accompany successful
delivery and reach Forge for later planning, without EP generating Missions.

Final assurance comes from a complete valid current-candidate/current-profile
review set, not `all([])` or `all(historical_reviews)`. Earlier failures remain
history without making repaired success fail forever. Bind findings bytes/digest
to the immutable terminal receipt; readback never manufactures missing evidence.
Outcome, delivery qualification and assurance disposition remain separate.

## One repair budget and immutable workflow constraints

For the current bootstrap, **at most three total corrective rounds per
run/continuation lineage**, across validation, quality, security, hosted checks
and corrective finalization. A stricter applicable grant/profile may allow less.
A larger future product limit requires explicit policy/authority approval and
cannot retroactively revive an exhausted run.

The first validation is a check, not a free correction. Reserve a repair
transactionally before corrective provider dispatch using existing CENTRAL/leases;
persist unique repair ID, lineage, input candidate, cause/findings and result.
Replays/recovery reuse the same reserved operation. New SHA, phase, PR or replacement
run ID does not replenish the allowance. Post-round-three review is allowed;
a fourth corrective dispatch is not. Non-blocking findings alone do not consume
repairs. Transport retry has its own bounded meaning, never hidden new mutation.

EP owns actual provider-repair accounting. Forge's programme/Action constraints
may further restrict admission; they are not another additive three-round loop.
Changing a future policy changes no consumed runtime fact.

## Merge, release and composition

Default human merge gates remain. Valid scoped delegation can feed the existing
authorized merge executor after exact candidate checks/reviews and applicable
Owner Authorization. `auto_merge` alone cannot satisfy that authority. Changed
head invalidates qualification, not automatically the unchanged delegation.

Native Forge release management determines intent/plan within product policy.
EP applies explicit versions via field-aware writers, qualified candidate updates,
clean exact-version builds and authorized publication. Keep one canonical version
source and separately test source and installed artifact projections. Repeated
build must not mutate source/version; external dependency fields must not change
because they happen to contain the same number.

Version changes occur before final candidate qualification; any later bot/version
commit is a new candidate. Bind idempotency to operation and expected source, not
commit-title markers or branch naming. Published identity binds version, exact
source, artifact bytes/digest and publication/qualification evidence. Major and
release decisions require explicit applicable authority. This does not implement
or approve the pending versioning changes in #100.

## Operator and consumer surfaces

Future EP owner interfaces support catalog/effective-policy reads, authorized
proposal/evaluation/activation and historical explanations. These are logical
contract requirements, not implemented endpoint claims. Preserve scoped actors,
revisions, redaction and audit; a producer credential cannot become operator
permission to quarantine/decline a queue item or modify policy.

The EP Console and future Workspace both project canonical evidence. Step-modals
show round identity/number, cause, actual correction, timestamps, revalidation/
re-review result and residual findings. Historical views use the run's policy,
not the newest one. No LLM request is needed to explain a stored policy decision.

## Qualification and migration acceptance

Require tests for weaker overrides denied; wrong owner/project/version; exact
request/applied-profile matching; stale actor/assignment; old candidate PASS
rejected; genuine FAIL -> repair -> re-review -> PASS; non-blocking findings
roundtrip; zero/partial reviews not PASS; mixed-phase fourth repair denied;
concurrent reservation/restart/revocation; policy-change self-approval denied;
legacy snapshots and budget preserved; receipt/Console/Workspace projection parity.
Distinguish deterministic lifecycle/provider seams from actual reviewer-provider
and installed production qualification. Test fixtures never become product
fallbacks for missing provider capability or authority.

Use a staged owner migration: inventory -> compatible definitions/readback ->
shadow comparison without granting writes -> qualified resolver/enforcer cutover
-> controlled management surface. No dual writers, reset or silently changed
in-flight policy. Unknown legacy records stay explicitly unresolved.

See the [policy roadmap](../development/POLICY_GOVERNANCE_V1_ROADMAP.md).
#100 remains the owning assurance implementation lane; #102 retains dependency
admission design. This document is not evidence that either implementation has
closed its findings. Only necessary canary seams precede the first dynamic
Mission loop; full policy UI and universal installer implementation do not.
