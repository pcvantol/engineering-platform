# Standalone EP Server foundation (B3)

`engineering-platform-server` is the canonical entrypoint for the standalone
EP Server foundation. It owns an installation data root, defaulting to a
platform application-data directory, never a project checkout or a DJConnect
workspace.

```text
engineering-platform-server init --data-root /secure/ep-server
engineering-platform-server start --data-root /secure/ep-server
engineering-platform-server health --data-root /secure/ep-server
engineering-platform-server stop --data-root /secure/ep-server
engineering-platform-server service-install --data-root /secure/ep-server
engineering-platform-server service-uninstall --data-root /secure/ep-server
engineering-platform-server relay-install --data-root /secure/ep-server
```

`init` creates a strict server configuration, a stable runtime instance ID and
an empty SQLite store. The installed Server's store is official schema **42**:
it contains canonical credential/consumer-registration structures, standalone
installation metadata, declared project/execution structures, Agent trust
tables, and immutable control provenance for installation, credential,
consumer-registration, and authority-relevant project-scope events. A fresh
store has no consumer credentials, projects, Agent registrations, executions,
leases, or Prompt History rows. It is created only from product-owned schema
definitions; it never accepts a legacy, forensic, or checkout database path.

`engineering_schema_migrations` is the schema-version authority. Readiness
fails closed unless it reports 42, the required tables and indexes exist,
installation metadata matches the local runtime identity, and SQLite integrity
passes. An empty store is valid (`operational_state: empty-valid`): it does
not imply CENTRAL activation, project attachment, or an execution authority
handoff. `start` supplies the lifecycle foundation and serves local
`GET /healthz` and `GET /readyz`; neither endpoint admits work. The installed
Server also owns the Operations Console at `/` and its secret-free topology
projection at `GET /v1/operations/projects`. See
[Standalone runtime surfaces](STANDALONE_RUNTIME_SURFACES.md) for the complete
installed-artifact authority and role contract.

`service-install` installs exactly one per-user macOS LaunchAgent for the
foreground `serve` entrypoint. Its fixed arguments use the installed Python
runtime and initialized data root; it never invokes the self-daemonizing
`start` helper or accepts arbitrary commands. `service-uninstall` removes only
that owned LaunchAgent and preserves CENTRAL and installation identity.

### System-domain migration boundary

The production-target supervisor is a separate EP-owned system-domain
LaunchDaemon contract. It is intentionally not an alias for `service-install`:
the latter is a legacy per-user lifecycle and cannot establish machine-wide
uniqueness. This increment defines and reads the future fixed label, exact
installed interpreter, absolute data-root argument and non-root service
account; it accepts neither `PATH` selection nor arbitrary commands. It does
not expose a privileged mutation API. A later EP provisioner must require root
authorization and use descriptor-anchored filesystem operations only after it
has durably bound machine-scope inventory, legacy-service quiescence, exact
artifact, backup/migration and postflight evidence.

The read-only `system-service-inventory` command reports system, shared-user
and explicitly declared user-service references, conflicts and inaccessible
inspection locations. It intentionally retains `MACOS_MACHINE` scope as incomplete until
a later product-owned privileged account-discovery and cutover operation
provides durable coverage evidence. No source-only contract changes the
current Mac, removes a LaunchAgent, creates an account, migrates CENTRAL or
claims a single operational installation.

The existing Execution Host remains unchanged and retains its current execution
authority. The server does not read a source checkout, `.engineering`, or any
DJConnect state at runtime.

`relay-install` is the optional, Server-owned installation boundary for the
Dashboard Relay. It compiles the package-owned Tailnet-to-loopback adapter into
the Server data root and installs its single canonical LaunchAgent. The relay
forwards only to the Server Console; it owns no project, submission, File Inbox,
Action, run or execution state.

## B6 convergence handoff

Exposed interfaces: `AgentRegistrationRequest` and
`AgentRegistrationIntake` are internal, transport-neutral extension points.
They deliberately do not specify a wire format, authentication, credential
issuance, enrollment persistence, project attachment, Agent lifecycle, or job
dispatch.

B6 must select and implement the Agent↔Server transport and authentication,
then bind authenticated registration to installation-owned persistence and the
future project-attachment model. It must also decide how a ready server is
distinguished from a server authorized to accept registered-agent work. B3's
health/readiness only proves the foundational process and empty store are
available.
