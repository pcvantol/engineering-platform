# Engineering Platform Genesis Mode

Genesis Mode is the local-only lifecycle for a new, greenfield repository. It
is intentionally separate from the managed Engineering Platform lifecycle.

Activate it only with this exact standalone prompt line:

```text
Execution Mode: Genesis
```

Genesis Mode permits a direct child of the configured Engineering Workspace
Root to be initialized or evolved as a local Git repository. It requires a
clean local commit checkpoint and local reconciliation evidence. It does not
require, create or contact an upstream remote, `origin/main` or a pull request.

The runner resolves the exact execution-mode declaration and the one explicit,
absolute `Target repository:` field before it selects any repository lifecycle
or readiness check. Genesis never falls back to Managed: a missing, malformed,
conflicting or host-repository target is terminally blocked before Codex starts.
Its only workspace preflight checks the local Git target, its writable metadata,
clean worktree, absent lock and active-workspace ownership.

The persisted terminal checkpoint is authoritative. Engineering Reports are
derived evidence and must state the same `COMPLETE`, `BLOCKED` or `FAILED`
outcome; the Inbox watcher rejects contradictory delivery evidence and supplies
a bounded corrected terminal report while preserving the original local report.

Managed transactions remain the default. Their repository synchronization,
GitHub pull-request, validation, merge, finalization and cleanup lifecycle is
unchanged.

## Complete project bootstrap and promotion — planned composition

The [project bootstrap execution design](PROJECT_BOOTSTRAP_EXECUTION_V1.md),
[owning roadmap](../development/PROJECT_BOOTSTRAP_V1_ROADMAP.md) and
[documentary DAG](../development/PROJECT_BOOTSTRAP_V1_DAG.json) specify the
future manifest-driven Genesis/Managed bootstrap and explicit promotion.
They reuse the local-only primitives above, but add planned artifact,
identity/adoption, authority, recovery and qualification contracts. They do
not expand current placement support or silently change the legacy declaration.

Genesis is local engineering, not automatic publication permission. Promotion
requires a separately approved remote/history/settings effect set and actual
Managed-governance readback, with unchanged project identity and ancestry.
New-project readiness never creates or approves a Mission. All added PB-E nodes
remain PLANNED; this documentation changes no runtime, grant or installation.
