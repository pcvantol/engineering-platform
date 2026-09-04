# Engineering Platform 2.x extraction and migration plan

**Status:** Phase 0 complete; Phase 1 complete / qualified; Phase 2 closed / retired clean-slate decision; Phase 3 authorized for history-preserving physical extraction
**Scope:** Engineering Platform 2.x extraction from `pcvantol/djconnect` to a
standalone, local-first Execution Operations Platform
**Decision baseline:** [ADR-0019](../adr/0019-engineering-platform-central-installation-store.md) and the
[EP consumer contract](ENGINEERING_PLATFORM_CONSUMER_CONTRACT.md)

> **B8R authority supersession:** For Git-declared project/repository
> attachment, the current authority is
> [Project identity and attachment runtime](../engineering/PROJECT_IDENTITY_AND_ATTACHMENT_RUNTIME.md).
> Earlier Workspace-supplied `project_id` wording remains historical consumer
> registration context and must not be read as topology authority.

## Review objective

Turn Engineering Platform (EP) from the current repository-local implementation
into a neutral, independently released product without losing operational
evidence, creating competing writers, or changing Forge/Workspace ownership.

The end state is one EP installation per local user/machine, one
installation-owned SQLite database, and one isolated execution queue per
canonical Workspace project. DJConnect and Forge/Workspace consume an
immutable, pinned EP wheel; neither retains EP source code.

This is a staged migration, not a rewrite or a history rewrite.

## Non-negotiable invariants

- Forge owns why and what should be engineered: canonical project/product
  planning identity, Mission Candidates, Missions, Engineering Actions,
  planning dependencies, planning/governance decisions, producer intent and
  Forge/Workspace-owned Runtime Prompts. Forge is not execution authority.
- EP owns how engineering is executed and delivered: project registration,
  admission, execution lifecycle, queues, leases, telemetry, evidence,
  Engineering Reports, Execution Receipts, Prompt History, PR/merge
  observation, Finalization, reconciliation, governed recovery actions and the
  Operations Console. EP is not a planner.
- Workspace owns presentation, comprehension and user interaction. It may
  compose Forge and EP projections, but never becomes canonical authority for
  planning or execution lifecycle.
- `project_id` is immutable, opaque and canonically supplied by Workspace.
  A path, repository name or display label is never an identity substitute.
- Workspace supplies the mutable, human-friendly `project_name`. EP uses it
  for the selector and diagnostics only; it does not copy the label into
  historical evidence rows.
- Every EP operational row and query is scoped by `project_id`.
- Each registered project has a separate Inbox route, FIFO queue and lease
  domain. Work for one project cannot be admitted by another project.
- There is exactly one active EP writer for the installation database. Legacy
  per-workspace stores are never left live after cutover.
- The physical Inbox route and the Workspace/Forge API route remain parallel,
  validated admission routes.
- SQLite remains operational truth. Files remain transport, rendered evidence,
  export or fallback—not a competing lifecycle authority.
- Repository evidence remains stronger than dashboards, telemetry, analyses or
  reviewer projections.
- All user-facing Operations Console copy remains complete for `en`, `nl`,
  `de`, `fr` and `es`.

## Identity and external scoping

EP installation identity, consumer identity, project identity, run identity,
producer identity and Forge Mission/Engineering Action identity are independent
concepts. They must not be derived from each other unless an accepted versioned
contract defines the correlation.

| Identity | Owner | Meaning |
| --- | --- | --- |
| EP installation identity | EP | One installed host and its local data root. |
| Consumer identity | EP credential registry | An authenticated DJConnect, Forge or Workspace caller. |
| Project identity | Workspace | Immutable, opaque canonical project identity. |
| Run identity | EP | EP execution and evidence lineage. |
| Producer identity | Consumer/Forge contract | Source of an admission request. |
| Mission/Engineering Action identity | Forge | Planning correlation, never an EP planning graph. |

The normal correlation is Workspace consumer identity to explicit project
identity to EP admission to EP run identity; independently, Forge Engineering
Action to producer/contract correlation to EP admission/run. Forge, Workspace
and DJConnect are three independently authenticated consumers; consumer
identity is not project identity.

Every project-scoped external EP operation requires explicit project identity:
reads, submissions, queue operations, execution actions, evidence/report/usage
queries, Prompt History queries, AllowedAction evaluation and governed recovery
actions. A UI's selected project is presentation state only and can never be
backend authority. Missing, mismatched or cross-project scope fails closed.
Only an explicitly named, capability-gated and audited installation-wide
administrative API may omit project identity.

## Target topology

```text
DJConnect consumer ─┐
Forge runtime ──────┼──> Local Consumer API ──> application services
Workspace BFF ──────┘                               │
                                               policy / projections / controls
                                                        │
Operations Console ───────────────────────────────> repositories
                                                        │
                                           installation-owned SQLite DB
                                                        │
                                     project A / B / C Inbox, queue and lease
```

