# Engineering Platform Roadmap

## Current standalone/bootstrap critical path — 2026-09-06

This section is the current sequencing authority where older roadmap prose or derived cross-product projections conflict with the post-P-TRANSPORT decisions.

### Current facts

- `P-TRANSPORT` is **MERGED / CLOSED**. It provides three canonical submission transports — HTTP, installed CLI and Server-owned File Inbox — normalized through the Server-owned Submission Service/CENTRAL boundary. File Inbox is transport only, never lifecycle authority.
- The Phase-1 Local Consumer API read-only foundation and the later P-TRANSPORT HTTP submission ingress are distinct. The existence of the earlier read-only API qualification must not be interpreted as “all EP HTTP is read-only”.
- Current work is `P-NEUTRAL`: remove remaining active DJConnect platform identity/authority while preserving historical evidence and bounded migration compatibility.
- `P-INSTALLER-V1` is now an explicit critical-path gate immediately after P-NEUTRAL. It installs/repairs only the standalone Engineering Platform Server-side product components required by the first installed execution canary. It does **not** install Forge, Workspace or generalized Project-Agent productization.
- The Canonical Project Authority Repository declares durable logical project/repository identity in `.engineering-platform/repository.json` under the B8R architecture. Workspace may project identity and own human-facing state/display naming; Workspace availability is not required for EP to attach a declared repository.
- Broader Project-Agent separation, generalized Agent dispatch, multi-host scheduling and multi-repository parallel execution are **not prerequisites by default** for the first standalone verification. They are follow-on productization unless the minimum installed execution canary proves a concrete dependency.
- Broad P-QUEUE/B8E productization must not become an artificial all-or-nothing gate. Only concrete queue/lease/recovery/finalization/zero-loss capabilities required to prove one installed governed execution are on the immediate critical path.
- Real-project dogfooding is part of the qualification strategy: first prove DJConnect as the standalone canary, then prove EP can engineer its own repository through installed CENTRAL before Forge depends on EP as execution producer.

## Critical path from now

```text
P-TRANSPORT merged/closed
  -> P-NEUTRAL closure
  -> P-INSTALLER-V1
       server-side EP product only
       clean install/repair/update
       canonical runtime + CENTRAL + HTTP/CLI/Inbox + Console/relay as applicable
       no Forge
       no Workspace
       no generalized Agent productization
  -> DJConnect committed .engineering-platform/repository.json
  -> attach DJConnect to installed CENTRAL EP
  -> first real governed DJConnect engineering Action
       canonical submission
       -> bounded admission
       -> current execution/provider path
       -> repository mutation + validation
       -> finalization
       -> immutable receipt/result/provenance
       -> canonical status/result observation
  -> EP::STANDALONE_EP_VERIFIED
  -> Engineering Platform committed repository declaration + CENTRAL attachment
  -> first real bounded EP self-development Action
  -> EP::SELF_HOSTED_ENGINEERING_VERIFIED
  -> Forge repository direct-EP dogfood
  -> Forge materialization/admission + P-TRANSPORT HTTP integration
  -> first Forge -> EP -> Forge canary
  -> autonomous next-Mission loop
```

The first standalone canary is intentionally single-project, single-Action and serial. It does not require the final generalized Agent topology, multi-host dispatch or multi-repository concurrency.

## P-INSTALLER-V1 — standalone Server product installer

`P-INSTALLER-V1` exists to make the installed EP Server product reproducible before real-project execution evidence is trusted.

In scope:

- install/upgrade/repair of the canonical Engineering Platform Server-side package/runtime;
- canonical CENTRAL database creation/open/migration;
- Server-owned HTTP API including the P-TRANSPORT HTTP submission ingress;
- installed CLI and Server-owned File Inbox components owned by the Server product;
- canonical lifecycle services required by the current Server runtime;
- Console/relay components that are part of the installed EP Server product;
- canonical configuration, non-secret installation identity and bounded health/readiness checks;
- uninstall/repair semantics for those EP Server-owned components;
- clean-install and reinstall/repair qualification.

Explicitly out of scope:

