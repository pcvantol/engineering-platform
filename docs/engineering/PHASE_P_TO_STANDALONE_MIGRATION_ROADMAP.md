# Phase P to standalone — migration roadmap

**Status:** canonical forward roadmap

**Last reviewed:** 2026-09-02

This roadmap complements `PHASE_P_MIGRATION_GAPS_REGISTER.md`. The migration-gaps register remains the authoritative Phase-P completion gate; this document fixes the intended sequencing from the current Phase-P transition state through a fully operational standalone Engineering Platform.

## Architectural destination

The target product has one installation-owned CENTRAL authority. Local repositories and worktrees are physical execution bindings only. File Inbox, CLI, and HTTP are equivalent ingress transports that normalize into canonical CENTRAL submissions. Physical execution later moves behind one generic Project Agent binary; the Engineering Platform source repository and DJConnect are ordinary projects using that same Agent model.

The final product lifecycle also includes a Universal Installer. Version 1 owns Engineering Platform components only; Forge and Workspace are later components of the same installer architecture.

## Required migration sequence

1. **P-CENTRAL-CORE — single operational authority.** CENTRAL becomes the sole authority for lifecycle, checkpoints, leases, retry/recovery, operator resolution, merge waits, finalization, telemetry, provider usage, and execution-evidence indexing. A clean installation has exactly one operational database: `<data_root>/engineering.db`. Repository-local operational databases and canonical local `StateStore` authority are forbidden.
2. **P-CENTRAL-CONSOLE — CENTRAL-native Console.** Replace historical root-based dashboard delegation with project-scoped CENTRAL APIs for status, history, telemetry, evidence, configuration, logs, and downloads. With no selected project, render only platform/provider state or explicitly labelled all-project aggregates.
3. **P-TRANSPORT — three canonical ingress transports.** File Inbox, CLI, and HTTP all normalize through the same submission service into CENTRAL. File watching is a thin transport adapter and owns no queue, retry, lifecycle, or recovery truth.
4. **P-QUEUE — formal project-scoped FIFO.** Exactly one execution lane exists per project, at most one execution occupies that lane, and different projects may execute concurrently. Qualification covers races, restart, retry, predecessor blocking, merge wait, finalization, and browser projection.
5. **P-NEUTRAL / runtime authority.** Remove active `DJCONNECT_*`, `com.djconnect.*`, product defaults, special cases, and obsolete standalone entrypoints. All supported processes consume one typed installation runtime configuration.
6. **P-INSTALLER — Universal Installer v1.** Introduce the component-generic installer architecture with Engineering Platform components only: Server/CENTRAL, Operations Console, CLI, File transport, managed Codex runtime, generic Project Agent binary, service registration, repair/uninstall, installation configuration, component inventory, and health. The installer is not a second configuration authority. Forge and Workspace are later components of this same installer.
7. **P-RELEASE — trusted release/update authority.** Establish signed, checksummed EP wheel releases, an embedded verification trust root, release channels, compatibility policy, quiesce/atomic activation, preflight, and rollback. GitHub Releases or an equivalent governed registry is distribution; cryptographic verification establishes trust.
8. **Phase-P migration re-audit.** Every active migration-gap entry must be repaired or explicitly retired. Required invariants include one operational database, zero active local operational authority, zero active DJConnect coupling, CENTRAL-native project isolation, and all three transports operational.
9. **P-D — final installed product Goldens.** Resume the historically BLOCKED P-D qualification and prove Managed, Genesis, and armed-interrupt/retry end-to-end behavior on the migrated architecture. `P_D_PREVIOUS_ATTEMPT = BLOCKED` remains historical truth until this final qualification passes.
10. **CUTOVER-DJCONNECT.** Perform reverse responsibility/zero-loss audit, then remove the embedded Engineering Platform implementation, services, workflows, runtime, scheduler, Console, and provider host from the DJConnect repository. Historical provenance may remain. DJConnect retains only project-specific EP declaration/configuration and later Agent binding metadata.
11. **Phase S — generic Project Agent execution.** Move physical execution from the Server behind the generic Project Agent boundary. CENTRAL retains admission, scheduling, lifecycle, and evidence authority. Agent disconnect/reconnect and recovery must not create a second scheduler or lifecycle authority.
12. **Real-project Agent configuration.** Configure the same generic Project Agent binary for the `engineering-platform` source project and for `djconnect`. No project-specific Agent binary or DJConnect-specific execution code is allowed.
13. **Agent Goldens / dogfooding.** Prove EP can develop EP and DJConnect through the Agent seam, both projects can execute concurrently while each retains one FIFO lane, and Agent interruption/reconnect recovers deterministically from CENTRAL.
14. **Standalone operational classification.** Only after the preceding gates may the product be classified `STANDALONE_ENGINEERING_PLATFORM_OPERATIONAL`; Agent completion is separately classified `GENERIC_PROJECT_AGENT_OPERATIONAL`.

