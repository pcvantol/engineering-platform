# Extracted functional-parity composition

For Phase P only, schema 44 supplies an installation-private local execution
repository binding.  P-C may call
`resolve_execution_repository(connection, project_id, repository_id)` to get a
validated canonical local root.  An unbound or invalid mapping fails closed.

This lookup is not execution eligibility: later Phase-P work still owns
repository/worktree safety, toolchain/provider readiness, and leases.  It does
not dispatch to Project Agent.

Authority boundaries are deliberately distinct: `repository.json` is portable
logical declaration; CENTRAL topology is logical installation registration;
Agent attachment is Agent-mediated physical availability; the local binding is
the Server-private same-host Phase-P path mapping.  Phase S distributed Project
Agent execution does not depend on this table.

Post-merge runbook: install the governed schema-44 artifact; quiesce/restart
the canonical Server lifecycle; migrate schema 43 to 44; verify identity,
topology, trust, submissions, and zero bindings; explicitly bind required
checkouts; verify no execution occurred; then resume P-C.
