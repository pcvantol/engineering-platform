# Engineering Platform Roadmap

## Release and operational installation lifecycle V1 — active coordinated plan

Repository evidence recorded on 2026-09-09 reads `origin/main` as
`0a98d0ca2395bd3b6b50deffd3139206b75c16e5` at this roadmap update. PyPI
holds immutable 2.3.1 wheel and sdist bytes; protected-main reconciliation
moved the canonical source projections forward to 2.3.2 without rewriting that
published identity. The observed Mac has one 2.3.1 server process/data root and
a distinct 2.3.0 PlatformIO PATH candidate. This is explicit-path investigation
evidence, not proof of Mac-wide uniqueness and not authorization to remove or
cut over either installation. CENTRAL schema 56, engineering-storage schema 41
and repository-attachment schema 1.0 remain separate contracts.

| Increment | Owning repository | Bounded result | Dependencies / acceptance |
| --- | --- | --- | --- |
| RL-1 | engineering-platform | Durable EP release-operation record, product-wide exclusive operation lock, exact wheel/sdist identities and separate `PUBLISHED`/`RELEASE_COMPLETE` states | SOURCE_FIXED through #153 `6919898`; the current parity increment also hardens owner-locked transitions, immutable policy identity, JSON recovery validation and `CLEANUP_PENDING` resume. No new release operation has been executed. |
| RL-2 | engineering-platform | Protected-main release workflow, exact-main qualification, registry readback, immutable `PUBLISHED` evidence, separate `RELEASE_COMPLETE` closure, receipt and scoped cleanup | The current parity increment retains `QUALIFIED` in a draft GitHub Release before PyPI, rejects unproven existing publications, reads back both exact distributions and completes cleanup before terminalization. 2.3.2 remains source-prepared only; no PyPI publication or release closure is claimed. |
| RL-3 | engineering-platform | Durable first-failure `CLEANUP_PENDING` evidence and controlled retry of release cleanup | SOURCE_FIXED: #165 `0a98d0c`. It canonicalizes and atomically hydrates a matching remote PENDING receipt, retains it on repeated cleanup failure, and detects dangling symlinks or post-delete residuals. No release operation has been dispatched. |
| OI-1 | engineering-platform | Read-only operational-installation resolver/diagnostic | SOURCE_FIXED: #111, #114 `9ef29bb`, #116 `71779d5`, #118 `f29006c`, #131 `28293b0`, #132 `d7efd67`, #133 `98e70e9`, #135 `99cbd4f`, #157 `f28fc84`, #161 `3161a4e`. The selected venv launcher and the actual server response remain separate from PATH/source observations. Explicit-path inventory only; no installation is verified. |
| OI-2 | engineering-platform | One EP-owned install/update/repair record and crash-resumable lifecycle | PARTIAL_SOURCE_FIXED: #113 `38b222a`, #121 `1bfe729`, #122 `a6f6c10`, #123 `2c4b081`, #127 `b77a638`, #128 `73c9729`, #129 `aadb3a5`, #130 `5073ee1`, #137 `a517ce3`, #144 `2848c52`, #158 `62eb6c4`. The executor serializes, journals and resumes explicit inventory/quiesce/backup/migrate/activate/verify actions; it atomically replaces only the exact registered record and performs operation-scoped cleanup. A product-specific runtime/service/migration adapter, a real update, and operational cleanup have not run. |
| OI-3 | engineering-platform | Product-owned consumer readback and exact-candidate assessment | SOURCE_FIXED: #162 `25ffe34`, qualified by #163 `495e196`. `operational-readback` resolves only the EP service interpreter, verifies live response identity and retains bounded explicit inventory evidence; `operational-update-assess` requires that `ACTIVE` readback and exact wheel/version/digest/source-revision preconditions. It does not execute, publish, cut over, migrate or clean an operational installation. |
| OI-4a | engineering-platform | EP-owned exact-wheel staging and non-operational candidate preparation | SOURCE_FIXED: #164. An existing exact update plan stages and re-hashes its wheel atomically beneath the operation root, then creates or resumes only an operation-scoped candidate venv and pip cache. It proves the candidate interpreter/package identity and fails closed on changed bytes, unsafe paths, markers, identities and locks. It does not alter services, data, migration, records, CLI selection, publication or a live installation. |
| OI-4b | engineering-platform | Durable binding and reboot recovery of the exact staged candidate | CANDIDATE_SOURCE_FIX: `codex/ep-prepared-operation-binding-v1`. The schema-3 journal binds operation, staged wheel, candidate venv, launcher, cache, package identity and a canonical current-record provenance snapshot; it refuses a source-wheel plan after binding and validates real-venv launcher symlinks, source disappearance, journal/marker/stage tampering and candidate escape. It still does not admit the candidate to execution. |
| OI-4c | engineering-platform | Locked pre-cleanup admission of the durable staged candidate | SOURCE_FIXED on `codex/ep-oi4c-execution-admission-v1`. The EP-owned admission re-verifies only the OI-4b staged candidate and its package/launcher identity under the existing installation lock, then durably binds the complete current registered-installation provenance. It refuses changed records, candidate/service selection, paths and journal evidence, and never re-reads a caller source wheel. It performs no activation, service, migration, cutover or cleanup operation. |
| FP-1 | forge-platform | Reconcile universal composition with EP-owned installation provision and consume the shared release-evidence semantics | SOURCE_FIXED: #21 `6395be9`, #22 `e666664`, #23 `ed91e81`, #27 `f77e5ea`, #28 `dd4d336`, #29 `8aa1139`, #30 `124b8fac`, #31 `6377980`, #32 `54fb36f`, #33 `ed227a3`, #34 `d8fbe36`. Composition verifies producer artifact bytes, retains resilient coordination evidence and does not create a second EP engine. Product adapters and live product operations remain product-owned. |
| F-1/W-1/AC-1 | forge, workspace, ai-development-contracts | Respectively consume release evidence, retain own version closure, and retain generic workflow/evidence rules | SOURCE_FIXED in Forge #62 `a84cf637` / #63 `8b8dd1af` / #65 `72e8dbb` / #67 `ce8accc` / #68 `847b552`, Workspace #18 `28ca0bc` / #19 `8291ef1` / #22 `b8a16a4` / #24 `4e268224` / #25 `bad3dd7`, and AI-development-contracts #10 `6ec3b443`. AI-development-contracts remains generic and owns no product runtime authority. |