The installation data root is outside every consumer repository and outside
the wheel. Consumers never construct its filesystem paths. EP resolves a
platform abstraction such as **user_data_dir(\"Engineering Platform\")**. On
macOS it resolves below the user Application Support directory; other supported
operating systems use their equivalent per-user application-data directory.
Its conceptual contents are the database, backups, logs, per-project Inbox
roots, runtime state and diagnostics; these internal paths are not a stable
consumer API.

## Local Consumer API, credentials and browser boundary

The versioned **Local Consumer API** is the long-term machine-to-machine
integration boundary. Loopback HTTP and/or a platform-appropriate local socket
may implement it behind the same application-service contracts. Workspace must
not structurally integrate by shelling out to EP CLI commands. The CLI remains
for humans, diagnostics, bootstrap, recovery, administration and explicitly
supported scripts.

Dependency direction is mandatory: API facade → application services →
projections/policy/controls → repositories/domain. Application or domain code
must not depend on HTTP implementation details.

EP authenticates at least three distinct consumers: DJConnect, Forge and
Workspace. Each receives a unique capability-based local credential, stored in
OS credential/keychain facilities where available, never in a project
repository and never exposed unnecessarily to browser JavaScript. Conceptual
capabilities include read, submit, action and admin, with narrower scopes and
allowed project identities as accepted by contract. Authentication proves caller
identity; EP policy, fresh evidence and required confirmation still decide
whether an action is allowed.

Workspace browser traffic goes through a Workspace BFF, which authenticates to
EP independently. Forge and DJConnect authenticate independently too. An
unauthenticated or merely locally reachable browser endpoint is never
execution authority. Mutation requires authenticated consumer/operator context,
explicit project scope, EP policy authorization, fresh evidence where required
and any required confirmation.

Read-only AI receives bounded, redacted projections. It may explain, summarize,
analyze and recommend existing AllowedActions. It may not change host
configuration, repository paths or project registration; mutate lifecycle;
invent executable actions; or become authentication or policy authority. Any
future recommendation passes the normal EP action gateway and fresh policy
evaluation.

## Delivery sequence

## Authorized Phase 1 increment sequence

The Phase 1 sequence is deliberately bounded. Increments 1, 2 and 2a are
complete; schema 39 is activated and the loopback runtime is post-merge
qualified.

| Increment | Canonical name | Authorization |
| --- | --- | --- |
| Phase 1 / Increment 1 | **Local Consumer API Contract Foundation** | Complete, contract-only |
| Phase 1 / Increment 2 | **Local API Transport + Authentication Runtime** | Implemented and post-merge qualified; schema 39 active |
| Phase 1 / Increment 2a | **Qualification Credential Boundary** | Complete, bounded qualification-only seam |
| Phase 1 / Increment 3 | **Consumer Registration + OS Credential Integration** | Complete / qualified; schema 40 active |

Increment 1 defines the versioned Local Consumer API v1 contract and
fail-closed validation only. It must not add an HTTP listener, Unix-socket
server, bearer generation or verification runtime, Keychain integration,
consumer cutover, storage migration, service-installation change or network
exposure.

**Increment 1 completion:** the repository now contains the provider- and
consumer-neutral v1 envelope module, deterministic normalization and JSON
serialization, stable safe errors, credential redaction and focused contract
coverage. The extraction audit reports 285 candidates classified exactly once
with no extraction-blocking imports. No runtime transport, authentication or
credential authority, Keychain call, storage migration or consumer cutover was
added. ADR-0021 now authorizes, but does not implement, Increment 2.

**Increment 2 implementation:** ADR-0021 authorizes and the repository now
implements only a
dedicated EP-managed loopback HTTP service; bounded health and read-only v1
capability endpoints; fail-closed bearer authentication; exact project scope;
schema-39 EP verifier metadata; service doctor integration; and isolation from
the existing lifecycle. It authorizes neither a consumer cutover nor any
mutating engineering action. Operator issuance and consumer Keychain work stay
in Increment 3.

The implementation adds the dedicated `127.0.0.1:8766` standard-library HTTP
service, unauthenticated bounded health, authenticated read-only capability
projection, verifier-only schema-39 metadata, LaunchAgent/doctor/desired-state
integration and focused regression coverage. It adds no credential issuance,
Keychain use, consumer cutover, mutation endpoint or standalone repository.
Schema 39 was activated only through the separately governed post-merge
quiescence procedure and is now active.

**Increment 3 architecture authorization:** ADR-0022 authorizes an explicit
EP-owned `(consumer_id, project_id)` registration authority, production
credential lifecycle and macOS Keychain consumer adapter. It requires a
minimal schema-40 registration table while preserving schema-39 verifier-only
records and the existing bearer path. It authorizes no schema activation,
Keychain call, credential issuance, Local API mutation, Forge/Workspace/
DJConnect cutover or standalone repository extraction until a separate
implementation increment.

**Increment 3 post-merge qualification:** a valid, non-revoked production
credential under an `ACTIVE` exact registration succeeds; the same credential
under a `DISABLED` registration is denied as authorization (`403`); and a
revoked credential is denied as authentication (`401`).

**Phase 1 closure:** Phase 1 is **COMPLETE / QUALIFIED**. Schema 40 is active;
the Local Consumer API is ready; and consumer registration plus credential
lifecycle are qualified. This rolling plan supersedes earlier deferred-schema
projections; immutable historical evidence remains unchanged.

## Phase 2 closure and Phase 3 entry

| Increment | Canonical name | Authorization |
| --- | --- | --- |
| Phase 2 / Increment 1 | **Installation Data-Root Contract and Central-Store Migration Guardrails** | Complete; documentation/control only; no store moved |
| Phase 2 / Increment 2 | **Central-Store Migration Tooling + Dry-Run Qualification** | Durable control/forensic capability retained; current live migration retired |
| Phase 2 / Increment 3 | **Controlled Central-Store Cutover** | Retired for migration `41feb31e-2e25-42c4-bca1-bbfc97dde6f4`; no Stage A, thaw, recovery or reverse reconciliation |
| Phase 2 / Increment 4 | **Post-Cutover Qualification + Central-Store Active Baseline** | Retired for the current migration; not an extraction prerequisite |

The canonical implementation controls remain [EP central-store migration guardrails](../engineering/EP_CENTRAL_STORE_MIGRATION_GUARDRAILS.md), hermetic authority isolation and generic forensic tooling. Under [ADR-0026](../adr/0026-ep-clean-slate-standalone-store-and-migration-retirement.md), the current migration is `RETIRED_FOR_CLEAN_SLATE_EXTRACTION`; its LEGACY/CENTRAL stores are read-only forensic evidence and not a standalone seed. Phase 3 may begin after this documentation/governance reconciliation. It performs history-preserving physical extraction before standalone package/import qualification, official schema-41 fresh-store bootstrap, standalone service qualification, and later consumer cutover.

### Canonical MVP activation and cutover runbook

This section is the current authoritative runbook for the clean-slate MVP. It
supersedes any interpretation of the retired Phase-2 database-copy cutover as
the route to standalone authority. It changes neither a runtime nor an
authority pointer by itself.

#### Authority and development-execution boundary

The legacy DJConnect-hosted EP is **suspended**. It remains non-authoritative
for development execution and must not be reactivated, thawed, or used for a
dual-run, shadow-mode, temporary-EP, bootstrap-scheduler, or migration
handoff. Its database and receipts are historical evidence only.

Until `STANDALONE_EP_VERIFIED` is recorded, all development execution for this
critical path is performed directly through the locally installed, native
Codex CLI, outside EP. Native Codex CLI is the temporary development execution
mechanism; it is not a temporary EP runtime, a new lifecycle/queue authority,
or a substitute Project Agent. This boundary does not change Forge planning
authority, Workspace project-identity authority, or the provenance of existing
evidence.

`DEVELOPMENT_HOST_MATCH` is a narrowly scoped development-host diagnostic
bypass. It may be used only for the already known host-drift condition and
only with its recorded drift evidence. It must not waive any other host,
package, service, project, credential, provider, or governed-execution gate.

#### Clean CENTRAL bootstrap: no MVP database migration

The new CENTRAL EP installation begins with a newly created, empty,
installation-owned operational database and a new installation identity. MVP
does **not** migrate, seed, merge, replay, or otherwise copy a legacy database
into CENTRAL. The following state is intentionally not transferred:

| Legacy state | MVP disposition in clean CENTRAL |
| --- | --- |
| Database, operational rows and runtime identity | New empty database and new installation identity. |
| Agents and hosts | Newly installed, paired and registered. |
| Projects and project registrations | Registered afresh from the canonical Workspace project identity. |
| Queues, leases, runs and execution/finalization state | Not resumed or copied; new lifecycle begins at activation. |
| Consumer and host credentials | Newly provisioned through the approved credential flow; no legacy secret is copied. |
| Prompt History, receipts, reports and migration artifacts | Independently retained as immutable/read-only historical evidence, never as runtime seed data. |

Historical evidence/provenance remains preserved independently of the new
operational database: retain the existing read-only stores, fingerprints,
receipts, reports, authority history and access provenance under the archival
rules in ADR-0026. It is neither deleted nor made queryable through the new
operational lifecycle merely because CENTRAL is activated.

#### Required activation sequence

1. Complete final EP #1 qualification and merge, making
   `engineering-platform/main` the canonical source authority.
2. Use native Codex CLI to complete the standalone EP Server artifact/runtime,
   Project Agent artifact, Server-to-Agent protocol and project-attachment
   contract. Do not start a migration runtime or reactivate legacy EP while
   doing so.
3. Install and qualify the standalone EP Server against its clean, empty
   CENTRAL operational database; install and start a newly registered Project
   Agent, then pair it with that Server.
4. Attach the existing DJConnect checkout as the first project through a fresh,
   explicit canonical project registration. This is a non-destructive
   attachment, not a migration of DJConnect state or an import of its old EP
   implementation.
5. Prove the first governed end-to-end execution through
   `EP Server -> Project Agent -> provider/Codex`, including the required
   authority, project-scope, credential, service and evidence gates.
6. Only after steps 3--5 qualify, record `STANDALONE_EP_VERIFIED` and cleanly
   activate the new CENTRAL installation as the execution authority. No legacy
   database cutover, writer handoff, reverse reconciliation, or legacy thaw is
   part of this activation.
7. Only after `STANDALONE_EP_VERIFIED` may the obsolete EP implementation in
   DJConnect be cleaned up through a separately reviewed source-retirement
   change. Preserve historical evidence and the stated authority boundaries.

If a qualification gate fails, CENTRAL is not activated and native Codex CLI
remains the development-execution mechanism. A failed gate never authorizes a
legacy restart, a database migration, a second writer, or a broader
`DEVELOPMENT_HOST_MATCH` bypass.

### Phase 0 — Freeze the extraction baseline

**Purpose:** establish the exact 2.x source and evidence baseline before any
repository movement.

1. Tag the final in-repository EP baseline and capture its version, schema,
   qualification report and package manifest.
2. Re-run the extraction audit for imports, runtime names, public entry points,
   test ownership, documentation and workflows.
3. Produce a reproducible, versioned path manifest that classifies every
   candidate item as EP product source, EP test, EP documentation, EP workflow,
   EP release asset, consumer adapter, DJConnect retained, generated local-only
   or excluded.
4. Analyse the current CI workflows and their individual jobs/steps before
   selecting the standalone pipeline. Record each check's purpose, inputs,
   owner, blocking status and whether it is EP-standalone relevant, EP
   consumer-integration relevant, or excluded. Retain or replace only the
   standalone-relevant evidence (for example EP unit, browser, localisation,
   package, migration/recovery, security and installer checks). Keep
   Home-Assistant integration validation, HACS, hassfest and other
   DJConnect-consumer checks in the consumer repository unless an explicit EP
   consumer-contract integration test needs them. The resulting matrix must
   name any equivalent standalone replacement rather than silently dropping a
   required assurance property.
5. Confirm public command compatibility: neutral commands stay neutral, and
   consumer-specific commands are either adapters or explicitly retired under a
   migration notice.

**Exit evidence**

- repeatable file/import audit with no Home Assistant, DJConnect runtime or
  repository-name dependency in EP product code;
- a complete export list of source, tests, documentation, workflows and
  qualification assets; and
- a reviewed current-CI-to-standalone-CI matrix, including every retained,
  replaced and consumer-only check/step with its rationale and the required
  standalone evidence replacement where applicable;
- an approved baseline tag, source commit, EP version, storage schema, Consumer
  Contract version, qualification evidence and rollback reference.
- public entry-point, runtime-name and filesystem/current-working-directory
  dependency audits.

### Phase 1 — Complete the public contract and service boundary

**Purpose:** make the consumer boundary explicit before moving code.

**Architectural decisions:** HTTP with versioned JSON contracts is the
canonical consumer contract; its contract is independent from bind/exposure
policy. Initial runtime exposure remains fail-closed and configuration
controlled, and may be loopback-only when Increment 2 is explicitly
authorized. Unix-domain sockets are not the public contract, although a later
implementation may use one internally without changing that contract.

EP is the credential authority. Each registered consumer/project relationship
will use one opaque cryptographically random bearer credential scoped to the
registered consumer identity and canonical `project_id`. EP will own issuance,
fingerprint/verifier metadata, validation, revocation and rotation; it will
never persist reusable plaintext credentials. Consumers persist their opaque
credential in the native OS secret store (Apple Keychain on macOS), never in a
repository, consumer SQLite store, Prompt History, report or dashboard. These
are contract decisions only in Increment 1; their runtime implementation is
reserved for later increments.

1. Turn the accepted consumer contract into versioned machine-readable host
   API schemas: input fields, types, enum values, length limits, newline and
   Unicode-normalisation rules, unknown-field rejection and stable error
   codes.
2. Keep server-side validation authoritative. Browser validation remains
   defense in depth only.
3. Define project registration, registration refresh and project selection
   operations. Registration must include canonical `project_id`, current
   `project_name`, validated workspace/repository location and project Inbox
   root.
4. Define capability credential issuance, rotation, project restriction and OS
   credential storage for three separate consumers without making an
   unauthenticated dashboard endpoint execution authority.
5. Explicitly protect read-only AI-chat inputs from changing host
   configuration, repository paths or system context.
6. Refactor internally, while retaining one local host process, into:

   ```text
   HTTP/API facade → application services → projections/controls → repositories
   ```

**Exit evidence**

- contract tests for valid, invalid and unknown input fields;
- redacted diagnostics tests that never retain raw prompts or sensitive input;
- consumer compatibility matrix for DJConnect, Forge and Workspace; and
- documented version negotiation and fail-closed behavior.

### Phase 2 — Central-store and project-scope migration

**Status:** **CLOSED / RETIRED CLEAN-SLATE DECISION.** The data-root,
single-writer, quiescence, migration-control, hermetic and forensic foundations
are durable Phase-2 deliverables. Migration
`41feb31e-2e25-42c4-bca1-bbfc97dde6f4` is
`RETIRED_FOR_CLEAN_SLATE_EXTRACTION`; its historical stores and reports are
read-only evidence, not a standalone seed. See [ADR-0026](../adr/0026-ep-clean-slate-standalone-store-and-migration-retirement.md).

**Purpose:** establish the reusable 2.x data model and controls; not migrate
the current DJConnect-hosted database into standalone EP.

1. Introduce the installation-owned EP data root and one SQLite database.
2. Add a `projects` registration table keyed by `project_id`, storing the
   current `project_name`, locations, Inbox root and registration metadata.
3. Add non-null `project_id` references, indexes and query filters to every
   EP-owned operational entity: Inbox/admission records, queues, leases,
   executions, lifecycle state, receipts, reports, Prompt History, telemetry,
   logs, status projections and project-scoped dashboard preferences.
4. Keep installation-wide configuration unscoped: EP version, provider
   capabilities, update state, log retention, log level and component/dashboard
   refresh settings.
5. Keep project configuration within the selected queue: Inbox route, Inbox
   scan cadence and open-pull-request check cadence.
6. Move free disk space, database path, database size and schema version to a
   dedicated machine/platform diagnostics block.
7. Implement forward-only migrations with transactional execution,
   pre-migration backup, integrity check, compatibility gate and a documented
   restore procedure. A created backup is not sufficient: restore must be
   rehearsed and prove that the restored database starts without a second
   writer.
8. Prove legacy store cardinality is exactly one before backfilling a single
   project identity. Repository location is not evidence. Multiple identities,
   ambiguous evidence or a failed proof stop migration for explicit operator
   resolution; no heuristic selection or history merging is permitted.
9. Preserve the historic legacy-to-central controls and forensic artifacts;
   do not invoke the retired incident migration as a standalone bootstrap.
10. Define fresh schema-41 bootstrap independently from future legitimate clean
    schema-40-to-41 compatibility support. MVP uses the fresh bootstrap only;
    neither path copies current DJConnect-hosted data, credentials,
    registrations or project scope.

**Exit evidence**

- migration fixtures for empty, populated, interrupted and rollback paths;
- verified backup-and-restore exercise, including periodic restore evidence;
- two-project isolation tests covering queues, leases, Inbox admission,
  dashboard filter, telemetry, reports and Prompt History;
- proof that a Workspace rename updates only the registration label; and
- proof that no legacy process can write after cutover.

## Central-store operational rules

SQLite remains the operational truth. Files are transport, rendered evidence,
exports, fallback or already-canonical immutable artifacts; central indexing
does not turn them into a competing lifecycle database. Centralization retains
original project/run identity, timestamps, authority/evidence, terminal
outcome, Prompt History semantics and report/receipt identity. A project name
change never rewrites historical evidence.

Installation-scoped data includes installation/version, provider capabilities,
update state, log retention, log level, global component-refresh settings,
database diagnostics and machine diagnostics. Project-scoped data includes
Inbox registration, admission, queue, leases, execution lifecycle, receipts,
reports, Prompt History, telemetry, project dashboard preferences and accepted
project scan/check cadence. Project scope is attached only where semantically
owned by a project; it is not added mechanically to genuinely global data.

Repository/data-access helpers must make unsafe unscoped project queries
difficult or impossible. Every project query requires the explicit project
identity unless it is a separately named privileged installation-wide query.
Cross-project isolation tests are mandatory.

Each project owns an isolated FIFO admission/queue domain. FIFO is guaranteed
within a project. Installation scheduling selects among eligible project queues
without altering Forge planning priority semantics; fairness and parallelism are
a later EP operational-scheduling decision and are not silently frozen by this
migration. A project execution lease is scoped by installation, project identity
and run/lease identity. Any globally exclusive host resource is an explicit
installation-level resource lease, not an overloaded project lease.

The physical project Inbox and Workspace/consumer API remain parallel
transports. Both converge on the same authoritative admission service and
validate consumer/project identity, submission contract, authority,
deduplication/idempotency and admission rules. There is one EP admission and
lifecycle domain, not one per transport.

### Phase 3 — Productise the EP repository and package

**Purpose:** create `pcvantol/engineering-platform` with relevant history and
an independently releasable package plus a clean standalone store.

The required order is physical code extraction, standalone package/import
qualification, official schema-41 fresh-store bootstrap, standalone service
qualification, fresh Project-Agent/DJConnect attachment, first governed
execution, then `STANDALONE_EP_VERIFIED` clean activation. No service identity
or authority transition is implied by repository extraction alone.

1. Create the new repository using a history-preserving filtered export of the
   audited EP source paths. Do not rewrite DJConnect history or cosmetically
   fabricate independence. Version the extraction command/configuration and
   record source repository, baseline tag and commit, path-manifest version,
   extraction date/tool version, license/provenance review and initial
   standalone release commit.
2. Establish neutral package namespace `engineering_platform` and neutral
   console commands, including `engineering-execution-host`.
3. Move EP-owned source, tests, package metadata, documentation, CI workflows,
   qualification registry and release tooling together. Retain only thin
   consumer adapters in DJConnect.
4. Ensure the package has no implicit current-working-directory or repository
   source-tree dependency. Data-root resolution and consumer registration are
   explicit.
5. Build a dedicated EP CI pipeline: unit, browser, localisation and package
   installation tests; migration/recovery tests; **required security gates**
   for dependency vulnerability scanning, static analysis, secret scanning,
   package/SBOM provenance and installer checksum/signature verification;
   release signing/publication policy; and immutable wheel release. The
   publication job builds from a clean, tagged checkout in explicit production
   release mode, never from a developer worktree or a CI test environment. It
   must install the resulting wheel into a fresh environment and verify both
   its runtime manifest and its installed contents against an allowlist:
   production package code, required static runtime assets, license/notices,
   package metadata and explicitly approved operational templates only. Tests,
   test fixtures/results, browser traces/screenshots, coverage data, source
   checkout metadata, local databases/logs, development documentation, build
   caches, `node_modules`, debug tooling and development-only dependencies are
   rejected from the published wheel. Debug endpoints, development defaults
   and verbose diagnostic instrumentation are disabled or excluded by the
   production build configuration. CI stores the manifest/SBOM/checksum as
   release evidence and rejects publication on any unexpected file, package
   dependency or debug/release-mode mismatch. A failed, skipped or unavailable
   required security or release-artifact check blocks publication rather than
   being treated as advisory evidence.
   The extracted repository carries the canonical
   [EP non-functional requirements](../engineering/ENGINEERING_PLATFORM_NON_FUNCTIONAL_REQUIREMENTS.md)
   matrix and turns every standalone release-gate requirement into a dedicated
   CI/qualification check.
6. Deliver a signed, notarized native macOS **Engineering Platform** installer
   application alongside the wheel. It is the supported first-install and
   repair experience for a local EP host; command-line installation remains a
   documented administrator alternative, not a separate runtime.
7. Publish the first pinned standalone EP wheel as **`2.0.0`** only after
   package install, native clean-machine installation and rollback proof
   succeed. The initial consumer pins must reference that exact version; no
   floating `2.x` range is permitted for the first release.
8. Define compatibility explicitly across EP version, Consumer Contract
   version, storage schema, DJConnect adapter, Forge adapter and Workspace
   adapter/BFF. Unknown or incompatible major versions fail closed; successful
   imports are insufficient proof.

**Exit evidence**

- a reproducible wheel installed into a clean environment without DJConnect
  source;
- production-release-mode evidence: the installed wheel's allowlisted file
  manifest, runtime dependency set, disabled debug profile, SBOM and checksum;
- dedicated EP CI and release evidence;
- passing, non-skipped required security-check evidence for the wheel and
  native installer; and
- all five locales verified from the installed package; and
- history and license provenance review of the new repository.

#### Native macOS installation and first-run contract

The native installer is an EP product surface, not a DJConnect bootstrap
adapter. Its signed macOS application is a user-friendly wrapper around the
idempotent installed command `engineering-platform-host --install`; it does
not maintain a second installation implementation. The application packages
or retrieves only the verified, pinned EP release, obtains the operator's
explicit confirmation and then invokes that command to perform the following
sequence:

1. inspect any existing EP installation and acquire an installation-wide
   installer lock, so two installers or a running writer cannot race;
2. verify the signed/notarized application, the pinned wheel and every
   supported dependency installer before invoking it;
3. install or upgrade the EP wheel and the supported Codex CLI and GitHub CLI
   dependencies when absent, using only EP-approved installers and explicit
   privilege elevation where macOS requires it;
4. create the per-user EP application-data root, an empty installation-owned
   SQLite database, backups/log/runtime directories and OS credential-store
   entries with restrictive permissions;
5. install and load the EP dashboard and watcher LaunchAgents, configured
   only with the new installation root and installed EP commands;
6. verify the installed command versions, database integrity/schema, service
   health, one-writer ownership and the watcher ready record including its
   resolved Inbox root; and
7. only after all checks pass, open the loopback Operations Console for the
   initial provider setup.

##### Single-installation and existing-data decision

EP permits exactly one installation for one local macOS user. Before any
write, `engineering-platform-host --install` resolves the canonical
installation data root, acquires an installation-wide exclusive lock and
checks its signed installation marker, database, service labels and active
writer lease. A second native app, a command-line invocation, or an already
running writer must therefore fail closed rather than create a second database
or a competing watcher.

When a previous EP data root or database is found, the native installer must
stop before changing it and present one explicit choice:

| Choice | Required behaviour |
| --- | --- |
| **Use existing data** | Retain the installation identity and database; make a verified backup before any schema/package upgrade, then verify and reuse the existing services. |
| **Replace after backup** | Stop the verified writer, create and verify a dated backup, then atomically replace the installation database and runtime state with a new empty installation. |
| **Remove and start clean** | Show the exact data-root path and require a second destructive confirmation; stop the verified writer, remove only the detected EP data root, then create a new empty installation. No repository, project Inbox or external account is removed. |

The app never preselects a destructive choice. If existing-state inspection,
lock acquisition, active-writer shutdown or backup verification fails, it shows
the failure and makes no data-root mutation. Uninstall uses the same three-way
decision: retain data, make a verified backup and remove, or explicitly remove
the exact EP data root. It must never silently erase execution evidence.

##### Installer verification and repair contract

`engineering-platform-host --verify` is a read-only, token-free command. It
emits structured check records with `id`, `outcome`, `summary`, `repair_kind`
and optional `repair_url`; the native app renders those records in all five
supported languages. It verifies the singleton marker/lock, installed EP
command and version, data-root ownership and permissions, SQLite integrity and
schema, dashboard/watcher services, one-writer ownership, watcher ready record
and resolved Inbox path, plus Codex and GitHub CLI availability/readiness.

Each failed check is classified before the UI offers a button:

- **EP-managed repair**: the installer can safely perform it itself (package
  repair, data-root creation, database initialization, supported CLI install,
  or LaunchAgent installation). The app explains the scope and asks for
  confirmation, including administrator permission when macOS requires it;
  exactly one repair/install runs at a time, followed by `--verify`.
- **Operator action required**: the installer cannot safely repair it (for
  example internet access, an unavailable approved package source, an Apple
  system requirement, or provider browser sign-in). The app leaves the
  installation non-admitting, gives a concise reason and offers only an
  official external help/install link. It never displays tokens, command
  output containing credentials, or a fake success state.

The approved external destinations are the official Codex CLI installation
documentation (`https://developers.openai.com/codex/cli/`) and GitHub CLI
installation documentation (`https://cli.github.com/`). Provider browser login
is never part of `--verify` or automatic repair: it remains a separate,
operator-triggered action after the host and its services are verified.

The first-run Console guides the operator through explicit Codex and GitHub
browser login. It stores no token in browser state, dashboard payloads, logs
or the project repository. A missing CLI, failed install, failed service
verification or failed provider login leaves the installation in a clearly
diagnosed repair state: it does not claim Inbox work, mutate a consumer
repository or consume Codex credits. The installer is idempotent; an upgrade
backs up the installation database before a migration, and uninstall offers a
separate, explicit retained-data/backup decision rather than silently deleting
execution evidence.

EP ships its own generic `engineering-platform-host --install`, `--verify`
and explicit `--repair` commands. `--install` is the single, idempotent
installation engine used by both the native application and documented
administrator setup; it returns structured, token-free progress/result
evidence that the app renders. `--verify` and `--repair` verify or repair the
installed application, commands, services, data root, database and token-free
provider readiness. None recreates DJConnect's Apple-signing, Home Assistant
lab, device or other product-development requirements. Browser login remains a
separate explicit Console action after installation; the command never starts
an automatic login or retry loop. The normal per-execution Host, Workspace and
Capability Preflights remain the second gate; an apparently healthy
installation is never authority to admit unsafe work.

#### Registering a project after installation

The installer creates an empty EP installation, not an inferred project. The
initial Console therefore presents a guided **Connect project** flow for both
new and existing Git repositories:

1. the Workspace consumer supplies its immutable canonical `project_id` and
   current display name; EP never derives either from a path, repository name
   or remote;
2. the operator selects the local Git checkout; EP resolves it, verifies it is
   an allowed workspace and records the registration without copying EP source
   or an EP database into that repository;
3. the operator chooses or accepts the project-specific Inbox root created
   below the installation data root; EP proves that its `Inbox` directory is
   writable and empty before activation;
4. EP verifies the requested execution mode. A Managed project additionally
   proves its remote, upstream, clean-worktree and GitHub repository-access
   requirements before it may accept work; and
5. EP starts the isolated project queue only after the registration, Inbox
   route and watcher ready record agree. The Console then shows the project as
   connected.

For a **new repository**, create the repository and its intended remote first,
then register it through this flow before submitting the first Engineering
Action. For an **existing repository**, registration is non-destructive: it
never copies, deletes or relocates repository files, creates a branch, or
imports legacy records without the separate audited migration procedure.

#### Consumer and CI wheel integration

Consumers integrate EP through the versioned Local Consumer API and a pinned,
immutable EP wheel. They must not vendor EP source, invoke the native installer
from CI, write the installation SQLite database or construct EP data-root
paths. A consumer change carries:

- the exact wheel version, checksum/provenance reference and supported
  Consumer Contract version;
- a thin adapter that registers/refreshes explicit canonical project identity
  and calls the scoped API using its own OS-stored credential;
- CI that installs the pinned wheel into an ephemeral environment and runs
  contract/adapter compatibility tests against an ephemeral EP store; and
- a separate release check that rejects an unpinned wheel, incompatible
  contract, missing project scope or direct database/filesystem coupling.

Consumer CI never attempts an interactive Codex or GitHub login, starts a
LaunchAgent, accesses a developer's installation database or spends provider
credits. Repository-local validation remains the responsibility of a real EP
execution after project registration; CI proves only the consumer integration
and contract boundary.

### Phase 4 — Consumer cutover and local upgrade

**Purpose:** move consumers to the wheel without a big-bang transition.

1. Add a thin DJConnect consumer adapter that installs the exact immutable EP
   wheel and supplies only the consumer contract inputs.
2. Add separate Forge and Workspace adapters with independent credentials,
   canonical project registration and project-name refresh. Forge owns planning
   and may submit Producer contracts; Workspace uses its BFF for EP read/action
   API access. Neither writes EP SQLite directly, and Workspace does not
   structurally shell out to the CLI.
3. Add an explicit clean local activation command that:

   ```text
   validates pinned wheel and consumer compatibility
   → creates a clean installation-owned CENTRAL database
   → newly provisions consumer/host credentials
   → registers the canonical project afresh
   → starts exactly one standalone writer
   → validates health, project routing and the first governed execution
   ```

4. Trial clean activation in a disposable clone, then on the first attached
   DJConnect workspace before declaring it supported.
5. Keep legacy suspended and historical only. Never operate dual database
   writers or migrate its database, registrations, queues, executions or
   credentials for MVP.
6. When Forge supplies future dependency metadata, EP validates bounded
   references and the required execution condition without discovering
   dependencies, creating a planning graph, scheduling prerequisites,
   reordering Missions or mutating Forge dependency truth.

The clean-activation UI uses the same native Installer and Connect-project flow
as a fresh installation. It must show the selected canonical project identity,
new installation identity, clean-database result, fresh credential/agent
registration result, service activation result and final watcher resolved Inbox
root. It must stop rather than guess when project identity, repository
evidence, agent pairing or writer ownership is ambiguous.

**Exit evidence**

- clean-install, fresh-registration and idempotency tests;
- a real first-project activation report with independent historical-evidence
  retention proof;
- package-only DJConnect and Forge/Workspace CI jobs; and
- dashboard proof of project selection, project isolation and legacy-history
  continuity.

### Phase 5 — Source retirement and release closure

**Purpose:** remove the in-repository EP implementation only after the
packaged path is proven.

1. Remove the obsolete EP implementation from DJConnect only after the
   package, CI, release, clean activation and `STANDALONE_EP_VERIFIED` satisfy
   their exit evidence.
2. Retain migration guides, consumer adapters, pinned version metadata and a
   bounded compatibility/rollback window.
3. Update DJConnect bootstrap, operational docs, CI and launch-service
   guidance to refer solely to installed EP commands.
4. Publish final extraction evidence and mark the legacy source boundary
   retired.

**Exit evidence**

- no EP runtime source remains in DJConnect;
- no DJConnect or Forge/Workspace job imports EP source directly;
- release artifacts are pinned, traceable and independently reproducible; and
- a final architect/maintainer sign-off confirms ownership boundaries.

## Sequencing and safety gates

### Migration-to-V1 dependency authority

This section is the canonical EP dependency authority from the current
Phase-3/Phase-P frontier through the two Phase-4 producer capabilities.  The
Phase-P roadmap supplies the implementation descriptions and the migration-gap
register supplies completion evidence; neither may introduce a conflicting
ordering.  Each node below is EP-owned.  These are EP-internal build and
qualification edges, not Forge or Workspace work allocations.

`EP::STANDALONE_EP_VERIFIED` is recorded by its B9 activation qualification.
It is not a shorthand that permits an unspecified migration prerequisite.

| Node ID | Depends on | Provides / qualification | Current status | V1 classification |
| --- | --- | --- | --- | --- |
| `EP::PHASE3_STANDALONE_PACKAGE_AND_INSTALL_QUALIFICATION_V1` | Phase-0/1 completed boundary and the clean-slate Phase-2 decision | History-preserving extraction; clean package/import; schema-41 clean store; Server and minimum Agent installation/service qualification; B8C/B8D evidence | AUTHORIZED / incomplete | REQUIRED_BEFORE_STANDALONE_VERIFIED |
| `EP::P_TRANSPORT_V1` | Retained P-CENTRAL-CORE and P-CENTRAL-CONSOLE repairs | File, CLI and HTTP normalize into CENTRAL; watcher owns no lifecycle truth | ACTIVE GAP | REQUIRED_BEFORE_STANDALONE_VERIFIED |
| `EP::P_QUEUE_V1` | `EP::P_TRANSPORT_V1` | Project-scoped FIFO, lease/recovery/finalization and isolation evidence | QUALIFICATION REMAINS | REQUIRED_BEFORE_STANDALONE_VERIFIED |
| `EP::P_NEUTRAL_V1` | `EP::P_QUEUE_V1` | No active DJConnect runtime identity, local authority or obsolete entrypoint | ACTIVE GAP | REQUIRED_BEFORE_STANDALONE_VERIFIED |
| `EP::P_INSTALLER_V1` | `EP::PHASE3_STANDALONE_PACKAGE_AND_INSTALL_QUALIFICATION_V1`; `EP::P_NEUTRAL_V1` | Idempotent verified installation, service, repair and clean activation path | PLANNED / incomplete | REQUIRED_BEFORE_STANDALONE_VERIFIED |
| `EP::P_RELEASE_V1` | `EP::PHASE3_STANDALONE_PACKAGE_AND_INSTALL_QUALIFICATION_V1`; `EP::P_INSTALLER_V1` | Pinned signed release, provenance, compatibility and rollback evidence | ACTIVE GAP | REQUIRED_BEFORE_STANDALONE_VERIFIED |
| `EP::PHASE_P_REAUDIT_V1` | `EP::P_TRANSPORT_V1`; `EP::P_QUEUE_V1`; `EP::P_NEUTRAL_V1`; `EP::P_RELEASE_V1` | Zero active migration gaps or explicit approved retirement | PLANNED / incomplete | REQUIRED_BEFORE_STANDALONE_VERIFIED |
| `EP::PD_INSTALLED_PRODUCT_GOLDENS_V1` | `EP::PHASE_P_REAUDIT_V1`; `EP::P_INSTALLER_V1` | Installed Managed, Genesis and armed-retry Golden evidence | BLOCKED_BY `EP::PHASE_P_REAUDIT_V1` | REQUIRED_BEFORE_STANDALONE_VERIFIED |
| `EP::PHASE_S_EXECUTION_PROTOCOL_FOUNDATION_V1` | `EP::PD_INSTALLED_PRODUCT_GOLDENS_V1` | Minimum CENTRAL-to-Project-Agent execution protocol and parity evidence needed for one governed execution | BLOCKED_BY `EP::PD_INSTALLED_PRODUCT_GOLDENS_V1` | REQUIRED_BEFORE_STANDALONE_VERIFIED |
| `EP::B8E_ZERO_LOSS_PASS` | `EP::PHASE_P_REAUDIT_V1`; `EP::PD_INSTALLED_PRODUCT_GOLDENS_V1`; `EP::PHASE_S_EXECUTION_PROTOCOL_FOUNDATION_V1` | Audited zero-loss disposition and installed-runtime evidence for each claimed live capability | `B8E_REPAIR_PLAN_REQUIRED` | REQUIRED_BEFORE_STANDALONE_VERIFIED |
| `EP::STANDALONE_EP_VERIFIED` | `EP::PHASE3_STANDALONE_PACKAGE_AND_INSTALL_QUALIFICATION_V1`; `EP::P_TRANSPORT_V1`; `EP::P_QUEUE_V1`; `EP::P_NEUTRAL_V1`; `EP::P_INSTALLER_V1`; `EP::P_RELEASE_V1`; `EP::PHASE_P_REAUDIT_V1`; `EP::PD_INSTALLED_PRODUCT_GOLDENS_V1`; `EP::PHASE_S_EXECUTION_PROTOCOL_FOUNDATION_V1`; `EP::B8E_ZERO_LOSS_PASS` | B9: installed EP Server, newly registered Project Agent, first attached project and first governed execution; then clean CENTRAL activation | PLANNED / unavailable | V1 producer gate |
| `EP::SOURCE_RETIREMENT_DJCONNECT_V1` | `EP::STANDALONE_EP_VERIFIED` | Retire obsolete DJConnect EP source/entrypoints while preserving historical evidence | POST-STANDALONE | REQUIRED_AFTER_STANDALONE_VERIFIED / NOT_V1_BLOCKING |
| `EP::PHASE_S_REAL_PROJECT_DOGFOODING_V1` | `EP::STANDALONE_EP_VERIFIED`; `EP::PHASE_S_EXECUTION_PROTOCOL_FOUNDATION_V1` | Real-project Agent configuration, multi-project dogfooding and advanced Agent qualification | POST-STANDALONE | POST_V1 / NOT_V1_BLOCKING |

The minimum Phase-S foundation is intentionally narrower than the later
real-project Agent programme.  It supplies the protocol necessary to qualify
the first governed execution; it does not require the broader dogfooding
programme to complete before B9.

The resulting mandatory order is:

```text
Phase-3 package/install + Phase-P prerequisites
  -> Phase-P re-audit and P-D Goldens
  -> minimum Phase-S execution protocol + B8E zero-loss pass
  -> B9 / EP::STANDALONE_EP_VERIFIED
  -> EP Phase-4 producer capabilities
  -> source retirement and broader Phase-S dogfooding
```

`CUTOVER-DJCONNECT` is therefore not a pre-B9 runtime-authority transition.
The runtime-authority cutover is the B9 recording and clean CENTRAL activation.
The former compound label is renamed here as
`EP::SOURCE_RETIREMENT_DJCONNECT_V1`: a post-standalone source/entrypoint
retirement activity.  It must never precede the standalone capability that
replaces it.

### B8E zero-loss gate (pre-B9)

Before B9, the capability-level audit in
[STANDALONE_ZERO_LOSS_CAPABILITY_AUDIT.md](../engineering/STANDALONE_ZERO_LOSS_CAPABILITY_AUDIT.md)
must be `B8E_ZERO_LOSS_PASS`: every historical responsibility maps to one
disposition, `UNRESOLVED=0`, every gap has severity/owner, and every claimed
live capability has installed-runtime evidence. Extraction or retirement of a
LaunchAgent alone is not semantic retirement.

The recovery order is mandatory: **Phase P — extracted standalone functional
parity** restores the installed extracted product before any redesign or
distributed decomposition. Only after `FULL_EXTRACTED_EP_CORE_VERIFIED` may
the minimum **Phase-S execution-protocol foundation** introduce the front
ingress and CENTRAL-to-Project-Agent physical-execution seams needed for B9,
each with parity evidence. Broader real-project Agent dogfooding is
post-standalone work as defined by the migration-to-V1 dependency authority
above.

```text
B8C_PASS + B8D_PASS + B8E_ZERO_LOSS_PASS + execution protocol ready -> B9
```

The current B8E result is `B8E_REPAIR_PLAN_REQUIRED`; B9 is not open.

| Gate | Required before proceeding |
| --- | --- |
| Contract gate | Versioned consumer API, project identity semantics and error/redaction rules approved. |
| Data gate | Clean standalone schema-41 bootstrap, backup/restore and no-dual-writer proof pass; current LEGACY/CENTRAL archive is not a seed. |
| Package gate | A clean-environment, production-release-mode wheel passes install/smoke checks; its allowlisted contents exclude tests, debug/development assets and development-only dependencies; dedicated EP CI, required security gates and supply-chain evidence pass. |
| Activation gate | Standalone EP Server, newly registered Project Agent, DJConnect as first attached project, and the first governed execution qualify; only then is `STANDALONE_EP_VERIFIED` recorded and clean CENTRAL activated. |
| Consumer gate | DJConnect and Forge/Workspace use only the pinned wheel and complete fresh registration with newly provisioned credentials. |
| Retirement gate | `STANDALONE_EP_VERIFIED`, clean activation and source-retirement evidence are proven; only then is obsolete EP implementation cleanup in DJConnect safe. |

No phase is authorized merely because code compiles. Each gate requires the
listed operational evidence and review approval.

## Architect decisions requested

The following decisions are accepted by ADR-0020: HTTP + versioned JSON is the
canonical Local Consumer API contract; exposure remains fail-closed and
configuration-controlled; Unix sockets are not the public contract; EP owns
per-consumer/project bearer-credential authority; and consumers use OS-native
secret storage (Apple Keychain on macOS). Increment 1 is explicitly
contract-only.

Remaining later-phase decisions are:

1. Confirm the history-extraction method and exact path manifest for the new
   repository.
2. Confirm the target per-user data-root conventions for macOS and the
   portability contract for other operating systems.
3. Confirm the fresh schema-41 bootstrap and legitimate clean schema-40-to-41
   compatibility decision; current contaminated CENTRAL is excluded.
4. Confirm the installation scheduler policy separately from per-project FIFO
   semantics, and confirm that EP validates future Forge dependency metadata
   without deriving or scheduling dependencies itself.

## Explicitly out of scope for this plan

- Rewriting DJConnect history or a big-bang repository move.
- Moving Workspace planning state or its database into EP.
- A cloud datastore or a global queue across unrelated projects.
- Multiple EP writers for one installation database.
- Replacing either the physical Inbox transport or the Workspace API route.
- Adding Forge planning concepts to EP.

## Success definition

EP is successfully extracted when a new local installation can install a
pinned wheel, register multiple Workspace projects, route each project to its
own Inbox and queue, retain and query all EP evidence by canonical
`project_id`, recover safely from a tested backup, and run independently of
DJConnect source code. DJConnect and Forge/Workspace then remain thin,
contract-compatible consumers of the released product.
