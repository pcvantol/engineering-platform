# Engineering Platform Roadmap

## Current standalone/bootstrap critical path — 2026-09-06

This section is the current sequencing authority where older roadmap prose or derived cross-product projections conflict with the post-P-TRANSPORT decisions.

### Current facts

- `P-TRANSPORT` is **MERGED / CLOSED**. It provides three canonical submission transports — HTTP, installed CLI and Server-owned File Inbox — normalized through the Server-owned Submission Service/CENTRAL boundary. File Inbox is transport only, never lifecycle authority.
- The Phase-1 Local Consumer API read-only foundation and the later P-TRANSPORT HTTP submission ingress are distinct. The existence of the earlier read-only API qualification must not be interpreted as “all EP HTTP is read-only”.
- Current work is `P-NEUTRAL`: remove remaining active DJConnect platform identity/authority while preserving historical evidence and bounded migration compatibility.
- The Canonical Project Authority Repository declares durable logical project/repository identity in `.engineering-platform/repository.json` under the B8R architecture. Workspace may project identity and own human-facing state/display naming; Workspace availability is not required for EP to attach a declared repository.
- Broader Project-Agent separation, generalized Agent dispatch, multi-host scheduling and multi-repository parallel execution are **not prerequisites by default** for the first standalone verification. They are follow-on productization unless the minimum installed execution canary proves a concrete dependency.
- Likewise, broad P-QUEUE/B8E productization must not become an artificial all-or-nothing gate. Only concrete queue/lease/recovery/finalization/zero-loss capabilities required to prove one installed governed execution are on the immediate critical path; unresolved broader capabilities remain explicit follow-on work.

### Shortest safe path to `EP::STANDALONE_EP_VERIFIED`

```text
P-TRANSPORT merged/closed
  -> P-NEUTRAL closure
  -> qualify minimum existing installed execution path
       canonical submission
       -> bounded admission
       -> current execution/provider path
       -> finalization
       -> immutable receipt/result evidence
       -> canonical status/result observation
  -> first governed installed execution
  -> EP::STANDALONE_EP_VERIFIED
```

The first standalone canary is intentionally single-project, single-Action and serial. It does not require the final generalized Agent topology, multi-host dispatch or multi-repository concurrency.

After `EP::STANDALONE_EP_VERIFIED`, Forge should consume the existing canonical HTTP submission transport and canonical EP run/result evidence to qualify the first Forge → EP → Forge loop. A new Forge-specific submission transport is prohibited unless an actual contract gap proves the existing P-TRANSPORT HTTP ingress insufficient.

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

P-NEUTRAL closure requires zero active generic DJConnect platform identity within the qualified host/repository scope. It does not authorize broad historical deletion.

## Standalone verification contract

`EP::STANDALONE_EP_VERIFIED` is satisfied by evidence that the installed EP product can independently execute one real governed engineering Action end to end with no legacy execution authority required.

Minimum proof:

1. installed EP Server/runtime healthy;
2. canonical project/repository identity validated and attached through current EP authority;
3. canonical submission accepted through a supported P-TRANSPORT ingress;
4. bounded admission/project scope enforced;
5. one mutating execution runs through the current qualified execution/provider path;
6. finalization completes;
7. immutable receipt/result/provenance evidence exists;
8. canonical status/result projection can be observed by a consumer;
9. failure/retry behavior for this single-run profile is fail-closed;
10. legacy DJConnect platform authority is not required.

Not required for this first gate unless the canary proves otherwise:

- generalized Agent separation;
- multi-Agent or multi-host scheduling;
- generalized dispatch architecture;
- multi-repository parallel mutation;
- Workspace UI/control plane;
- full later B8E capability productization unrelated to the single-run proof.

Any concrete missing capability discovered by the canary becomes a bounded prerequisite; future capability labels do not become prerequisites merely by historical roadmap association.

## Follow-on execution productization

After standalone verification, continue separately with broader capabilities where valuable:

- generalized Project-Agent separation and dispatch;
- multi-host/Agent availability and routing;
- richer queue policy, capacity and ordering;
- multi-repository leases/parallelism;
- broader recovery/retry profiles;
- B8E zero-loss/product parity closure not required by the first canary;
- installed-product Goldens and broader dogfooding.

These remain EP-owned and must preserve the same authority split.

## Forge integration after standalone verification

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
| `EP::STANDALONE_EP_VERIFIED` | One independent installed governed execution with canonical evidence. | NEXT major qualification target after P-NEUTRAL/minimum execution closure. |
| `EP::PROJECT_ATTACHMENT_AND_ADMISSION_V1` | Consumer-facing attachment/admission hardening beyond the current B8R runtime. | FOLLOW-ON/consumer qualification; do not reinterpret as absence of current EP attachment authority. |
| `EP::ENGINEERING_CONTRACT_FOUNDATION_V1` | Rich consumer contract for DoR/DoD/Human Gates/Action projections and quality outcomes. | Long-term Forge producer contract; the first bootstrap canary should use minimum existing contracts and expose only real gaps. |

## Consumer and Workspace position

Workspace is not on the critical path for first standalone verification or the first Forge → EP → Forge machine canary. Workspace later composes Forge planning and EP execution projections and initiates permitted intent without becoming authority.

Older documentation that says Workspace supplies the canonical EP logical `project_id` is superseded for topology authority by B8R. Consumer registration identity and human-facing Workspace project state remain separate concepts.

## Policy

Platform code must not acquire DJConnect runtime, Home Assistant, branding or repository-name dependencies. Historical evidence and explicitly bounded migration compatibility may retain names without retaining authority.

The planned home deployment of one authoritative Forge installation, one authoritative EP installation and one primary execution host on a Mac mini is a deployment profile, not a product-wide singleton invariant.
