# Managed Codex runtime lifecycle

Phase P preserves the historical local execution ownership model. The installed
Engineering Platform Server host owns the user-scoped managed Codex CLI at
`~/.local/share/engineering-platform/codex-cli/bin/codex`. It observes that
exact path at startup and exposes runtime capability separately from CENTRAL
health. A missing or broken runtime leaves CENTRAL and the Operations Console
available; it is repaired only by the existing, explicit Console provider
Install action.

The preserved repair flow resolves the published `@openai/codex` version,
installs that exact version with npm's managed prefix support, and verifies the
managed launcher before readiness can report it available. The historical
Console update action retains its version-pinned update behavior. Authentication
remains a separate explicit provider-login step.

Phase S must move physical runtime presence, provisioning, updates, readiness,
and provider invocation to the Project Agent. CENTRAL remains the policy and
status authority, and the Operations Console remains the operator surface. The
future flow is Console -> CENTRAL bounded runtime-management intent -> Project
Agent -> capability/status report -> CENTRAL -> Console. This Phase-P change
does not alter the Agent protocol or grant it runtime-install or execution
authority.
