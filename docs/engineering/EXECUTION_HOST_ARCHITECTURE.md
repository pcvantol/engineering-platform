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

Readiness has three explicit profiles: platform host, Managed repository and
Genesis target. A Genesis run only evaluates its target profile; a Managed run
only evaluates its repository profile. JSON status files are projections, not
an ownership or lifecycle authority.

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
