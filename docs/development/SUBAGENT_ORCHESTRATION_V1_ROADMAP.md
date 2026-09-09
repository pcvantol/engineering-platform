# EP subagent orchestration and efficiency roadmap

Scoped lane under [Engineering Platform Roadmap](ENGINEERING_PLATFORM_ROADMAP.md).
Owning audit/target: [Subagent orchestration and efficiency](../engineering/SUBAGENT_ORCHESTRATION_AND_EFFICIENCY.md).
Increment: `EP_SUBAGENT_ORCHESTRATION_AND_EFFICIENCY_V1`.

The [documentary DAG](SUBAGENT_ORCHESTRATION_V1_DAG.json) owns the hard
`depends_on` edges for this lane. The table below is its matching human-readable
projection; update/check both together. This JSON is documentation, not a Forge
programme, scheduler input, execution envelope or permission to start work.
No existing executable DAG, Mission, run or policy activation is changed.

## Status and pickup priority

`SA-0` records this documentation; it becomes canonical only through protected
main delivery. All nine implementation nodes and the `SA-Q` integrated
qualification node remain PLANNED, with no qualification evidence claimed.
The original 2026-09-09 source audit is pinned at `62eb6c4` and reconciled to
`0c282bacc40731221da267bcff289e0687ca945c`; later work must fetch again and
reconcile each OPEN finding against current code before implementing it.

Start with mandatory-context correctness and invocation isolation. Correct the
measurement foundation before making cost/value claims. Then take the independent
validation, selection, findings-consumer and role-specialization lanes. Optional
parallel assurance follows its actual prerequisites; more agents are not the goal.
Priority is not an additional hard dependency beyond the DAG.

| Node | Status | Hard dependencies | Findings | Bounded delivery and acceptance |
| --- | --- | --- | --- | --- |
| SA-0 | DOCUMENTED | none | SA-F01, SA-F02, SA-F03, SA-F04, SA-F05, SA-F06, SA-F07, SA-F08, SA-F09 | Retain the source audit, target design and documentary DAG. Pinned sources, nine finding IDs, forensic limits, discoverable links and acyclic dependencies; canonical only on protected main. |
| SA-CTX | PLANNED | SA-0 | SA-F01 | Preserve the complete mandatory role contract. Production-module tests: oversized first/combined sections, unstructured input, actual mandatory-role routing and explicit overflow; no omitted required safety/acceptance under PASS. |
| SA-ISO | PLANNED | SA-0 | SA-F04 | Isolate invocation results, telemetry and cancellation state. Real adapter tests with controlled parallel interleavings, distinct usage/content, success/failure/timeout/cancel and snapshot reset; no cross-invocation attribution. |
| SA-OBS | PLANNED | SA-ISO | SA-F05, SA-F07, SA-F08 | Complete the invocation ledger and correct measurement semantics. Mandatory Q/S and failure/recovery IDs join usage/timing; unknown remains unknown; event replay/id deduplication; legacy proxy counters stay historical and are not fabricated adoption. |
| SA-VAL | PLANNED | SA-CTX, SA-OBS | SA-F06 | Execute known validation controls without an LLM turn. Actual Managed and validation-only paths retain candidate/profile/exit evidence, full required controls and bounded failure diagnostics; zero provider calls for eligible deterministic control execution. |
| SA-PUB | PLANNED | SA-VAL, SA-PUBLICATION-CONTRACT | SA-F06 | Use a deterministic host-owned first-PR publication adapter. Current validation, both independent reviews and authority precede dispatch; unchanged SHA; ambiguous response/restart reconciles one exact PR with no new provider turn or duplicate publication. |
| SA-SEL | PLANNED | SA-OBS | SA-F03 | Select useful optional specialists under bounded capacity. Capability/path/risk selection rejects misleading md/yaml/coordinator triggers; selected/skipped rationale and consumer recorded; finite wave/concurrency allowance, mandatory work reserved and saturation tested. |
| SA-LOOP | PLANNED | SA-CTX, SA-OBS | SA-F02, SA-F07 | Join bounded specialist findings to genuine dispositions. Actual proposed/accepted/rejected/deferred/implemented/verified transitions with references; no-consumer skip; no private-reasoning/approval sharing, invented adoption or scope/repair-budget expansion. |
| SA-ROLE | PLANNED | SA-CTX, SA-OBS | SA-F09 | Qualify role rubrics and explicit provider/model/effort policy. Versioned Q/S coverage, unresolved criteria, requested versus observed settings, incompatible-capability denial and comparative detection quality; preserve existing effective-profile authority. |
| SA-PAR | PLANNED | SA-CTX, SA-ISO, SA-OBS, SA-SEL | SA-F04, SA-F05 | Qualify bounded parallel mandatory reviews. Independent read-only invocations on one immutable candidate/profile; complete-set join, timeout/failure/cancel/stale tests and no early publication; preserve sequential behavior until qualified. |
| SA-Q | PLANNED | SA-VAL, SA-PUB, SA-SEL, SA-LOOP, SA-ROLE, SA-PAR | SA-F01, SA-F02, SA-F03, SA-F04, SA-F05, SA-F06, SA-F07, SA-F08, SA-F09 | Qualify integrated correctness and measured efficiency. Exact-source/artifact/provider/profile comparative corpus, predeclared thresholds, complete usage coverage and wall time, unique verified findings and non-regression; installed proof separate from source merge. |

