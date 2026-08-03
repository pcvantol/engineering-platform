# Execution Host Configuration Resolver

The Execution Host Configuration Resolver is the sole provider-neutral boundary
for host-specific Engineering Platform configuration. Consumers request a
capability; they do not construct iCloud, Inbox, report, status, log, runtime
or telemetry paths.

`tools.engineering.platform_api.execution_host_configuration(root)` provides:

- Runtime Prompt transport;
- status, report, log and telemetry stores;
- configured runtime executable; and
- safe Execution Host identity: name, version, runtime and transport.

The current configured Runtime Prompt transport is `icloud_inbox`. iCloud is
therefore an implementation detail of the resolver, not a Forge concern. The
same API can later resolve a SQLite queue, REST transport or GitHub Actions
without changing consumers.

The resolver reads the canonical Engineering Platform configuration and never
owns scheduling, engineering, mission or repository logic. Invalid, missing or
unsupported configuration fails closed. Dashboard projections may display only
the safe host identity fields; filesystem locations are never displayed.

Forge communicates through the Execution Host Contract only. It must not know
the selected transport or any host-local directory.
