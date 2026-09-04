# Phase P to standalone — migration roadmap

**Status:** canonical forward roadmap

**Last reviewed:** 2026-09-02

This roadmap complements `PHASE_P_MIGRATION_GAPS_REGISTER.md`. The migration-gaps register remains the authoritative Phase-P completion gate. The [migration-to-V1 dependency authority](../development/ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md#migration-to-v1-dependency-authority) is authoritative for the ordering and stable identifiers through `EP::STANDALONE_EP_VERIFIED`; this document must not create a conflicting sequence.

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
10. **Phase-S execution-protocol foundation.** Implement only the minimum CENTRAL-to-Project-Agent protocol and parity evidence required for one governed execution. This is a B9 prerequisite; CENTRAL retains admission, scheduling, lifecycle and evidence authority.
11. **B8E and B9 / `EP::STANDALONE_EP_VERIFIED`.** Complete the zero-loss audit and activate the clean standalone authority only after every prerequisite named in the canonical migration-to-V1 dependency authority passes.
12. **`EP::SOURCE_RETIREMENT_DJCONNECT_V1`.** After standalone verification, perform the reverse responsibility audit and retire obsolete DJConnect EP source, services, workflows, runtime, scheduler, Console and provider-host entrypoints. Historical provenance remains read-only. This is source retirement, not the runtime-authority cutover.
13. **Phase-S real-project Agent qualification.** Configure the generic Agent for the EP source project and DJConnect, then prove multi-project dogfooding, FIFO isolation and deterministic Agent interruption/reconnect recovery. This broader programme is post-standalone.
14. **Standalone operational classification.** Classify `STANDALONE_ENGINEERING_PLATFORM_OPERATIONAL` after the preceding standalone gates; classify `GENERIC_PROJECT_AGENT_OPERATIONAL` only after the broader Agent qualification.

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
