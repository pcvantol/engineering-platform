# EP HTTP-only peer integration and ingress parity V1

**Increment:** `HTTP_ONLY_PEERS_AND_THIN_CLI_V1`. **Owner:** Engineering Platform.
**Status:** documentary refinement and conformance plan; NO_BUMP.
[Delivery DAG](../development/HTTP_TRANSPORT_BOUNDARIES_V1_DAG.json).
This is a conformance slice of existing P-TRANSPORT, not a new transport engine.
Source observed 2026-09-12: EP `bd4c09ff85bc82783da6efb9f1f781dfb8100e17`,
Forge `f2f1d8d8535d23323397df4ff7af8586f2c6364f`, Workspace
`a12d5b925da1e04b86c1142e709f6f0fb4c7f617`. Existing HTTP/CLI/File Inbox
foundations are retained, not reclassified as unimplemented. Only the new
conformance qualification below remains PLANNED without new evidence.

## Peer restriction versus EP ingress support

Forge Server -> EP Server and Workspace Server -> EP Server use ONLY the
versioned authenticated EP HTTP API, even on the same host. Workspace asks EP
about EP-owned operations directly, not through a generic Forge proxy. The
products must not use EP CLI, File Inbox, imported runtime classes, direct SQL,
shared queues or local filesystem commands as a peer transport or outage fallback.

EP nevertheless retains its own supported HTTP, installed CLI and Server-owned
File Inbox adapters. Human/local AI automation may use the supported CLI or
Inbox under their real scope and admission rules. No Inbox deletion or transport
regression is authorized. A JSON producer envelope alone does not prove which
transport carried it; conformance tests must observe actual HTTP traffic and
prohibit peer shortcuts. Qualified DTO/client libraries may encode HTTP contracts,
not embed EP business/storage implementations. One adapter's availability grants
no consumer the right to use a forbidden product-integration path.

Each ingress translates input into the SAME EP-owned application-service boundary.
Admission, identity, capabilities, queues, leases, policy, retries and immutable
evidence stay there, not in CLI commands, HTTP handlers or Inbox watchers. An
Inbox file arrival, CLI exit or HTTP 2xx is not execution completion. Preserve
three-ingress regression tests and qualification for every currently supported
operation; exact interfaces may expose different authorized capability sets.

Own CLI is the EP administration/AI-automation entrance. A qualified local
management command may use its own service when HTTP is unavailable; remote
CLI uses HTTP. LOCAL_ONLY_ADMIN must be explicitly declared, use exact installed
identity/root and OS authority, and preserve writer exclusion/audit. Do not
expose privileged local commands remotely just to make all routes identical.
No second writer, direct SQL recovery, silent service startup, new credential
or grant follows from a failed HTTP request. Runtime provider/Git/launchd use
is EP-owned execution tooling, not forbidden peer CLI integration.

## Identity, effects and authority

Authenticate and authorize callers against the selected EP instance and project/
repository capability scope. Preserve caller principal and service identity;
never trust roles in request JSON or infer privilege from localhost/Tailscale.
Peer grants are independent from login, discovery and an enabled UI button.
Browser-facing mutation additionally preserves Origin/CSRF protection. Binding
and descriptor discovery are not alternate command transports.

Use existing operation/idempotency and expected-state/version contracts. Duplicate
HTTP requests reconcile the same submission/receipt; ambiguous acknowledgement
cannot switch transport or reset budgets. Cancellation/disposition remains the
owning existing state transition, not deletion of a run. Consumer callbacks, if
introduced, require an explicit versioned HTTP contract; otherwise readback.
No call into Forge internals or another product's DB is allowed. Retain terminal
v1.2 compatibility until an independently versioned consumer-qualified change.

## Conformance roadmap

| Node | Internal dependencies | Required delivery/evidence |
| --- | --- | --- |
| EH-CONTRACT | none | Classify existing ingress operations and HTTP-only peer constraints |
| EH-CONFORMANCE | EH-CONTRACT | Reuse HTTP/CLI/Inbox suites; add peer no-shortcut and authority negatives |
| EH-Q | EH-CONFORMANCE | Exact installed ingress/API/OpenAPI/Postman conformance evidence |

EH-CONFORMANCE consumes the existing qualified P-TRANSPORT subset required by
its tests. This does not require finishing installers, PR175, SA-ROLE, Workspace
or Forge UI. New needed behavior must be handled in a separately bounded repair;
this document does not declare current tests failed or waive existing invariants.
Actual evidence may satisfy a test without rebuilding a working EP ingress.

## Acceptance

Use HT-01..HT-07 in the coordinated transport contract as shared scenario IDs.
For EP, preserve supported three-ingress equivalence of authorization, validation,
idempotency, admission and terminal readback. Assert Forge/Workspace consumer
fixtures actually use authenticated HTTP and fail if CLI/import/SQL/Inbox fallback
is attempted. Test same/different hosts, absent/forged/expired or wrongly scoped
credentials, unavailable endpoint, lost acknowledgement and restart. Producer
fixtures prove EP semantics, not live consumer delivery.

Track OpenAPI, actual route inventory and derived Postman parity for affected
operations, including errors. Local-only capabilities remain intentionally absent
from public operation routes. Keep current coverage/security/installed/ingress
requirements. Immutable execution evidence, budgets, credentials and CENTRAL stay
unchanged. This documentation performs no runtime, route, File Inbox, workflow,
installation, queue, Mission or canary mutation; all grants remain as they were.