`SOURCE_FIXED` is established only for the rows stated above. No new product
release is `RELEASE_COMPLETE`; no installation is `INSTALLATION_VERIFIED` or
`SINGLE_OPERATIONAL_INSTALLATION_VERIFIED`; and no cleanup is
`CLEANUP_COMPLETE`. A live publication, installation, cutover, service
mutation or artifact deletion remains a separate authorization.

### Current bounded order

1. Merge and hosted-qualify EP RL-3, then retain the durable release-cleanup
   repair as source evidence only.
2. Merge EP OI-4b and OI-4c, then use OI-4c to admit only its bound staged candidate to
   pre-cleanup execution under the existing EP lock. OI-4c must bind current
   record provenance as well as version/digest and must not re-read a staged
   wheel during an already-verified cleanup-only recovery. A later activation
   must explicitly retain one selected operational runtime or remove the
   unused candidate; an operation-root candidate is not a second permanent
   installation by default.
3. Forge Platform consumes the EP-owned installation provision only after
   that EP execution admission exists; it must not grow a second EP
   migration, installation or runtime-selection engine.

The related Forge Platform, Forge and Workspace workflow repairs already
share the main-first, immutable-artifact, durable-PUBLISHED and resumable
cleanup model in their own release authorities. Their artifacts, versions and
receipts remain product-specific. A production publication or an actual Mac
installation/update remains outside this source order until separately
authorized.

## Subagent orchestration and efficiency — retained audit and planned lane

