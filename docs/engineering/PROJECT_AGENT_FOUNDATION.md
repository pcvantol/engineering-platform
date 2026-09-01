# Project Agent foundation (B4)

`engineering-project-agent` is a standalone, local process that observes one
Host/OS-user context and zero or more explicit repository roots. It can run
without an Engineering Platform Server or any DJConnect checkout.

Run it after installation:

```sh
engineering-project-agent --repository-root /path/to/repository
```

It emits a JSON observation with a stable local installation identity, host
boundary, Git/toolchain/provider-CLI availability and repository inventory.
Tool discovery only checks executable presence and bounded `--version` output;
it does not inspect credentials, authenticate, contact a provider, or execute
repository work.

## Boundaries

The identity is a UUID persisted locally in the current user's configuration
directory. It is an input to future B6 pairing, not a credential, server
registration, authorization decision, or durable execution record. A host
boundary is the observed hostname, OS user, operating system and architecture.

Repository roots are explicit observations. The Agent supports zero, one, or
many roots, but does not own the final project-attachment model, scan arbitrary
directories, change repositories, or create durable attachment state.

The Agent owns no queue, scheduler, lock, execution lifecycle, checkpoint,
terminal evidence, or admission decision. Engineering Platform Server remains
the future durable execution authority.

## B6 convergence handoff

- **Identity surface:** `AgentIdentity` exposes `agent_id`, `identity_format`,
  `host_context_key`, and `created_at`.
- **Capability model:** `CapabilitySnapshot` contains the host boundary and
  typed Git, toolchain and provider-CLI facts.
- **Local lifecycle:** each invocation observes and emits one snapshot, then
  exits. Process supervision/singleton policy is intentionally deferred.
- **Client abstraction:** `AgentControlPlaneClient.publish_observation()` is a
  transport-only placeholder; no network implementation exists.
- **Open questions:** pairing proof and credential storage; whether host facts
  need privacy-preserving rotation; reconnect/backoff and snapshot freshness;
  server-side registration/admission; and the canonical project-attachment
  schema and lifecycle.

## B6B macOS lifecycle

The Project Agent can be installed as a macOS per-user LaunchAgent without an
EP Server or Workspace Client. See
[macOS Project Agent service lifecycle](MACOS_PROJECT_AGENT_SERVICE.md) for
the role artifact boundary, paths, commands, B6A configuration extension
points, security boundary, and Forge Platform composition handoff.
