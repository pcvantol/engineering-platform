# Agent ↔ Server trust lifecycle (B6A)

## Scope and transport

B6A is the authenticated control-plane foundation only. The standalone Server
is durable registration and liveness authority; a Project Agent is a local
execution edge, and no engineering execution is dispatched in this phase.
The MVP transport is JSON over loopback HTTP. The Server continues to bind only
`127.0.0.1`; an insecure non-loopback Agent endpoint is rejected. Loopback is
not authentication. Future LAN operation must introduce TLS with a configured
and verifiable Server identity before the bind policy is widened.

The Agent stores the configured endpoint and the Server instance UUID received
during pairing. Every response carries that UUID; a changed UUID fails closed
and requires explicit re-pairing. This protects the supported local development
topology from accidental endpoint substitution; TLS certificate pinning is
deferred with LAN transport.

## Pairing and credentials

The operator runs `engineering-platform-server pairing-create --agent-id ID`.
It creates a random, single-use pairing code, valid for ten minutes, in the
Server SQLite store. The Agent runs `engineering-project-agent pair
--server-endpoint http://127.0.0.1:PORT --pairing-code CODE`. On success, the
Server creates/stabilizes the durable paired Agent ID, returns a random bearer
credential once, and stores only its domain-separated SHA-256 verifier.

The Agent's B4 installation UUID is its Agent ID; host identity (hostname, OS,
architecture, OS-user) is observed metadata, never a credential. The Agent
configuration contains endpoint, expected Server UUID, Agent ID and credential
outside Git at user configuration scope with mode 0600 (parent directory 0700).
B6B may configure this path and invoke `register` then periodic `heartbeat`; it
must not add pairing or launchd semantics.

## Registration and repository reporting

Authenticated `register` persists host metadata, B4's bounded capability
snapshot, B5 validated attachment read-surfaces, registration state and
last-seen time. The report only includes explicitly supplied roots that contain
valid `.engineering-platform/repository.json`; it omits local paths, secrets,
and filesystem discovery. It can report zero or many repositories. B5 logical
project topology remains declarative and is not replaced by a host inventory.

## Liveness, reconnect, and reset

An authenticated heartbeat updates `last_seen_at`. Status is `ONLINE` within
90 seconds and `STALE` afterwards; registration survives temporary loss,
Agent restart and Server restart. Invalid credentials, unknown/revoked agents,
malformed payloads and protocol versions other than `1.0` are rejected without
credential disclosure. `agent-revoke` invalidates the credential durably;
`agent-reset` removes the registration and pending pairing material, allowing a
fresh pairing.

## Threat model and deferred scope

Explicit operator code approval blocks unsolicited enrollment. Random one-time
codes, verifier-only storage and durable revocation limit impersonation,
credential theft and replay. Server UUID pinning and loopback-only transport
avoid same-machine/localhost trust assumptions; network interception and
Server impersonation on a future LAN require TLS and are intentionally not
enabled by this MVP. No PKI, discovery, Workspace/Forge mediation, CENTRAL
activation, macOS installer, scheduling, or execution dispatch is included.
