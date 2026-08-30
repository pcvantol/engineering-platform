# Execution Host architecture

The Execution Host owns the Engineering lifecycle, checkpoints and terminal
evidence. It does not own platform process execution or dashboard projection.

- `execution_host.py` coordinates a single bounded lifecycle.
- `execution_transaction.py` owns transaction-scoped identity and lease context.
- `execution_repository.py` owns repository and GitHub coordination through the
  canonical Git and GitHub providers.
- `execution_finalization.py` owns bounded repository-cleanup sequencing.
- `execution_reporting.py` owns terminal-report validation and delivery.
- `execution_executor.py` owns Codex invocation, result normalization and
  prompt-free live activity projection through `CodexCliProvider`.
- `reviewer_evidence.py` owns the bounded run-scoped repository fact
  projection used by one Managed reviewer wave.
- `execution_models.py` and `execution_evidence.py` own shared execution and
  terminal-evidence value types.
- `execution_lease.py` owns SQLite-backed active-run ownership and liveness.
- `execution_readiness.py` selects and evaluates one typed readiness profile.
- `providers.py` is the process/platform boundary. Codex, local Git, launchd,
  iCloud transport and Tailscale interactions are implemented there; lifecycle
  code consumes provider methods.
- `provider_readiness.py` is the shared token-free admission and dashboard
  projection boundary for Codex and GitHub availability/session readiness.

Readiness has three explicit profiles: platform host, Managed repository and
Genesis target. A Genesis run only evaluates its target profile; a Managed run
only evaluates its repository profile. JSON status files are projections, not
an ownership or lifecycle authority.

Lifecycle phase identifiers are compatibility contracts. Their presentation is
mode-aware: the shared `REPAIR_AGENT` phase is projected as pull-request check
repair for Managed work and autonomous quality repair for Genesis. This is a
display-only distinction; no checkpoint or transaction state is translated.

The immutable profile lists repository, remote, upstream, clean-worktree,
branch, workspace authorization, host and capability qualification, providers,
datastore, active-lease and Producer Contract requirements. Facts are observed
separately; an unknown required fact blocks admission.

Each admitted run persists a typed readiness decision in the canonical
datastore. The policy defines requirements; preflight and providers acquire
facts; the Execution Host only responds to the resulting decision.

The provider inventory is intentionally narrow: `CodexCliProvider` owns Codex
CLI invocation, `GitProvider` local Git, `GitHubProvider` GitHub CLI,
`LaunchdProvider` service control, `ICloudProvider` Inbox transport facts and
`TailscaleProvider` network identity. `LocalProcessProvider` is the sole
generic process implementation. Core orchestration consumes their typed
results and owns neither process invocation nor lifecycle policy.

Lease reconciliation is datastore-only. A stale lifecycle checkpoint is
projected separately as `STALE`, never as an active run; the Inbox watcher only
gates later work on a valid live lease after a transaction exists. Recovery
continues through the existing Resume/Retry/Dismiss semantics and retains the
previous lease history.

## Execution scope, Workers and deployment

EP serializes mutating work at the repository/execution-scope boundary: no more
than one mutating execution may own a scope at once. FIFO is the default queue
ordering within that scope, but admission and selection remain policy-driven;
FIFO is not a second planning authority. The active mutation lease starts with
the accepted execution and is retained through provider work, validation,
delivery, finalization and reconciliation. It is released only after terminal
or governed recovery evidence establishes that the scope is safe for later
work.

Workers are capability-based, replaceable execution resources. A Worker may
provide Codex, Git, tests, builds or browser validation, but it never owns the
execution lifecycle or canonical evidence. EP dispatches only under its
admission, lease and provider policy; a disconnected Worker is recoverable
from EP's durable execution state and fresh repository/GitHub evidence.

In the initial home profile, Forge, EP and a primary Worker can be co-located
on an always-on Mac mini. This is deployment topology only: Forge and EP remain
separate authorities, Workspace remains a remote-capable human client, and a
later Worker may run elsewhere. External NVMe may hold reconstructable
execution data such as checkouts, worktrees, caches, builds and artifacts;
canonical EP control-plane storage must remain durable and independent of that
execution volume. Instance identity plus mDNS and configured Tailscale
endpoints support discovery and reachability. Tailscale is transport, not
authentication or authorization.

## Reviewer repository evidence

After the Managed Execution Host synchronizes the repository while holding
the run lease, it creates one content-minimal `ReviewerEvidence` projection
for that exact Run ID. It separates facts by freshness:

- **RUN-STABLE:** repository identity and execution mode;
- **MUTABLE:** branch, HEAD, worktree classification and `main` ancestry;
- **BOUNDARY-SENSITIVE:** the post-synchronization/pre-reviewer-wave boundary
  and its invalidators.

The same facts are supplied to each independent reviewer and the first primary
provider invocation. Reviewers keep independent reasoning and recommendations;
their conclusions are not merged into another reviewer or the primary provider
context. The projection contains no command output, diff content or
conclusions. It is valid only until repository mutation, validation,
pull-request mutation, merge, finalization or cleanup. A later reviewer or
resumed execution creates a fresh projection rather than reusing a mutable
observation. Git/GitHub detail remains available through narrow on-demand
retrieval when a review actually needs it. Genesis runs receive no Managed
checkout projection.

## Provider interruption recovery boundary

A provider-proven interruption is recovered only within the existing run. The
host persists the interruption before it considers a replacement invocation,
then consumes at most one automatic recovery attempt for that run, branch and
worktree. The replacement receives a distinct invocation identity; it is not a
retry submission, a new root run or permission to repeat completed delivery
steps. A second interruption or any ambiguous recovery evidence fails closed
for operator attention. Execution receipts retain each provider attempt
separately, while validation and delivery evidence remain attributable to their
own lifecycle activities.

A recovered record is phase-scoped historical evidence: it may be retained for
reporting, but it can be consumed only by its recorded lifecycle phase and
cannot satisfy or interfere with a later provider phase.