- Forge installation;
- Workspace installation;
- generalized Project-Agent installation/productization;
- multi-host Agent fleet management;
- generalized dispatch redesign;
- project repository declarations themselves;
- source retirement in DJConnect.

The first standalone canary may continue to use the current qualified execution/provider path even if future Agent separation remains incomplete. P-INSTALLER-V1 must not smuggle later Agent productization onto the critical path.

Required milestone:

`EP::P_INSTALLER_V1_QUALIFIED`

Minimum evidence:

```text
clean host/install scope
  -> install canonical Server-side EP product
  -> canonical installation identity
  -> CENTRAL open/migrate PASS
  -> Server lifecycle PASS
  -> HTTP/CLI/File-Inbox transport availability PASS
  -> installed health/readiness PASS
  -> repair/reinstall idempotency PASS
  -> no DJConnect active platform authority
  -> no Forge/Workspace/Agent-productization installed as part of this bundle
```

`P_INSTALLER_V1_SERVER_ONLY = TRUE`
`P_INSTALLER_V1_INSTALLS_FORGE = FALSE`
`P_INSTALLER_V1_INSTALLS_WORKSPACE = FALSE`
`P_INSTALLER_V1_GENERAL_AGENT_PRODUCTIZATION = FALSE`

## Bootstrap governance compression — steps 1 through 6

The bootstrap through `EP::SELF_HOSTED_ENGINEERING_VERIFIED` must use the **minimum number of manual governance interruptions** consistent with authority safety.

Numbered sequence:

1. P-NEUTRAL closure.
2. P-INSTALLER-V1 qualification and installed Server cutover/repair.
3. DJConnect declaration + CENTRAL attachment.
4. First real DJConnect Engineering Action.
5. `EP::STANDALONE_EP_VERIFIED`.
6. First real Engineering Platform self-development Action -> `EP::SELF_HOSTED_ENGINEERING_VERIFIED`.

Governance policy:

- One bounded bootstrap authority envelope may cover repository implementation, installer implementation, declaration creation, attachment, read-only inspection, deterministic validation, defect repair, exact-head requalification, hosted checks and repeated Human Security review without returning to the owner between each engineering sub-step.
- P-NEUTRAL implementation and P-INSTALLER-V1 implementation/qualification are engineering loops: implement -> validate -> diagnose -> bounded repair -> revalidate -> security re-review until merge/cutover-ready.
- Repository declaration creation and CENTRAL attachment are topology realization inside the approved B8R boundary; they are not separate owner gates when project/repository identity, install scope and allowed host are already pinned.
- Tests, CI failures, installer defects, migration defects and Security-review findings that remain within the approved phase boundary are not owner gates.
- `EP::STANDALONE_EP_VERIFIED` and `EP::SELF_HOSTED_ENGINEERING_VERIFIED` are evidence milestones, not manual approval stops by themselves.
- A successful DJConnect canary may flow directly into EP self-development dogfood under the same pre-approved bootstrap envelope if the second Action's repository, write scope and risk class are already pinned.

Manual owner intervention is reserved for genuine authority expansion, specifically:

1. the first activation of a new mutating execution authority not already covered by the bootstrap envelope;
2. broader write scope, destructive operation, deployment or secret-bearing capability;
3. an architecture/security finding that cannot be resolved without weakening an approved invariant;
4. destructive source retirement or later generalized Agent/dispatch authority.

The preferred operational model is therefore:

```text
OWNER APPROVES ONE BOUNDED BOOTSTRAP ENVELOPE
        |
        v
P-NEUTRAL engineering loop
        |
        v
P-INSTALLER-V1 engineering + installed qualification loop
        |
        v
DJConnect declaration/attach
        |
        v
DJConnect real Action
        |
        v
STANDALONE_EP_VERIFIED
        |
        v
EP declaration/attach
        |
        v
EP self-development real Action
        |
        v
SELF_HOSTED_ENGINEERING_VERIFIED
        |
        v
RETURN TO OWNER ONLY IF NEXT AUTHORITY EXPANSION REQUIRES IT
```

Where repository/branch policy itself requires an owner merge, consolidate implementation into the minimum practical number of merge decisions and do not create extra governance gates for qualification-only iterations.

