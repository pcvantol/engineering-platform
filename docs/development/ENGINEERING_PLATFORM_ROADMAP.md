# Engineering Platform Roadmap

## Current standalone/bootstrap critical path — 2026-09-06

This section is the current sequencing authority where older roadmap prose or derived cross-product projections conflict with the post-P-TRANSPORT decisions.

### Current facts

- `P-TRANSPORT` is **MERGED / CLOSED**. It provides three canonical submission transports — HTTP, installed CLI and Server-owned File Inbox — normalized through the Server-owned Submission Service/CENTRAL boundary. File Inbox is transport only, never lifecycle authority.
- The Phase-1 Local Consumer API read-only foundation and the later P-TRANSPORT HTTP submission ingress are distinct. The existence of the earlier read-only API qualification must not be interpreted as “all EP HTTP is read-only”.
- Current work is `P-NEUTRAL`: remove remaining active DJConnect platform identity/authority while preserving historical evidence and bounded migration compatibility.
- The Canonical Project Authority Repository declares durable logical project/repository identity in `.engineering-platform/repository.json` under the B8R architecture. Workspace may project identity and own human-facing state/display naming; Workspace availability is not required for EP to attach a declared repository.
- Broader Project-Agent separation, generalized Agent dispatch, multi-host scheduling and multi-repository parallel execution are **not prerequisites by default** for the first standalone verification. They are follow-on productization unless the minimum installed execution canary proves a concrete dependency.
- Broad P-QUEUE/B8E productization must not become an artificial all-or-nothing gate. Only concrete queue/lease/recovery/finalization/zero-loss capabilities required to prove one installed governed execution are on the immediate critical path.
- Real-project dogfooding is part of the qualification strategy: first prove DJConnect as the standalone canary, then prove EP can engineer its own repository through installed CENTRAL before Forge depends on EP as execution producer.

### Shortest safe path to `EP::STANDALONE_EP_VERIFIED`

```text
P-TRANSPORT merged/closed
  -> P-NEUTRAL closure
  -> DJConnect committed .engineering-platform/repository.json
  -> attach DJConnect to installed CENTRAL EP
  -> qualify minimum existing installed execution path
       canonical submission
       -> bounded admission
       -> current execution/provider path
       -> finalization
       -> immutable receipt/result evidence
       -> canonical status/result observation
  -> first real governed DJConnect engineering Action
  -> EP::STANDALONE_EP_VERIFIED
```

The first standalone canary is intentionally single-project, single-Action and serial. It does not require the final generalized Agent topology, multi-host dispatch or multi-repository concurrency.

### Real-project CENTRAL dogfooding sequence

Real repositories are used deliberately to prove that EP is an execution product independently of Forge workflow.

1. **DJConnect — mandatory standalone canary.** Add and separately govern the B8R declaration in `.engineering-platform/repository.json`, attach the repository to installed CENTRAL and execute one real low-risk Engineering Action end to end. Submission may use any qualified P-TRANSPORT ingress; HTTP is preferred when qualifying the future machine-consumer path. The run must prove admission, real repository mutation, canonical validation, finalization and immutable receipt/result/provenance evidence. This proof is part of `EP::STANDALONE_EP_VERIFIED`.
2. **Engineering Platform — mandatory self-development dogfood immediately after standalone.** Add/qualify its own declaration and execute a real bounded EP-development Action through the installed CENTRAL EP. This creates the milestone `EP::SELF_HOSTED_ENGINEERING_VERIFIED`: the installed product can maintain its own source repository through the same execution authority offered to other projects. This is required before relying on EP as Forge's normal execution producer, but is not folded back into the already-earned standalone gate.
3. **Forge — next dogfood project before or during Forge execution integration.** Add/qualify the Forge repository declaration and prove a direct EP-governed Forge development Action independently of Forge's own Mission workflow. This separates “EP can engineer Forge” from “Forge can orchestrate EP”.
4. **Workspace — additional real-project dogfood, non-blocking for first Forge autonomy.** Add/qualify the Workspace declaration and prove EP execution when useful; Workspace remains off the first Forge-autonomy critical path.

For every real-project dogfood proof, require:

```text
committed B8R declaration
  -> EP validation + attachment
  -> canonical P-TRANSPORT submission
  -> CENTRAL admission/run identity
  -> provider/execution
  -> repository mutation
  -> repository canonical validation
  -> finalization
  -> immutable receipt/result/provenance
  -> canonical status/result observation
```

Real-project dogfooding does not authorize multi-project parallel mutation. Runs may remain serial. It proves project isolation and repeatable attachment/execution semantics without requiring generalized Agent/dispatch productization.

