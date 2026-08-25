# Engineering Platform 2.x extraction and migration plan

**Status:** Proposed for Platform Architect review
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
4. Confirm public command compatibility: neutral commands stay neutral, and
   consumer-specific commands are either adapters or explicitly retired under a
   migration notice.

**Exit evidence**

- repeatable file/import audit with no Home Assistant, DJConnect runtime or
  repository-name dependency in EP product code;
- a complete export list of source, tests, documentation, workflows and
  qualification assets; and
- an approved baseline tag, source commit, EP version, storage schema, Consumer
  Contract version, qualification evidence and rollback reference.
- public entry-point, runtime-name and filesystem/current-working-directory
  dependency audits.

### Phase 1 — Complete the public contract and service boundary

**Purpose:** make the consumer boundary explicit before moving code.

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
   installation tests; migration/recovery tests; SBOM/provenance/checksum
   evidence; release signing/publication policy; and immutable wheel release.
6. Publish a first pinned 2.x wheel only after package install, clean-machine
   bootstrap and rollback proof succeed.
7. Define compatibility explicitly across EP version, Consumer Contract
   version, storage schema, DJConnect adapter, Forge adapter and Workspace
   adapter/BFF. Unknown or incompatible major versions fail closed; successful
   imports are insufficient proof.

**Exit evidence**

- a reproducible wheel installed into a clean environment without DJConnect
  source;
- dedicated EP CI and release evidence;
- all five locales verified from the installed package; and
- history and license provenance review of the new repository.

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
| Package gate | Installed wheel works from a clean environment with dedicated EP CI and supply-chain evidence. |
| Consumer gate | DJConnect and Forge/Workspace use only the pinned wheel and complete registration. |
| Retirement gate | Supported upgrade, rollback and launch-service cutover are proven; source removal is then safe. |

No phase is authorized merely because code compiles. Each gate requires the
listed operational evidence and review approval.

## Architect decisions requested

1. Confirm loopback HTTP, a local socket, or both as the initial Local Consumer
   API transport behind the application-service boundary.
2. Confirm capability issuance, rotation, project restrictions and OS
   credential-storage policy for DJConnect, Forge and Workspace.
3. Confirm the history-extraction method and exact path manifest for the new
   repository.
4. Confirm the target per-user data-root conventions for macOS and the
   portability contract for other operating systems.
5. Confirm the schema/data compatibility decision record that retires
   LEGACY_ROLLBACK_COMPATIBLE into LEGACY_ROLLBACK_RETIRED.
6. Confirm the installation scheduler policy separately from per-project FIFO
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
