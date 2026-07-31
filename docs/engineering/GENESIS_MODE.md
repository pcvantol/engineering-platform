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

Managed transactions remain the default. Their repository synchronization,
GitHub pull-request, validation, merge, finalization and cleanup lifecycle is
unchanged.