## Dependency shape

This is a compressed view; the table and JSON name every exact edge.

```text
SA-0 -> SA-CTX
SA-0 -> SA-ISO -> SA-OBS
{SA-CTX, SA-OBS} -> SA-VAL
{SA-VAL, SA-PUBLICATION-CONTRACT} -> SA-PUB
SA-OBS -> SA-SEL
{SA-CTX, SA-OBS} -> SA-LOOP
{SA-CTX, SA-OBS} -> SA-ROLE
{SA-CTX, SA-ISO, SA-OBS, SA-SEL} -> SA-PAR
{SA-VAL, SA-PUB, SA-SEL, SA-LOOP, SA-ROLE, SA-PAR} -> SA-Q
```

`SA-PUBLICATION-CONTRACT` is an external evidence gate owned by EP, not another
implementation node. It refers to `EP_MANAGED_POST_ASSURANCE_PUBLICATION_CLOSURE_V1`.
Its qualification is REQUIRED_EVIDENCE_UNVERIFIED in this graph. Before `SA-PUB`
activation, verify the actual owning publication contract and exact qualified
source/artifact. A local candidate, a handoff, or this documentation is not proof.
Do not reimplement the existing repair as part of this design.

## Existing contract joins

Reuse the [policy/profile lane](POLICY_GOVERNANCE_V1_ROADMAP.md) for effective
profile semantics and reviewer/finding policy; this graph does not duplicate
POL-E/POL-B/POL-Q services or require all future policy administration UI.
Use the existing provider ledger, readiness, lease, deterministic-control,
review/repair and finalization boundaries. Product installation and release
operations retain their own evidence and authority. No new peer startup dependency.

The `SA-LOOP` typed-result consumer is an explicit future contract evolution.
Today's no-conclusion-sharing boundary is not silently changed by this record.
No raw reasoning/transcript injection, reviewer mutual approvals or implementer
self-approval. Role allocation must not infer actual models from requested settings.
General native nested-agent trees and parallel repository writers are out of scope.

## Canary positioning and delivery gates

The complete family is not a new first serial autonomy-canary prerequisite.
Correctness is different from optimization: a concrete missing mandatory criterion
or untrustworthy required assurance on the selected path needs a bounded safety
assessment/fix. This plan neither authorizes ignoring that defect nor forces
unrelated UI, model routing or all subagent productization onto the canary path.

The publication repair, qualified EP artifact/install operation and first real
Forge-to-EP autonomy proof remain separate work. No runtime mutation, wheel release,
CENTRAL change, credential/network redesign, expired programme reuse or repair-budget
reset follows from documentary merge. Preserve full local validation plus independent
current-candidate Quality/Security before first publication, one host-owned dispatch,
three total corrective rounds per run/continuation lineage and exact-head delivery.

Every runtime node needs its own applicable source and installed qualification;
`SA-Q` is the full-family integration milestone, not a prohibition on independently
qualified bounded repairs. Keep `IMPLEMENTED`, `QUALIFIED` and installed/consumer
availability separate, with exact source/artifact/evidence references. On pickup,
record actual new receipts; do not turn forensic failure reproductions into PASS.

## Documentary acceptance

Require unique node/finding IDs; all dependencies resolvable; an acyclic graph;
matching table/JSON edges; coverage of SA-F01 through SA-F09; checked internal links;
original reproduction files retained byte-for-byte with scope limitations; and no
source, test, workflow, package, active policy, grant or executable programme changes.
Historical Python files under the evidence directory are standalone diagnostics,
not runtime implementation or CI qualification. The version classification is NO_BUMP.
