# Engineering Platform 1.5 Migration Report

## Outcome

Engineering Platform 1.5 introduces an independent Platform Identity, a
consumer-owned Workspace Identity, declarative provider selection and a stable
public API without changing the `dj-engineer` command, transaction lifecycle,
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

Actual package extraction, generic command renaming and additional provider
implementations require extraction-readiness evidence. The 1.5 repository
bootstrap API, idempotent workspace provisioning and generic configuration
template are complete compatibility surfaces.
No functional product, release, deployment or publication behavior changed.
