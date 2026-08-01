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