`BOOTSTRAP_MICRO_APPROVALS_PROHIBITED = TRUE`
`ENGINEERING_REPAIR_WITHIN_APPROVED_BOUNDARY_AUTONOMOUS = TRUE`
`SECURITY_REVIEW_IS_QUALIFICATION_NOT_OWNER_GATE = TRUE`
`EVIDENCE_MILESTONE_IS_NOT_OWNER_GATE = TRUE`

## Real-project CENTRAL dogfooding sequence

Real repositories are used deliberately to prove that EP is an execution product independently of Forge workflow.

1. **DJConnect — mandatory standalone canary.** After P-INSTALLER-V1, add/qualify the B8R declaration in `.engineering-platform/repository.json`, attach the repository to installed CENTRAL and execute one real low-risk Engineering Action end to end. Submission may use any qualified P-TRANSPORT ingress; HTTP is preferred when qualifying the future machine-consumer path. The run must prove admission, real repository mutation, canonical validation, finalization and immutable receipt/result/provenance evidence. This proof earns `EP::STANDALONE_EP_VERIFIED`.
2. **Engineering Platform — mandatory self-development dogfood immediately after standalone.** Add/qualify its own declaration and execute a real bounded EP-development Action through installed CENTRAL EP. This earns `EP::SELF_HOSTED_ENGINEERING_VERIFIED` and proves EP can maintain its own source repository through the same execution authority offered to other projects.
3. **Forge — next dogfood project before or during Forge execution integration.** Add/qualify the Forge repository declaration and prove a direct EP-governed Forge development Action independently of Forge's own Mission workflow.
4. **Workspace — additional real-project dogfood, non-blocking for first Forge autonomy.** Add/qualify the Workspace declaration and prove EP execution when useful.

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

Real-project dogfooding does not authorize multi-project parallel mutation. Runs may remain serial.

`DJCONNECT_REAL_ACTION_REQUIRED_FOR_STANDALONE = TRUE`
`EP_SELF_HOSTED_ENGINEERING_VERIFIED_REQUIRED_BEFORE_FORGE_EXECUTION_DEPENDENCY = TRUE`
`FORGE_DIRECT_EP_DOGFOOD_BEFORE_OR_DURING_FORGE_INTEGRATION = TRUE`
`WORKSPACE_DOGFOOD_BLOCKS_FIRST_FORGE_AUTONOMY = FALSE`

## Product authority split

- **Forge owns why/what:** Mission, Engineering Action intent, planning dependencies and governance.
- **EP owns how:** submission/admission, execution lifecycle, provider execution, finalization, receipts/evidence, operational recovery and canonical execution projections.
- **Workspace owns human/project UX:** presentation, comprehension and permitted intent; never execution authority.
- **Canonical Project Authority Repository owns declared logical topology input:** EP validates the committed `.engineering-platform/repository.json`; path, Git remote, display name, host, Agent or Workspace runtime identity are never substitutes.

One installed EP instance owns its operational CENTRAL datastore. Project, repository, installation, consumer, Agent, run, producer and Forge Mission/Action identities are independent and correlated only by explicit versioned contracts.

## Canonical submission transports

P-TRANSPORT qualifies exactly three supported submission transports:

1. canonical HTTP submission ingress;
2. installed CLI submission ingress;
3. Server-owned File Inbox ingress.

All normalize through the same Server-owned Submission Service/CENTRAL authority. Forge's machine-to-machine integration should prefer canonical HTTP.

## Current P-NEUTRAL increment

P-NEUTRAL removes active DJConnect naming/identity from generic EP runtime, installation, lifecycle, configuration and logging authority. Historical-only artifacts and migration-source references may remain when explicitly classified. Current concrete work includes neutralizing the remaining installed relay identity without creating dual authority or unsafe rollback.

P-NEUTRAL closure requires zero active generic DJConnect platform identity within the qualified host/repository scope. It does **not** remove the historical EP implementation from the DJConnect source repository. DJConnect source retirement remains separately governed post-standalone work.

## Standalone verification contract

