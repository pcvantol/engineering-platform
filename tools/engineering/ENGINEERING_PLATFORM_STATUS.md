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
- Strict sequential Inbox safety: a `BLOCKED` or `FAILED` predecessor holds
  later submissions at `WAITING_FOR_PREDECESSOR` until an explicit
  `Retry-Of` replacement completes. This is the safe default until a future
  Engineering Intent dependency model can express finer-grained ordering.
- iCloud Drive is submission transport only. Claimed prompts, immutable
  execution copies, status, reports, diagnostics and component logs are
  canonical local `.djconnect/` evidence.
- Advisory Engineering Memory.
- Capability-aware generic reviewers and product capability specialists.
- Deterministic Engineering Qualification and local evidence reports.
- Provider-neutral runtime, repository, service, submission and private-access
  configuration, with Codex CLI, GitHub, launchd, iCloud Inbox and Tailscale as
  current configured providers.
- The private dashboard is read-only and binds only to loopback plus the
  locally reported Tailscale IPv4 address. It never binds a wildcard, LAN or
  public address, and it does not configure Tailnet ACLs, Funnel, port
  forwarding or network policy.
- Watcher and dashboard application logs are structured, bounded, rotated and
  redacted before persistence. The dashboard shows a log tail only after an
  explicit maintainer action.
- The private dashboard's Codex advice surface is separately bounded to a
  read-only, ephemeral CLI process with context from the repository, matching
  terminal prompt and Engineering Report. It cannot start engineering or
  mutate repository, lifecycle, release or deployment state.

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
