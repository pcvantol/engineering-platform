# Engineering Platform 1.5 Migration Report

## Outcome

Engineering Platform 1.5 introduces an independent Platform Identity, a
consumer-owned Workspace Identity, declarative provider selection and a stable
public API and the provider-neutral `engineering-execution-host` command without
changing transaction lifecycle,
watcher transport, dashboard authority or repository governance.

## Configuration hierarchy

`Platform defaults -> Platform configuration -> Workspace configuration ->
Repository configuration -> Local installation configuration` is deterministic.
The checked-in configuration supplies the first three applicable layers;
provider-local state stays outside the repository. Unknown provider categories,
unsupported providers and identity/version disagreement fail closed.

## Compatibility

The existing runner, watcher and dashboard remain compatibility surfaces. The
configured implementations are Codex CLI, GitHub, launchd, iCloud Inbox and
Tailscale. They are described through provider contracts; no provider receives
engineering execution authority merely by being configured.

## Deferred to 1.6

Actual package extraction and additional provider implementations require
extraction-readiness evidence. The generic command rename is complete; the
canonical `engineering-execution-host` command is the supported automation
entry point. The previous `dj-engineer` compatibility alias has been removed.
1.5 repository bootstrap API, idempotent workspace provisioning and generic
configuration template are complete compatibility surfaces.
No functional product, release, deployment or publication behavior changed.

## 2.x extraction target: central installation store and multi-project scope

The package extraction is not a per-repository copy of the current runtime.
Engineering Platform 2.x will install once per local user/machine and own one
central installation database outside consumer repositories. Every EP-owned
operational record is scoped by the canonical Workspace `project_id`, including
Inbox routing, queue, lease, lifecycle, telemetry, Prompt History, Engineering
Reports and Execution Receipts. The dashboard will select an active project and
filter all project data accordingly. Workspace also supplies a mutable,
human-friendly `project_name` for that selector; the immutable `project_id`
remains the only data and queue identity.

Workspace remains the source of truth for project identity and planning state;
EP never creates a competing project identity or writes Workspace planning
state. The extraction upgrade must back up legacy state, register the existing
workspace as a project, migrate records atomically and ensure only the
installation-owned EP process writes the central database.

### Machine/platform diagnostics placement

The 2.0 dashboard already presents the following diagnostics outside the
project-scoped **Workspace** block, in **Configuration**. When the central
installation database is introduced, that interim presentation becomes its
own EP machine/platform block:

- free disk space;
- Engineering database path;
- Engineering database size; and
- database schema version.

They describe the one local EP installation rather than a registered Workspace
project. Project name, workspace/repository location, tracked files, branch
and commit remain project-scoped.

See [ADR-0019](../adr/0019-engineering-platform-central-installation-store.md)
and the [consumer contract](ENGINEERING_PLATFORM_CONSUMER_CONTRACT.md).