`DJCONNECT_REAL_ACTION_REQUIRED_FOR_STANDALONE = TRUE`

`EP_SELF_HOSTED_ENGINEERING_VERIFIED_REQUIRED_BEFORE_FORGE_EXECUTION_DEPENDENCY = TRUE`

`FORGE_DIRECT_EP_DOGFOOD_BEFORE_OR_DURING_FORGE_INTEGRATION = TRUE`

`WORKSPACE_DOGFOOD_BLOCKS_FIRST_FORGE_AUTONOMY = FALSE`

### After standalone verification

```text
EP::STANDALONE_EP_VERIFIED
  -> EP::SELF_HOSTED_ENGINEERING_VERIFIED
  -> Forge repository direct-EP dogfood
  -> Forge materialization + execution admission
  -> existing canonical P-TRANSPORT HTTP submission
  -> EP canonical run/finalization/result evidence
  -> Forge result observation + reconciliation
  -> first Forge -> EP -> Forge governed execution canary
  -> autonomous next-Mission loop
```

A new Forge-specific submission transport is prohibited unless an actual contract gap proves the existing P-TRANSPORT HTTP ingress insufficient.

`P_TRANSPORT_STATUS = MERGED_CLOSED`
`P_NEUTRAL_STATUS = ACTIVE_CRITICAL_PATH`
`GENERAL_AGENT_SEPARATION_ON_STANDALONE_CRITICAL_PATH = FALSE`
`WORKSPACE_REQUIRED_FOR_FIRST_STANDALONE_CANARY = FALSE`
`FORGE_SHOULD_REUSE_P_TRANSPORT_HTTP = TRUE`

## 1.4 — Remote Engineering Experience

Completed. Canonical status, remote dashboard and repository handoffs remain operational surfaces without planning or product authority.

## 1.5 — Platform Productization

Completed and operational. Engineering Platform is an independent engineering product. Platform identity, provider/capability registries, configuration validation and public boundaries remove architectural dependence on DJConnect. Historical compatibility does not grant current authority.

## 1.6 / 2.0 — Extraction and versioned boundary

Repository extraction/versioning work established the independent product boundary. Versioning, packaging and migration evidence do not themselves change runtime authority; authority changes only through qualified installed-product gates.

## 2.x — Standalone Execution Operations Platform

Engineering Platform is the installed, provider- and consumer-neutral execution authority. The Execution Operations Console is presentation/operations only and never a second source of planning, project topology or lifecycle authority.

### Core authority split

- **Forge owns why/what:** Mission, Engineering Action intent, planning dependencies and governance.
- **EP owns how:** submission/admission, execution lifecycle, provider execution, finalization, receipts/evidence, operational recovery and canonical execution projections.
- **Workspace owns human/project UX:** presentation, comprehension and permitted intent; never execution authority.
- **Canonical Project Authority Repository owns declared logical topology input:** EP validates the committed `.engineering-platform/repository.json`; path, Git remote, display name, host, Agent or Workspace runtime identity are never substitutes.

### Datastore and identity

One installed EP instance owns its operational CENTRAL datastore. Project, repository, installation, consumer, Agent, run, producer and Forge Mission/Action identities are independent and correlated only by explicit versioned contracts. Files are transport/rendered evidence/export/fallback, not competing lifecycle authority.

### Canonical submission transports

P-TRANSPORT qualifies exactly three supported submission transports:

1. canonical HTTP submission ingress;
2. installed CLI submission ingress;
3. Server-owned File Inbox ingress.

All normalize through the same Server-owned submission/CENTRAL authority. Consumers choose a supported transport; transport selection never changes admission/lifecycle authority. Forge's machine-to-machine integration should prefer the canonical HTTP transport.

### Current P-NEUTRAL increment

P-NEUTRAL removes active DJConnect naming/identity from generic EP runtime, installation, lifecycle, configuration and logging authority. Historical-only artifacts and migration-source references may remain when explicitly classified. Current concrete work includes neutralizing the remaining installed relay identity without creating dual authority or unsafe rollback.

P-NEUTRAL closure requires zero active generic DJConnect platform identity within the qualified host/repository scope. It does **not** remove the historical EP implementation from the DJConnect source repository. DJConnect source retirement remains a separately governed post-standalone responsibility-transfer/retirement increment after installed EP authority has been proven.

## Standalone verification contract

`EP::STANDALONE_EP_VERIFIED` is satisfied by evidence that the installed EP product can independently execute one real governed DJConnect Engineering Action end to end with no legacy execution authority required.

Minimum proof:

