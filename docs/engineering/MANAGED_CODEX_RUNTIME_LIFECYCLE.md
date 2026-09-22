# Managed Codex runtime lifecycle

Phase P's historical per-user Server owns the user-scoped managed Codex CLI at
`~/.local/share/engineering-platform/codex-cli/bin/codex`. That location
remains legacy compatibility only.

A system-domain Server instance instead owns its Codex runtime, `CODEX_HOME`,
auth state and non-secret bootstrap receipt below that instance's provider
root. Its LaunchDaemon carries the exact managed prefix and never depends on an
interactive user's HOME, PATH or GUI login. Executable substitution or missing
auth evidence fails readiness closed. See
[EP Server system-domain multi-instance runtime and provisioner v1](EP_SERVER_SYSTEM_MULTI_INSTANCE_V1.md).

The preserved repair flow resolves the published `@openai/codex` version,
installs that exact version with npm's managed prefix support, and verifies the
managed launcher before readiness can report it available. The historical
Console update action retains its version-pinned update behavior. Authentication
remains a separate explicit provider-login step.

Project Agent provider state remains independently user-owned per Host/OS-user.
An Agent never borrows a Server instance's Codex or GitHub context. Moving
project-execution provider work to that Agent does not change the Server's
need for its own cold-boot-capable provider context.