`EP_SUBAGENT_ORCHESTRATION_AND_EFFICIENCY_V1` records the
[source findings and target design](../engineering/SUBAGENT_ORCHESTRATION_AND_EFFICIENCY.md),
[scoped roadmap](SUBAGENT_ORCHESTRATION_V1_ROADMAP.md) and
[documentary dependency DAG](SUBAGENT_ORCHESTRATION_V1_DAG.json).
The source audit at `62eb6c4631cc23b9e4d2a53043216be6f20bfaae` was reconciled
against `0c282bacc40731221da267bcff289e0687ca945c`; isolated diagnostics are
retained without promoting them to production or installed qualification.

Nine OPEN findings cover mandatory-context loss, advisory-result consumption,
selection/fan-out, shared telemetry, mandatory-review accounting, unnecessary
provider turns, utility semantics, event deduplication and role specialization.
Start with `SA-CTX`/`SA-ISO`, then `SA-OBS`; validation, selection, findings-consumer
and role lanes follow their exact DAG edges. All runtime work is PLANNED.
`SA-PUB` additionally requires qualified evidence from the separately owned
post-assurance publication contract; this lane does not close that repair.

The full family is not a new first-canary gate. A concrete context/assurance defect
on the selected canary path still requires a scoped safety assessment/fix.
Reuse existing policy, provider, CENTRAL, lease and review/repair contracts.
This NO_BUMP documentation changes no source behavior, runtime, release, grant,
budget, active policy, Mission or executable programme DAG.

## Repository observation and safe cleanup — documented target

The coordinated `PROJECT_HYGIENE_AND_REPOSITORY_RECONCILIATION_V1` increment
adds [EP-owned observation and safe cleanup](../engineering/REPOSITORY_HYGIENE_AND_SAFE_CLEANUP.md)
and the [HY-E/HY-C/HY-Q roadmap](PROJECT_HYGIENE_V1_ROADMAP.md).
EP owns fresh host/provider facts and actual scoped cleanup; Forge owns
project-wide cases and reasoning; Workspace presents requests/decisions.
Own-run finalization cleanup does not require a Forge Mission or online UI.

`HY-0 -> HY-E -> HY-C -> HY-Q` is the local documentary lane; HY-Q also consumes
Forge HY-F/HY-S. All implementation/qualification remains PLANNED. Reuse existing
admission, providers, leases and finalization rather than add a second queue or
arbitrary-shell execution mode. Mutation requires current actor/scope, exact
expected state, no active owner, retention and conditional provider safety.
Semantic supersession, branch age and a merged PR alone are not delete authority.

Cleanup warnings preserve proven delivery; protected/unknown-owned work and
ignored/untracked runtime state remain retained. The full hygiene family is not
a new first-canary or global release gate. No package, schema, workflow, grant,
budget, active programme or installed-state change is made by this document set.

## Policy governance and effective assurance profiles — documented target

The coordinated `POLICY_GOVERNANCE_AND_EFFECTIVE_PROFILES_V1` increment adds
[EP-owned policy/assurance architecture](../engineering/POLICY_GOVERNANCE_AND_ASSURANCE_PROFILES.md)
and its [scoped roadmap/DAG](POLICY_GOVERNANCE_V1_ROADMAP.md).
This is documentation, not implementation or live policy activation.

EP owns effective admission, review/quality/security, validation, provider and
repair policies; Workspace presents permitted policy management, Forge plans
bounded work/releases, and Forge Platform composes compatible artifacts. A
policy is not a grant, consumed budget is not an editable preference, and the
current three-round bound cannot reset on a new SHA/PR/restart. Standalone EP
retains approved local policy without requiring Forge or Workspace availability.

The local lane is `POL-0 -> POL-E -> POL-B -> POL-Q`, with explicit release
execution `POL-E -> VR-X -> VR-Q`. The latter joins Forge-owned release planning
before production installer composition; it does not make a matching version
string proof of a published artifact. All implementation nodes are PLANNED.
Existing #100 assurance and #102 dependency admission remain separate owning
work; this architecture does not approve or claim their implementation complete.
Full policy-administration UI is not a new first-canary gate, and no executable
programme DAG, installation, package version or grant changes in this increment.