1. installed EP Server/runtime healthy;
2. committed DJConnect B8R project/repository declaration validated and attached;
3. canonical submission accepted through a supported P-TRANSPORT ingress;
4. bounded admission/project scope enforced;
5. one mutating DJConnect execution runs through the current qualified execution/provider path;
6. repository canonical validation passes;
7. finalization completes;
8. immutable receipt/result/provenance evidence exists;
9. canonical status/result projection can be observed by a consumer;
10. failure/retry behavior for this single-run profile is fail-closed;
11. legacy DJConnect platform authority is not required.

Not required for this first gate unless the canary proves otherwise: generalized Agent separation; multi-Agent/multi-host scheduling; generalized dispatch architecture; multi-repository parallel mutation; Workspace UI/control plane; full later B8E productization unrelated to the single-run proof.

Any concrete missing capability discovered by the canary becomes a bounded prerequisite; future capability labels do not become prerequisites merely by historical roadmap association.

## Follow-on execution productization

After the standalone + self-development proofs, continue separately with broader capabilities where valuable: generalized Project-Agent separation and dispatch; multi-host/Agent availability and routing; richer queue policy/capacity/ordering; multi-repository leases/parallelism; broader recovery/retry profiles; remaining B8E parity/productization; broader installed-product Goldens.

## Forge integration after standalone/self-development proof

Forge must not become an execution engine. The intended machine boundary is:

```text
Forge immutable Action/submission intent
  -> EP canonical P-TRANSPORT HTTP submission
  -> EP admission/run/finalization
  -> EP immutable terminal evidence
  -> Forge observation/reconciliation
```

Forge records its intended Action/submission key before the HTTP call. EP persists canonical submission/run evidence. Ambiguous HTTP outcomes are reconciled by canonical identity/correlation rather than automatic duplicate submission.

Forge may consume canonical EP readiness/status/result/evidence APIs but must not derive authority from CENTRAL filesystem access, launchctl, Console HTML, logs or direct Agent control.

## Cross-product producer capabilities

| Node ID | Current meaning | Bootstrap disposition |
| --- | --- | --- |
| `EP::LOCAL_CONSUMER_API_V1` | Qualified consumer/authentication/read contract foundation. | AVAILABLE; not the only HTTP surface after P-TRANSPORT. |
| `EP::P_TRANSPORT_V1` | Three canonical submission transports with Server/CENTRAL normalization. | MERGED / QUALIFIED transport capability. |
| `EP::P_NEUTRAL_V1` | No active generic DJConnect platform identity/authority. | ACTIVE critical-path increment. |
| `EP::STANDALONE_EP_VERIFIED` | One independent installed governed DJConnect execution with canonical evidence. | NEXT major qualification target after P-NEUTRAL/minimum execution closure. |
| `EP::SELF_HOSTED_ENGINEERING_VERIFIED` | Installed EP executes a real bounded change to the Engineering Platform repository through CENTRAL. | IMMEDIATE POST-STANDALONE dogfood gate before Forge relies on EP execution. |
| `EP::PROJECT_ATTACHMENT_AND_ADMISSION_V1` | Consumer-facing attachment/admission hardening beyond current B8R runtime. | FOLLOW-ON consumer qualification; current B8R authority remains usable for dogfood. |
| `EP::ENGINEERING_CONTRACT_FOUNDATION_V1` | Rich DoR/DoD/Human-Gate/Action-quality producer contract. | Long-term Forge producer contract; first canary uses minimum proven contracts and exposes real gaps. |

## Consumer and Workspace position

Workspace is not on the critical path for first standalone verification or the first Forge -> EP -> Forge machine canary. Workspace later composes Forge planning and EP execution projections and initiates permitted intent without becoming authority. Workspace itself is a valid later CENTRAL dogfood project, but that proof is not a prerequisite for first Forge autonomy.

Older documentation that says Workspace supplies the canonical EP logical `project_id` is superseded for topology authority by B8R. Consumer registration identity and human-facing Workspace project state remain separate concepts.

## Source retirement

The old EP implementation/source remnants in DJConnect are not removed by P-NEUTRAL. After standalone authority, self-development and required responsibility-transfer evidence are established, a separately governed DJConnect source-retirement increment may remove obsolete EP runtime/source/wrappers/configuration while preserving historical/provenance evidence and justified migration material.

## Policy

Platform code must not acquire DJConnect runtime, Home Assistant, branding or repository-name dependencies. Historical evidence and explicitly bounded migration compatibility may retain names without retaining authority.

The planned home deployment of one authoritative Forge installation, one authoritative EP installation and one primary execution host on a Mac mini is a deployment profile, not a product-wide singleton invariant.
