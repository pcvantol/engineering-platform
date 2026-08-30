# Engineering Platform 2.x consumer contract

## Purpose

This contract lets DJConnect, Forge and Workspace consume an installed,
pinned Engineering Platform wheel without carrying EP source code or owning EP
execution data.

## Local Consumer API v1 contract foundation

Phase 1 / Increment 1 defines a machine-readable, versioned **v1** contract.
It is an additive contract artifact only: no API server, socket, credential
issuance, credential verifier persistence, Keychain call, consumer cutover or
storage migration exists in this increment.

The v1 contract categories are request, response and error envelopes; project
scope; consumer identity and authentication envelope shape; normalization;
validation; and redaction. A later runtime must expose this same contract over
HTTP with versioned JSON. HTTP/JSON is the public contract, not an authorization
to bind broadly: exposure defaults fail-closed and remains configuration
controlled. A future Unix-domain socket may be an internal or optional local
transport only; consumers must not depend on a socket path as their API
identity.

Every contract operation uses explicit `project_id` scope. Consumer identity
and the future bearer credential are both bound to that scope; a mutable
`project_name` is never authorization identity. Missing or malformed scope,
incompatible versions, unsupported request types, unknown forbidden fields,
malformed authentication envelopes, invalid normalization input and oversized
bounded values fail closed without permissive repair.

### Authentication and secret boundary

For each registered consumer/project relationship, EP will issue one opaque,
cryptographically random bearer credential. EP alone owns issuance, credential
identity/fingerprint, validation, revocation, rotation and metadata. Consumers
receive only the opaque credential and store it in their operating system's
native secret store—Apple Keychain on macOS. Credentials must never be stored
in source control, repository configuration, consumer-owned SQLite,
environment files committed to source, Prompt History, Engineering Reports,
logs or dashboard projections.

EP must never retain the plaintext reusable credential after issuance. The
later authentication-runtime increment persists only the bounded verifier,
fingerprint and metadata necessary for validation. Credential policy is
independent of submitted engineering-prompt content: prompts can never grant,
expand or override API authorization.

### Deterministic validation, normalization and errors

The v1 schema will define deterministic Unicode and newline normalization,
empty/null handling, bounded identifiers and stable serialization where each is
applicable. It rejects malformed input rather than silently repairing it.
Errors are bounded, machine-readable and stable; they must never reveal a
bearer credential, authorization header, provider secret, stack trace or
database content. Any projection containing a prohibited secret-bearing field
is rejected or redacted before it reaches logs, reports, Prompt History or the
dashboard.

### Phase 1 / Increment 2 architecture authorization

Increment 2 is authorized but not implemented. ADR-0021 fixes the later
runtime shape: a dedicated EP-owned, loopback-only service at `127.0.0.1` with
default port `8766`; unauthenticated bounded `GET /health`; and one read-only,
authenticated `POST /v1/capabilities` endpoint. It adds no consumer cutover or
mutating engineering action.

The transport accepts a credential only through `Authorization: Bearer
<credential>`. `UNAUTHENTICATED` is the stable 401-equivalent error for missing
or invalid credentials; `PROJECT_NOT_AUTHORIZED` is the stable 403-equivalent
error for a valid credential outside the exact canonical `project_id` scope.
The future schema-39 EP-owned verifier record never stores plaintext bearer
values. Issuance, registration, rotation/revocation workflows and Keychain
integration remain Increment 3 work.

### Local Consumer API v1 envelope schema

The canonical machine-readable version representation is the string `"1.0"`.
The v1 foundation accepts exactly one non-dispatching request identity,
`"contract.foundation"`; it proves envelope compatibility only and performs no
business operation. Every request is a JSON object with exactly these fields:

```json
{
  "contract_version": "1.0",
  "request_type": "contract.foundation",
  "request_id": "request-123",
  "project_id": "project-123",
  "consumer": {"consumer_id": "workspace-client"},
  "auth": {"scheme": "bearer", "credential": "opaque-carrier"},
  "payload": {}
}
```

`project_id`, `consumer_id` and `request_id` are 1–128-character canonical
ASCII identifiers matching `[a-z][a-z0-9-]*`. They are lower-case, opaque
identities; display labels, paths, repository names and prompts are never
substitutes. `project_id` is mandatory. The authentication envelope defines
only an opaque bearer-carrier shape: its `credential` is printable ASCII,
non-empty and at most 4096 characters. It is not issued, verified, persisted,
rotated or sent to a Keychain in this increment.

The payload is a bounded JSON object (at most 8192 UTF-8 bytes, depth 4 and 64
items per object/array). Payload strings and keys are NFC-normalized and have
CRLF or CR newlines normalized to LF. Identifiers and credentials are not
silently normalized: a non-canonical value is rejected. `null` remains distinct
from an omitted field. Unknown fields in every envelope are rejected.

Success responses contain exactly `contract_version`, `request_id`, `status`
(`"success"`) and bounded `payload`. Error responses contain exactly
`contract_version`, `request_id`, `status` (`"error"`) and `error`; `error`
contains a stable `code`, safe fixed `message`, and optional bounded `field` and
`path`. The v1 error codes are `INVALID_CONTRACT_VERSION`,
`MISSING_PROJECT_ID`, `INVALID_PROJECT_ID`, `INVALID_CONSUMER_IDENTITY`,
`INVALID_AUTH_ENVELOPE`, `UNKNOWN_FIELD`, `UNSUPPORTED_REQUEST_TYPE`,
`INVALID_NORMALIZATION`, `VALUE_TOO_LARGE` and `MALFORMED_REQUEST`.
ADR-0021 additionally reserves `UNAUTHENTICATED` and
`PROJECT_NOT_AUTHORIZED` as the Increment-2 transport authentication and
authorization errors; their runtime mapping is not implemented yet.

