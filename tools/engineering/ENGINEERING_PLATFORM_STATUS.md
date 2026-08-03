# Engineering Platform Status

**Engineering Platform:** Generation 2
**Status:** `PRODUCTIZATION_IN_PROGRESS`

## Closure

Engineering Platform 1.5 productizes the stable Generation 1 foundation while
remaining inside this repository. Platform Identity is independent; Workspace
Identity supplies consumer branding, repository metadata and active providers.
The Public Platform API and provider registry are the supported extension
boundary. Existing commands remain compatibility interfaces.

## Stable capability set

- Autonomous lifecycle and validation ownership.
- Bounded repair loop, repository reconciliation and repository cleanup.
- Owner authorization and automatic PR/Finalization lifecycle.
- Progress reporting and post-run Engineering Reports, with initial reviewer
  observations explicitly separated from the checkpoint-authoritative final
  repository outcome; see `docs/engineering/ENGINEERING_REPORTING.md`.
- Codex CLI invocations request JSONL events so a run's explicitly reported
  token usage is captured only for that exact run. Costs and plan allowance are
  shown only when the CLI supplies them.
- Terminal reports preserve report-bound runtime provenance: Runtime Provider,
  actual AI Model, reported Reasoning and Configuration Profiles, and Codex CLI
  Version. The dashboard presents those values only for the matching completed
  run and never infers unavailable values.
- Strict sequential Inbox safety: a `BLOCKED` or `FAILED` predecessor holds
  later submissions at `WAITING_FOR_PREDECESSOR` until **Resume Queue** creates
  a corrective `Retry-Of` replacement. **Retry Execution** remains separately
  available for every terminal `BLOCKED` run and records immutable lineage.
  This is the safe default until a future
  Engineering Intent dependency model can express finer-grained ordering.
- Execution Host Preflight Levels 1 and 2 run before every Inbox claim. Level 1 validates
  host-only configuration, runtime directories, configurable disk capacity,
  Codex CLI availability, telemetry storage, structured logging and host
  identity. Level 2 validates only the resolved workspace: approved root, Git
  metadata, a clean worktree, no unfinished Git operation and mode-aware branch
  readiness. A failed check preserves the Inbox item and prevents an engineering
  run; compact evidence is retained locally and included in the report.
- iCloud Drive is submission transport only. Claimed prompts, immutable
  execution copies, status, reports, diagnostics and component logs are
  canonical local `.engineering/` evidence.
- Advisory Engineering Memory.
- Capability-aware generic reviewers and product capability specialists.
- Deterministic Engineering Qualification and local evidence reports.
- Provider-neutral runtime, repository, service, submission and private-access
  configuration, with Codex CLI, GitHub, launchd, iCloud Inbox and Tailscale as
  current configured providers.
- The private dashboard is status- and evidence-first for Engineering lifecycle
  activity and
  binds only to loopback plus the locally reported Tailscale IPv4 address. It
  never binds a wildcard, LAN or public address, and it does not configure
  Tailnet ACLs, Funnel, port forwarding or network policy. Its sole bounded
  account-side action is consuming one available Codex resetcredit after an
  explicit maintainer confirmation. Its only local service action is a
  confirmed restart of a fixed, owned dashboard, watcher or relay LaunchAgent;
  it cannot affect Inbox work, repository, lifecycle, release or deployment
  state.
- Watcher and dashboard application logs are structured, bounded, rotated and
  redacted before persistence. The dashboard automatically refreshes a bounded
  log tail only when its server-pushed revision changes.
- Terminal runs are indexed in local SQLite prompt history with their status,
  title, completed timestamp, available commit and delivered-report reference.
  The private dashboard renders this evidence projection as a searchable,
  sortable, paginated Promptgeschiedenis table; reports remain downloadable
  only when local delivery succeeded.
- The private dashboard's Codex advice surface is separately bounded to a
  read-only, ephemeral CLI process with context from the repository, matching
  terminal prompt and Engineering Report. It cannot start engineering or
  mutate repository, lifecycle, release or deployment state.
- Dashboard presentation is provider-neutral while preserving explicit
  per-run provenance (for example, `AI-provider: Codex CLI`). It offers
  server-pushed status, category-coded evidence cards, client-side structured
  log filtering/sorting and browser-session-local read-only advice history.
- Engineering Storage schema `5` is versioned and fail-closed in the platform
  manifest. `.engineering/engineering.db` and the surrounding `.engineering/`
  workspace are canonical; a verified, fail-closed legacy migration preserves
  prior `.djconnect/` evidence before the legacy directory is removed.

## Future governance

Future Engineering Platform work is classified as Maintenance, Bug Fix,
Compatibility, Qualification Improvement, Evidence-driven Enhancement or
Architecture Revision. Only Evidence-driven Enhancement and Architecture
Revision may introduce a capability, and both require explicit architectural
approval.

Every future capability requires Implementation, Qualification and Evidence.
It is not complete before qualification. Continuous improvement may address
qualification, diagnostics, compatibility and maintenance; it must not
continuously expand the feature set.

The next bounded enhancement is **Execution Host Preflight Level 3 (Capability
Preflight)**: let Forge capabilities contribute workspace-specific readiness
checks without changing the generic Execution Host. It must not broaden Levels
1 or 2 into mission or action validation.

Engineering Memory remains advisory and may improve recommendations only.
Repository evidence remains authoritative and Memory never autonomously changes
engineering behavior. Capability reviewers are stable architecture; a future
reviewer requires evidence that the current set has insufficient coverage.

Engineering Platform versioning is mandatory. Breaking engineering-contract
changes require a new major version; compatible improvements follow semantic
versioning.

## Bootstrap compatibility

The repository bootstrap is the authoritative compatibility contract. Future
Platform Engineering prompts require Engineering Platform `>= 1.5.0`; older
versions are incompatible and must fail closed with an upgrade-required
diagnostic. This records a documentation and compatibility requirement only.
