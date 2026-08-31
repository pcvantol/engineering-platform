# Engineering Platform 2.x extraction and migration plan

**Status:** Phase 0 complete; Phase 1 complete / qualified; Phase 2 / Increment 1 complete
**Scope:** Engineering Platform 2.x extraction from `pcvantol/djconnect` to a
standalone, local-first Execution Operations Platform
**Decision baseline:** [ADR-0019](../adr/0019-engineering-platform-central-installation-store.md) and the
[EP consumer contract](ENGINEERING_PLATFORM_CONSUMER_CONTRACT.md)

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

## Authorized Phase 2 increment sequence

| Increment | Canonical name | Authorization |
| --- | --- | --- |
| Phase 2 / Increment 1 | **Installation Data-Root Contract and Central-Store Migration Guardrails** | Complete; documentation/control only; no store moved |
| Phase 2 / Increment 2 | **Central-Store Migration Tooling + Dry-Run Qualification** | Implemented / dry-run qualified; live preflight proves the current schema-40 source but is presently ineligible until the separately authorized admission freeze and service quiescence |
| Phase 2 / Increment 3 | **Controlled Central-Store Cutover** | Not authorized by Increment 1 |
| Phase 2 / Increment 4 | **Post-Cutover Qualification + Rollback-Compatible Baseline** | Not authorized by Increment 1 |

The canonical implementation control is [EP central-store migration guardrails](../engineering/EP_CENTRAL_STORE_MIGRATION_GUARDRAILS.md). Phase 2 completes central-store/project-scope migration before Phase 3 physical extraction/package, Phase 4 consumer cutover, and Phase 5 legacy removal. Phase 3 may begin only after the installation-owned central store and its authority/rollback state are qualified.

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

**Purpose:** build the 2.x data model before consumer cutover.

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
9. Make the legacy-to-central cutover explicit: stop/admission-freeze the
   legacy writer; verify it; back up and verify; register its canonical
   Workspace project; migrate schema and backfill atomically; verify referential
   integrity, Inbox ownership and evidence continuity; disable the legacy
   writer; start one installation writer; then verify health, routing and no
   competing writer.
10. Govern rollback by explicit **LEGACY_ROLLBACK_COMPATIBLE** and
    **LEGACY_ROLLBACK_RETIRED** states. Schema/data compatibility is
    authoritative, with one complete EP minor-release window as the default
    support target unless an earlier evidenced irreversible boundary retires
    legacy startup.

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
an independently releasable package.

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
3. Add an explicit local upgrade command that:

   ```text
   validates pinned-wheel and consumer compatibility
   → backs up legacy state
   → registers/migrates the legacy project into the central store
   → applies in-place database migrations
   → replaces launchd commands with installed EP commands
   → starts exactly one writer
   → validates health, project routing and retained evidence
   ```

4. Trial the upgrade in a disposable clone, then on a representative retained
   local workspace before declaring it supported.
5. Run old and new paths only in a migration-controlled, read-only/disabled
   legacy mode. Never operate dual database writers.
6. When Forge supplies future dependency metadata, EP validates bounded
   references and the required execution condition without discovering
   dependencies, creating a planning graph, scheduling prerequisites,
   reordering Missions or mutating Forge dependency truth.

The local-upgrade UI uses the same native Installer and Connect-project flow
as a fresh installation. It must show the discovered legacy state, selected
canonical project identity, backup location, service cutover result and final
watcher resolved Inbox root. It must stop rather than guess when project
identity, legacy-store cardinality, repository evidence or writer ownership is
ambiguous.

**Exit evidence**

- upgrade, downgrade/restore and idempotency tests;
- a real local upgrade report with recovered backup evidence;
- package-only DJConnect and Forge/Workspace CI jobs; and
- dashboard proof of project selection, project isolation and legacy-history
  continuity.

### Phase 5 — Source retirement and release closure

**Purpose:** remove the in-repository EP implementation only after the
packaged path is proven.

1. Remove `tools/engineering/` source only after the package, CI, release and
   local upgrade paths satisfy their exit evidence.
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

| Gate | Required before proceeding |
| --- | --- |
| Contract gate | Versioned consumer API, project identity semantics and error/redaction rules approved. |
| Data gate | Transactional central-store migration, backup/restore and no-dual-writer proof pass. |
| Package gate | A clean-environment, production-release-mode wheel passes install/smoke checks; its allowlisted contents exclude tests, debug/development assets and development-only dependencies; dedicated EP CI, required security gates and supply-chain evidence pass. |
| Consumer gate | DJConnect and Forge/Workspace use only the pinned wheel and complete registration. |
| Retirement gate | Supported upgrade, rollback and launch-service cutover are proven; source removal is then safe. |

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
3. Confirm the schema/data compatibility decision record that retires
   LEGACY_ROLLBACK_COMPATIBLE into LEGACY_ROLLBACK_RETIRED.
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
