# Engineering Platform Roadmap

## 1.4 — Remote Engineering Experience

Completed. Canonical status, remote dashboard, inbox watcher, Tailscale
diagnostics and repository handoffs remain local, read-only and without
release, deployment or product authority.

## 1.5 — Platform Productization

Completed and operational. The platform is an independent engineering product
located in this repository as an implementation strategy. Platform Identity,
Workspace Identity, provider and capability registries, configuration
validation and the Public Platform API remove architectural dependence on
DJConnect. Existing commands remain compatibility wrappers.

The completed operational hardening keeps iCloud Drive limited to Inbox
transport, stores canonical prompt archives, status, reports and logs under
`.engineering/`, stops a strict Inbox sequence after `BLOCKED` or `FAILED`, and
provides bounded redacted component logs, report analysis and read-only private
Codex advice. Qualification covers 39 registered scenarios. These are
compatible 1.5 maintenance and evidence improvements, not a 1.6 requirement.
The Engineering Platform CI quality gate measures branch coverage for its four
protected core execution files and requires each to remain at least 80.20%; the
authoritative requirement matrix is
[Engineering Platform non-functional requirements](../engineering/ENGINEERING_PLATFORM_NON_FUNCTIONAL_REQUIREMENTS.md).

## 1.6 — Repository Extraction Readiness

Planned. Dependency, namespace, import and public-API audits will demonstrate
that extraction is primarily repository movement, not a redesign.

## 2.0 — Versioned Platform Boundary

In review. Engineering Platform `2.0.0` aligns the platform, runner, Inbox
watcher and private dashboard at one major version and raises the fail-closed
minimum version for new engineering prompts. Storage, protocol and lifecycle
formats remain unchanged.

Standalone packaging, repository templates and a generic CLI remain separate
follow-on work until repository-extraction readiness is qualified. The version
bump alone does not move the platform out of this repository or change
authority.

## 2.x — Standalone Execution Operations Platform

Planned. The 2.x extraction turns Engineering Platform into an installed,
provider- and consumer-neutral local Execution Host. The dashboard is
positioned internally as the **Execution Operations Console**: it presents
host operations and a selected Workspace project, but is never a second source
of lifecycle, planning or repository authority.

**Canonical phase roadmap (ADR-0026):**

1. **Phase 2 — CLOSED / RETIRED CLEAN-SLATE DECISION.** The current
   contaminated migration is `RETIRED_FOR_CLEAN_SLATE_EXTRACTION`; its stores
   are forensic evidence, not standalone authority.
2. **Phase 3 — HISTORY-PRESERVING PHYSICAL EXTRACTION + CLEAN STANDALONE
   STORE.** Extract the proven EP implementation, qualify it independently,
   then create a fresh official schema-41 installation store.
3. **Phase 4 — CONSUMER CUTOVER.** Register consumers afresh and issue new
   OS-secret-stored credentials only after standalone qualification.
4. **Phase 5 — LEGACY RUNTIME REMOVAL.** Remove generic EP runtime from
   DJConnect only after package, store and consumers qualify; forensic archive
   retention is decided separately.

The extraction sequence is deliberately incremental and provenance-preserving:

**Current authorization:** Phase 0 / Increments 1 and 2 and Phase 1 /
Increment 1 — **Local Consumer API Contract Foundation** — are complete.
Phase 1 / Increment 2 (**Local API Transport + Authentication Runtime**) is
implemented and post-merge qualified as a loopback-only, minimal read-only v1
service with EP-owned verifier metadata; schema 39 is active. Increment 3
(**Consumer Registration + OS Credential Integration**) is architecture
authorized by ADR-0022 only: it requires schema 40 for registration authority,
may later add production credential lifecycle and macOS Keychain consumer
storage, and does not authorize consumer cutover or Engineering mutation.

1. **Boundary and consumer contract — architecture complete.** ADR-0019 and the EP consumer
   contract establish one installation-owned store, the canonical
   Workspace-provided `project_id`, and the mutable Workspace-provided
   `project_name` used for display.
