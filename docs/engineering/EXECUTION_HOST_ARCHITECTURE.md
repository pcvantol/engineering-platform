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