## Server deployment and discovery sequencing

EP's target server is headless, launchd-managed on macOS, with one EP-owned
CENTRAL runtime root outside source/Git and versioned HTTP application ingress.
It retains EP-only execution authority. Stable instance identity, configured
and authenticated Forge peer binding, and restart-safe CENTRAL/evidence are
minimum seams for the first Forge→EP→Forge canary. LAN DNS-SD/mDNS discovery,
configured/unicast/tailnet bootstrap, general server-peer pairing UX and
installer composition are later productization work and do not block that
canary. Discovery is never authorization and may not silently move a binding.

See [EP Server deployment and discovery](../engineering/EP_SERVER_DEPLOYMENT_AND_DISCOVERY.md).

## Current standalone/bootstrap critical path — 2026-09-06

This section is the current sequencing authority where older roadmap prose or derived cross-product projections conflict with the post-P-TRANSPORT decisions.

### Current facts

- `P-TRANSPORT` is **MERGED / CLOSED**. It provides three canonical submission transports — HTTP, installed CLI and Server-owned File Inbox — normalized through the Server-owned Submission Service/CENTRAL boundary. File Inbox is transport only, never lifecycle authority.
- The Phase-1 Local Consumer API read-only foundation and the later P-TRANSPORT HTTP submission ingress are distinct. The existence of the earlier read-only API qualification must not be interpreted as “all EP HTTP is read-only”.
- `P-NEUTRAL` is **MERGED / CLOSED**. Commit `b44af0914622dd57c5c5c2266ee2caf9b31d9007` supplies the final authority-closure register and guarded evidence that active DJConnect platform identity/authority is absent while historical evidence remains classified and retained.
- `P-INSTALLER-V1` is the **CURRENT AUTONOMY FRONTIER**. It installs/repairs only the standalone Engineering Platform Server-side product components required by the first installed execution canary. It does **not** install Forge, Workspace or generalized Project-Agent productization.
- The Canonical Project Authority Repository declares durable logical project/repository identity in `.engineering-platform/repository.json` under the B8R architecture. Workspace may project identity and own human-facing state/display naming; Workspace availability is not required for EP to attach a declared repository.
- Broader Project-Agent separation, generalized Agent dispatch, multi-host scheduling and multi-repository parallel execution are **not prerequisites by default** for the first standalone verification. They are follow-on productization unless the minimum installed execution canary proves a concrete dependency.
- Broad P-QUEUE/B8E productization must not become an artificial all-or-nothing gate. Only concrete queue/lease/recovery/finalization/zero-loss capabilities required to prove one installed governed execution are on the immediate critical path.
- Real-project dogfooding is part of the qualification strategy: first prove DJConnect as the standalone canary, then prove EP can engineer its own repository through installed CENTRAL before Forge depends on EP as execution producer.

## Critical path from now