2. **Clean data-root and multi-project bootstrap.** Install one EP data root and one
   SQLite database per local user/machine, outside all consumer repositories.
   Register projects before admission; scope every EP-owned Inbox route, queue,
   lease, lifecycle record, receipt, report, Prompt History, telemetry and
   dashboard projection by the immutable `project_id`. A project name is a
   label only and can be refreshed without rewriting historical evidence.
3. **Settings and diagnostics placement.** Keep Inbox routing, Inbox scan
   cadence and open-PR check cadence in the selected project's queue settings.
   Keep log retention, log level and dashboard/component refresh preferences
   installation-wide. Move free disk space, database path, database size and
   schema version into a machine/platform diagnostic block; they do not belong
   to a Workspace project.
4. **Datastore governance and recovery.** Ship forward-only, transactional
   migrations with a pre-migration backup, startup integrity check, explicit
   compatibility gate and documented recovery procedure. Fresh standalone EP
   creates schema 41 from canonical product definitions; a future legitimate
   clean schema-40 upgrade is separate. No current DJConnect database is a
   standalone seed.
5. **Server-side API contracts.** Define typed, bounded host API contracts per
   endpoint: accepted fields and enums, unknown-field rejection, Unicode and
   newline rules, stable error codes and redacted diagnostics. The server stays
   authoritative; browser-side normalization is defense in depth only. The
   read-only AI chat must not allow supplied text to override host configuration,
   repository paths or system context.
6. **Internal service boundaries.** Preserve one local host process unless an
   operational need proves otherwise, while separating the HTTP/API facade,
   application services, status projection, operational controls and datastore
   repositories. The Operations Console remains a thin presentation consumer.
7. **Forge-native host integration.** Forge/Workspace remains the owner of
   planning, Runtime Prompts and the canonical project registry; EP owns
   admission, execution lifecycle, telemetry, evidence, Inbox and Prompt
   History. Retain a fail-closed serial default-FIFO queue per repository/
   execution scope, with at most one mutating execution and a lease retained
   through finalization/reconciliation. Queue selection remains policy-driven;
   it must not make EP a planner. When Forge later supplies `depends_on`, EP
   validates and enforces it without becoming a planner. The physical Inbox
   route and Workspace API route remain parallel admission paths.
8. **Advisory telemetry.** Retain telemetry only as operational observation,
   never as repository or lifecycle evidence. Add median/p50 and p95 views,
   then segment by execution mode, target repository, terminal state and
   model/provider when that metadata is supplied by the consumer contract.
9. **Package, release and consumer cutover.** Extract relevant history into the
   EP repository; publish an immutable pinned wheel with dedicated CI,
   supply-chain evidence and releases. Update DJConnect and Forge/Workspace to
   install that wheel only, provide a local backup/compatibility/migration/
   launch-service upgrade path, and remove `src/engineering_platform` only after the
   packaged paths have been proven.

The central-store and project-scope decision is specified in
[ADR-0019](../adr/0019-engineering-platform-central-installation-store.md).
[ADR-0026](../adr/0026-ep-clean-slate-standalone-store-and-migration-retirement.md)
retires the current contaminated legacy-to-CENTRAL migration in favor of a
clean standalone store. The
concrete registration and ownership boundary is specified in the
[EP consumer contract](ENGINEERING_PLATFORM_CONSUMER_CONTRACT.md). The
phase-level delivery, safety gates and architect review questions are in the
[EP extraction and migration plan](ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md).

## Policy

Platform code must not acquire DJConnect runtime, Home Assistant, branding or
repository-name dependencies. Consumer-specific presentation and metadata
enter through Workspace configuration or qualified providers only.

The planned home deployment of one authoritative Forge installation, one
authoritative EP installation and one primary Worker on a Mac mini is a local
deployment profile only. It is not a product-wide singleton invariant and does
not authorize Phase 0, Increment 2, extraction, storage migration or other
roadmap implementation.