`EP::STANDALONE_EP_VERIFIED` is satisfied by evidence that the P-INSTALLER-V1-installed EP product can independently execute one real governed DJConnect Engineering Action end to end with no legacy execution authority required.

Minimum proof:

1. `EP::P_INSTALLER_V1_QUALIFIED` and installed Server/runtime healthy;
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

Not required unless the canary proves otherwise: generalized Agent separation; multi-Agent/multi-host scheduling; generalized dispatch architecture; multi-repository parallel mutation; Workspace UI/control plane; full later B8E productization.

## Follow-on execution productization

After standalone + self-development proofs, continue separately with generalized Project-Agent separation/dispatch, multi-host routing, richer queue policy/capacity/ordering, multi-repository leases/parallelism, broader recovery/retry and remaining B8E parity/productization.

## Forge integration after standalone/self-development proof

```text
Forge immutable Action/submission intent
  -> EP canonical P-TRANSPORT HTTP submission
  -> EP admission/run/finalization
  -> EP immutable terminal evidence
  -> Forge observation/reconciliation
```

Forge records intended Action/submission identity before the HTTP call. EP persists canonical submission/run evidence. Ambiguous HTTP outcomes are reconciled by canonical identity/correlation rather than automatic duplicate submission.

Forge may consume canonical EP readiness/status/result/evidence APIs but must not derive authority from CENTRAL filesystem access, launchctl, Console HTML, logs or direct Agent control.

## Cross-product producer capabilities

| Node ID | Current meaning | Bootstrap disposition |
| --- | --- | --- |
| `EP::LOCAL_CONSUMER_API_V1` | Qualified consumer/authentication/read contract foundation. | AVAILABLE. |
| `EP::P_TRANSPORT_V1` | Three canonical submission transports with Server/CENTRAL normalization. | MERGED / QUALIFIED. |
| `EP::P_NEUTRAL_V1` | No active generic DJConnect platform identity/authority. | ACTIVE. |
| `EP::P_INSTALLER_V1` | Reproducible standalone Server-side EP install/repair/update boundary; excludes Forge/Workspace/general Agent productization. | CRITICAL PATH immediately after P-NEUTRAL. |
| `EP::STANDALONE_EP_VERIFIED` | One independent installed governed DJConnect execution with canonical evidence. | AFTER P-INSTALLER-V1 + DJConnect canary. |
| `EP::SELF_HOSTED_ENGINEERING_VERIFIED` | Installed EP executes a real bounded Engineering Platform source change through CENTRAL. | IMMEDIATE POST-STANDALONE dogfood gate. |
| `EP::PROJECT_ATTACHMENT_AND_ADMISSION_V1` | Consumer-facing attachment/admission hardening beyond current B8R runtime. | FOLLOW-ON consumer qualification. |
| `EP::ENGINEERING_CONTRACT_FOUNDATION_V1` | Rich DoR/DoD/Human-Gate/Action-quality producer contract. | Long-term Forge producer contract. |

## Consumer and Workspace position

Workspace is not on the critical path for first standalone verification or first Forge -> EP -> Forge canary. Workspace itself is a valid later CENTRAL dogfood project, but that proof is not a prerequisite for first Forge autonomy.

## Source retirement

The old EP implementation/source remnants in DJConnect are not removed by P-NEUTRAL or P-INSTALLER-V1. After standalone authority, self-development and required responsibility-transfer evidence are established, a separately governed DJConnect source-retirement increment may remove obsolete EP runtime/source/wrappers/configuration while preserving historical/provenance evidence and justified migration material.

## 1.5 — Platform Productization

Completed and operational.

The completed 1.5 productization boundary remains historical maturity evidence. Current P-NEUTRAL/P-INSTALLER/bootstrap work is subsequent maintenance and standalone qualification, not a reopening of 1.5.

## Policy

Platform code must not acquire DJConnect runtime, Home Assistant, branding or repository-name dependencies. Historical evidence and explicitly bounded migration compatibility may retain names without retaining authority.

The planned home deployment of one authoritative Forge installation, one authoritative EP installation and one primary execution host on a Mac mini is a deployment profile, not a product-wide singleton invariant.
