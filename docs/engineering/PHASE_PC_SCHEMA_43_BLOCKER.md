# Phase P-C schema-43 local-root blocker

Schema 43 deliberately registered logical projects, repositories, and path-free
Agent attachments, but supplied no Server-owned durable local checkout root.
That was insufficient for the temporary standalone Phase-P local lifecycle.

Schema 44 resolves the blocker with `ep_local_repository_bindings`: an
installation-private, explicit `(project_id, repository_id) -> local_root`
mapping.  It is empty after migration and never inferred from CWD, Git, Agent
storage, or repository naming.  The path is validated against the canonical
repository declaration and is not returned by Consumer or Operations APIs.

Agent attachment remains Agent-mediated physical availability; a local binding
is this Server host's local Phase-P mapping.  Neither grants authority to the
other.