```text
P-TRANSPORT merged/closed
  -> P-NEUTRAL merged/closed
  -> P-INSTALLER-V1 current frontier
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

1. P-NEUTRAL closure (complete; preserved predecessor).
2. P-INSTALLER-V1 qualification and installed Server cutover/repair.
3. DJConnect declaration + CENTRAL attachment.
4. First real DJConnect Engineering Action.
5. `EP::STANDALONE_EP_VERIFIED`.
6. First real Engineering Platform self-development Action -> `EP::SELF_HOSTED_ENGINEERING_VERIFIED`.

Governance policy:

- One bounded bootstrap authority envelope may cover repository implementation, installer implementation, declaration creation, attachment, read-only inspection, deterministic validation, defect repair, exact-head requalification, hosted checks and repeated Human Security review without returning to the owner between each engineering sub-step.
- P-INSTALLER-V1 implementation/qualification is an engineering loop: implement -> validate -> diagnose -> bounded repair -> revalidate -> security re-review until merge/cutover-ready. P-NEUTRAL is complete and remains only as the preserved predecessor/closure evidence.
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

## Completed P-NEUTRAL authority closure

P-NEUTRAL removed active DJConnect naming/identity from generic EP runtime,
installation, lifecycle, configuration and logging authority. The merged final
closure commit is `b44af0914622dd57c5c5c2266ee2caf9b31d9007`; its authority
guard and closure register preserve historical-only artifacts and
migration-source references when explicitly classified. It is a completed
predecessor, not current work.

P-NEUTRAL closure requires zero active generic DJConnect platform identity within the qualified host/repository scope. It does **not** remove the historical EP implementation from the DJConnect source repository. DJConnect source retirement remains separately governed post-standalone work.

## Standalone verification contract

`EP::STANDALONE_EP_VERIFIED` is satisfied by evidence that the P-INSTALLER-V1-installed EP product can independently execute one real governed DJConnect Engineering Action end to end with no legacy execution authority required.

### First-loop gate decomposition and reconciliation status

This is a **MERGED_CANONICAL** reconciliation of the older migration-to-V1
dependency table, not a statement that its full umbrella gates are already
complete. The owning migration/DAG authority is updated together with this
roadmap before a narrower edge is treated as merged canonical sequencing. For
each gate, the first-loop test is physical: if the named capability is absent, can
one bounded Forge Action still enter through HTTP, receive durable identity,
be admitted, mutate its repository, validate/review/repair/finalize, retain
terminal evidence, and reconcile after Forge restart?

`EP_RUN_QUALITY_ASSURANCE_V1` is a bounded EP execution-contract increment:
reuse the existing quality lifecycle step for pinned, independent read-only
quality and security assurance, structured findings/readback and one shared
three-round repair budget. It is not a second orchestrator, generalized Agent
topology, or a claim of installed qualification until exact-head delivery and
installed evidence are retained.

| Umbrella gate | Exact first-loop capability | Evidence state at this proposal | First-loop disposition |
| --- | --- | --- | --- |
| Phase-3 package/install | A clean installed Server/CENTRAL/runtime that can run the canary | Historical dependency authority says incomplete; no current installed-proof claim is made here | `AUTONOMY_CRITICAL` bounded capability; full historical phase needs reconciliation |
| P-TRANSPORT | HTTP JSON -> Server -> Submission Service -> CENTRAL, with CLI/File Inbox retained as peer ingresses | Merged P-TRANSPORT authority and installed ingress qualification | `AUTONOMY_CRITICAL`; qualified transport capability |
| P-QUEUE | Durable submission/run identity, one serial mutating lane, lease/restart/replay protection, finalization/evidence for the canary | Older authority records broad qualification remaining | `PARTIALLY_AUTONOMY_CRITICAL`; not generalized queue/fairness productization |
| P-NEUTRAL | No dual current execution, Server/CENTRAL, routing, or credential authority in the canary scope | Merged closure `b44af091`; guarded current-authority inventory and retained historical classifications | `AUTONOMY_CRITICAL` completed predecessor; historical/forensic labels are not blockers when safely classified |
| P-INSTALLER | Reproducible Server-side install/repair/update and health for the one EP instance | Proposed here; qualification not yet claimed | `AUTONOMY_CRITICAL` bounded capability; excludes Forge, Workspace and general Agent productization |
| P-RELEASE | A trusted artifact/version/rollback path sufficient for the canary install | Older authority records active gap | `PARTIALLY_AUTONOMY_CRITICAL`; full channel/product release programme is not presumed required |
| Phase-P re-audit / installed Goldens / B8E | Zero-loss disposition and installed evidence for every capability claimed live by the canary | Older authority records these incomplete/blocked | `PARTIALLY_AUTONOMY_CRITICAL`; audit the canary capability set, not unrelated future product scope |
| Phase-S / Project Agent | The actual current provider path or the smallest CENTRAL-to-execution-edge protocol needed by the canary | Must be demonstrated by implementation and installed evidence | `PARTIALLY_AUTONOMY_CRITICAL`; generalized Agent fleet/topology is follow-on |
| B9 / standalone verified | One real attached-project governed execution with terminal evidence | Qualification milestone, not pre-existing capability | `AUTONOMY_CRITICAL` evidence milestone |
| Engineering Contract Foundation | Bounded Action identity, repository/write scope, readiness, validation, Human Gates, finalization evidence and correlation | Long-term richer producer contract remains planned | `PARTIALLY_AUTONOMY_CRITICAL`; do not weaken governance or require the whole future programme without evidence |

The durable reconciliation outputs are:

```text
P_NEUTRAL_FULL_CLOSURE_REQUIRED_FOR_FIRST_LOOP = only active-authority closure in the canary scope
P_NEUTRAL_MINIMUM_REQUIRED_SUBSET = singular Server/CENTRAL/lifecycle/routing/credential authority
P_NEUTRAL_DEFERABLE_REMAINDER = safely classified historical, forensic, and naming cleanup residuals
FULL_P_INSTALLER_REQUIRED_FOR_FIRST_LOOP = FALSE
GENERAL_AGENT_PRODUCTIZATION_REQUIRED_FOR_FIRST_LOOP = FALSE unless the canary proves a minimal protocol gap
```

These are conditional dependency semantics, not an authorization to bypass
qualification. Record actual implementation, qualification, and installed
runtime evidence in the owning migration/qualification records; do not infer
them from this roadmap.

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
| `EP::P_NEUTRAL_V1` | No active generic DJConnect platform identity/authority. | MERGED / CLOSED; completion evidence `b44af091`. |
| `EP::P_INSTALLER_V1` | Reproducible standalone Server-side EP install/repair/update boundary; excludes Forge/Workspace/general Agent productization. | CURRENT AUTONOMY FRONTIER. |
| `EP::STANDALONE_EP_VERIFIED` | One independent installed governed DJConnect execution with canonical evidence. | AFTER P-INSTALLER-V1 + DJConnect canary. |
| `EP::SELF_HOSTED_ENGINEERING_VERIFIED` | Installed EP executes a real bounded Engineering Platform self-development Action through CENTRAL. | IMMEDIATE POST-STANDALONE dogfood gate. |
| `EP::PROJECT_ATTACHMENT_AND_ADMISSION_V1` | Consumer-facing attachment/admission hardening beyond current B8R runtime. | FOLLOW-ON consumer qualification. |
| `EP::ENGINEERING_CONTRACT_FOUNDATION_V1` | Rich DoR/DoD/Human-Gate/Action-quality producer contract. | Long-term Forge producer contract. |

## Consumer and Workspace position

Workspace is not on the critical path for first standalone verification or first Forge -> EP -> Forge canary. Workspace itself is a valid later CENTRAL dogfood project, but that proof is not a prerequisite for first Forge autonomy.

## Source retirement

The old EP implementation/source remnants in DJConnect are not removed by P-NEUTRAL or P-INSTALLER-V1. After standalone authority, self-development and required responsibility-transfer evidence are established, a separately governed DJConnect source-retirement increment may remove obsolete EP runtime/source/wrappers/configuration while preserving historical/provenance evidence and justified migration material.

## 1.5 — Platform Productization

Completed and operational.

The completed 1.5 productization boundary remains historical maturity evidence. Current P-NEUTRAL/P-INSTALLER/bootstrap work is subsequent maintenance and standalone qualification, not a reopening of 1.5.

## 1.6 — Repository Extraction Readiness

Planned.

This historical milestone marker is retained for documentation-contract compatibility; current sequencing authority is the bootstrap critical-path section above.

## 2.0 — Versioned Platform Boundary

In review.

This historical milestone marker is retained for documentation-contract compatibility; it does not override the post-P-TRANSPORT critical path above.

## Policy

Platform code must not acquire DJConnect runtime, Home Assistant, branding or repository-name dependencies. Historical evidence and explicitly bounded migration compatibility may retain names without retaining authority.

The planned home deployment of one authoritative Forge installation, one authoritative EP installation and one primary execution host on a Mac mini is a deployment profile, not a product-wide singleton invariant.
