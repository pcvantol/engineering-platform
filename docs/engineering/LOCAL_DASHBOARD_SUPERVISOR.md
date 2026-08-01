# Local Dashboard Supervisor

The macOS Engineering Dashboard supervisor is a local-only companion for the
private dashboard. It binds only to the current Tailscale IPv4 address on port
`8765`, relays requests to the loopback dashboard backend, and supervises the
local Inbox watcher.

The supervisor never creates public listeners, Funnel configuration, ACLs,
port-forwarding, pull requests, releases or deployments. It retries when
Tailscale is temporarily unavailable during macOS startup; temporary listener
failures are not process crashes. Broken client connections are handled without
terminating the supervisor.

The watcher remains repository-owned and runs only for the configured local
repository. It has no additional authority beyond the normal Engineering
Platform lifecycle.