## Permanent Golden assurance model

The installed Managed and Genesis canaries being developed during P-CENTRAL-CORE are not disposable qualification scripts. Their reusable harness and scenarios must become permanent release assurance.

### Deterministic CI Goldens

For main/release-candidate CI, exercise the real installed product chain:

```text
fresh installed wheel
→ Server
→ CENTRAL
→ installation enrollment
→ canonical ingress
→ LifecycleWorker
→ dispatcher
→ EngineeringRunner
→ provider boundary
→ validation/review/finalization
→ CENTRAL evidence
```

Only external nondeterministic systems are substituted:

- managed Codex uses a deterministic contract-faithful fake provider;
- GitHub uses a deterministic contract-faithful fake GitHub boundary.

The Server, CENTRAL, worker, dispatcher, EngineeringRunner, validation, lifecycle state machine, evidence persistence, project isolation, and filesystem-authority assertions must remain real. Tests must not mock internal lifecycle transitions merely to make the Golden deterministic.

The standard deterministic Golden suite must cover at minimum:

- **Managed:** submission → claim → deterministic provider delivery → validation → PR/check semantics → review-ready/merge event → finalization → terminal expected state;
- **Genesis:** canonical Genesis bootstrap and provider lifecycle through its expected terminal semantics;
- **Armed retry:** execution → explicit armed interruption → durable CENTRAL recovery/retry → terminal expected state;
- **Restart/recovery:** restart while lifecycle state is durable and recover from CENTRAL;
- **Two-project isolation/FIFO:** A1 and B1 may overlap, A2 may not overlap A1, and project data/actions remain isolated;
- **Authority invariants:** `OPERATIONAL_DATABASE_COUNT = 1`, repository-local operational DB created = 0, canonical local StateStore created = 0.

These deterministic Goldens are intended to run as standard qualification for changes on `main` and for release candidates, subject to the repository's normal cost/sharding policy. A release candidate must not depend on live Codex or GitHub availability for deterministic lifecycle correctness.

### Real-service qualification

Keep a separate real-service qualification using the EP-managed Codex runtime and disposable GitHub repositories. Its purpose is adapter/authentication/integration assurance, not deterministic lifecycle regression. It may run as a release qualification, scheduled qualification, or another governed lower-frequency gate rather than on every commit.

The same Golden scenario definitions should support both modes where practical:

```text
ManagedScenario(provider=FakeCodex, github=FakeGitHub)   # deterministic CI
ManagedScenario(provider=RealManagedCodex, github=RealGitHub) # real-service qualification
```

The implementation must preserve the architectural boundary: swap external adapters, not the internal lifecycle.

## Golden harness ownership

Build one reusable **EP Golden Harness**, not separate P-CENTRAL-CORE, P-D, release, and Phase-S test worlds. It should own reusable installed-package bootstrap, disposable CENTRAL, project/repository enrollment, repository declaration provisioning, provider/GitHub adapters, lifecycle observation, filesystem-authority assertions, and expected-state assertions.

P-CENTRAL-CORE canaries feed this harness. P-D promotes the Managed/Genesis/armed-retry scenarios to final installed-product Goldens. Phase S reuses the same scenarios over the Project Agent seam. The release pipeline then consumes the deterministic harness as permanent regression assurance.

## Reading rule

Passing deterministic or real-service Goldens never supersedes unresolved `ACTIVE GAP` entries in `PHASE_P_MIGRATION_GAPS_REGISTER.md`. Conversely, a migration gap is not considered safely closed merely because its implementation exists; the required focused and end-to-end evidence must also exist.
