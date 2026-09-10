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
```

`init` creates a strict server configuration, a stable runtime instance ID and
an empty SQLite store. An empty store is valid (`operational_state:
empty-valid`): it does not imply CENTRAL activation, project attachment, or an
execution authority handoff. `start` supplies the lifecycle foundation and
serves local `GET /healthz` and `GET /readyz`; neither endpoint admits work.

The existing Execution Host remains unchanged and retains its current execution
authority. The server does not read a source checkout, `.engineering`, or any
DJConnect state at runtime.

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