Contract JSON is serialized with sorted keys, compact separators and UTF-8
Unicode. Transport serialization preserves the credential carrier; safe
rendering replaces it with `[REDACTED]`. Errors and safe rendering never echo
submitted credential values, authorization headers, payload text or other
secrets.

### Increment acceptance gate

The subsequent implementation must test valid requests/responses/errors,
missing and malformed `project_id`, incompatible versions, unknown fields,
Unicode and newline normalization, deterministic serialization, stable errors,
malformed auth envelopes and credential redaction/prohibited-secret
projections. Full EP and dashboard regressions plus the extraction audit must
pass. It does not require a new live-API Managed E2E, but existing iCloud/HUMAN
ingress, provider recovery, validation/qualification, reporting and
Forge-facing behavior must remain regression-green. No schema activation,
historical evidence rewrite or runtime cutover is allowed.

## Project registration

Before a consumer submits an Engineering Action, it registers one active
Workspace project with the local EP installation. Registration contains:

- `project_id`: immutable, opaque and canonical in Workspace;
- `project_name`: current human-friendly Workspace label, required for normal
  dashboard presentation but never used as identity;
- the validated local repository/workspace path;
- the project-specific writable Inbox root;
- optional consumer display metadata.

`project_id` is mandatory on every consumer-to-EP operation. Paths, repository
names and display names are not identities and cannot substitute for it.

### Registration projection

EP stores a single current registration record per `project_id`, including at
least `project_id`, `project_name`, repository/workspace path, Inbox root and
the last registration update time. `project_name` is a mutable label supplied
by Workspace. Registering the same canonical ID with a new name is an atomic
label update, not a new project and not a migration of execution data.

The dashboard's project selector renders `project_name` as its primary text.
It may expose `project_id` as secondary diagnostic information, but it must
not make the technical ID the normal label. If a legacy registration has no
name, EP uses a clearly marked temporary fallback until Workspace refreshes
that registration; it must never infer a name from a repository path.

## Ownership and isolation

EP keeps one installation-wide SQLite database. All EP execution data carries
`project_id` and is queried, queued, leased and displayed within that project
scope. Each project has an independent Inbox route and queue; an execution for
one project can never consume another project's prompt.

`project_id` enters EP as a Forge/Workspace-owned foreign canonical identity.
EP validates its registration but never mints, infers or translates it from a
path, repository name or label. The registered repository/execution scope is
the concurrency boundary: at most one mutating EP execution may own that scope
at a time. FIFO is the default ordering within a scope; policy may make an
explicit, auditable selection without making EP a Forge planner. The mutation
lease remains held through delivery, finalization and reconciliation, not just
through provider execution.

Project names are deliberately not copied into project-scoped lifecycle,
receipt, report, telemetry or Prompt History rows. Their stable relation is
always the canonical `project_id`; selector and dashboard labels are resolved
from the current registration so a Workspace rename is immediately reflected
without rewriting historical evidence.

Workspace keeps its own planning state and canonical project registry.
Forge remains the owner of planning and Runtime Prompts. EP remains the owner
of execution lifecycle, telemetry, evidence, dashboard, Inbox and Prompt
History. The physical Inbox transport and Workspace API route remain parallel
ways to admit a prompt for the same registered project.

### Settings scope

The registered Inbox root, Inbox scan interval and open-pull-request check
interval are project settings. They belong to the selected project's queue,
never to installation-wide EP configuration. Log retention, log level,
platform-health refresh and component-detail refresh remain installation-wide
settings. Fixed lifecycle, lease and retry safeguards remain EP runtime
defaults rather than consumer-editable project settings.

## Upgrade and compatibility

The local upgrade runs before the installed EP process becomes the writer:

1. create a recoverable backup of legacy EP state;
2. verify consumer and wheel compatibility;
3. register the legacy workspace as one canonical project;
4. migrate and backfill project-scoped EP records in place into the central
   installation store;
5. update launchd to the installed EP commands;
6. validate that only the installed EP writer is active.

The consumer pins the immutable EP 2.x wheel version. It must fail closed when
the requested contract version or canonical Workspace `project_id` is absent.

## Installation and consumer onboarding

The native macOS Engineering Platform installer owns installation, CLI
dependency setup, installation data-root creation, empty SQLite creation,
LaunchAgent activation and first-run Operations Console opening. A consumer
does not bootstrap these components itself and must never add the installation
database, credentials, LaunchAgent plists or EP source tree to its repository.

After the operator completes explicit provider login in the Operations Console,
the consumer registers a project through the Local Consumer API. Registration
requires the canonical `project_id`, current `project_name`, allowed local Git
checkout and an EP-owned project Inbox route. For Managed execution, EP itself
also verifies the repository remote/upstream, worktree safety and GitHub access
before it can admit work.

For CI, every consumer pins the EP wheel and Consumer Contract version and
tests its adapter against an isolated ephemeral EP store. CI must not invoke
the native installer, start an EP LaunchAgent, access a real user installation,
authenticate a provider or submit work. It validates compatibility and the
consumer boundary; an installed EP host remains responsible for provider
readiness, admission and execution.
