# Parallel Action execution V1 — EP owning roadmap

**Owner:** Engineering Platform. **All added implementation/qualification nodes: PLANNED. NO_BUMP.**
This is the bounded decomposition of the existing dependency-admission and P-QUEUE/multi-execution seams in [the canonical roadmap](ENGINEERING_PLATFORM_ROADMAP.md). It does not implement a second scheduler or reopen the entire Agent fleet, subagent-efficiency or post-assurance-publication programme.

Owning contract: [Forge Action dependency and EP admission boundary](../engineering/FORGE_ACTION_DEPENDENCY_ADMISSION_BOUNDARY.md). Machine-readable index: [documentary DAG](PARALLEL_ACTION_EXECUTION_V1_DAG.json). Forge companion: `docs/roadmap/PARALLEL_ACTION_RUNTIME_V1.md`; shared tests: `docs/architecture/PARALLEL_ACTION_QUALIFICATION_V1.md`, PA-01..PA-26.

| Node | Dependencies | Bounded deliverable |
| --- | --- | --- |
| PA-E0 | Forge PA-F0 contract fixtures | Versioned per-Action target/dependency/evidence/concurrency contract and compatibility resolution |
| PA-E1 | PA-E0 | Independent immutable submission/admission and exact predecessor predicates; typed wait/scope states |
| PA-E2 | PA-E1 | Real concurrent worker/provider delivery across independent repository resources, bounded slots and fairness |
| PA-E3 | PA-E2 | Per-invocation/workspace isolation, fenced leases, partial-submit/restart/cancel safety and repair-budget preservation |
| PA-E4 | PA-E1 | Independent terminal readback, multi-active collection, canonical timing/usage and MD/JSON evidence parity |
| PA-EQ | PA-E2, PA-E3, PA-E4 | Installed one-host/two-target concurrency and negative-case qualification with actual execution overlap |

EP PA-EQ consumes Forge PA-F0 fixtures, not Forge's final PA-FQ. Forge PA-F3 consumes PA-E1/PA-E2/PA-E3; Forge final qualification consumes PA-EQ. This is acyclic. EP PA-E3 may reuse already qualified SA-ISO or equivalent invocation-isolation capabilities, but cannot assume the whole historic SA finding set is still open or require all SA-Q work. Active telemetry/reporting fixes stay closed unless a concrete regression is found.

First delivery uses two authorized repositories and isolated workspaces on one compatible host. Existing admission/worker/lease/provider paths must actually run concurrently; capability labels or a threadpool alone do not qualify it. No full Workspace UI, distributed Agent fleet, new universal installer or nested provider-agent engine prerequisite. Same-repository parallel writes remain a separate stricter profile.

The integrated consumer canary demonstrates Forge-derived fan-out, A-result-driven continuation while B runs, a true A+B artifact join and correct Mission completion. Tests compare serial and parallel behavior at the same quality/authority gates and report honest elapsed/usage evidence, not a promised speedup. It is separate from the current serial Mission-3 proof and data-reset implementation. Documentation does not allocate new Mission/Action IDs or activate policy, gates, runtimes or package versions.
