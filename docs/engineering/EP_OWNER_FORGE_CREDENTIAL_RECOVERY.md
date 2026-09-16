# Installed-owner Forge credential recovery

**Status:** EP-owned implementation and qualification contract. Installed
availability and a live recovery operation remain separate evidence states.

EP owns the credential authority for the existing Forge HTTP consumer. The
installed-owner route repairs only the fixed consumer-side destination
`keychain://forge.ep/consumer`; it is not a registration API, general secret
export, HTTP administration endpoint or alternative submission path.

## Binding and authority

`owner-consumer-readback` is secret-free and requires the installed EP
interpreter, the owned non-group/world-writable data root, the exact installed
instance, CENTRAL integrity and all critical live capabilities. An optional
relay may be degraded. The route identifies the consumer from the exact active
project/repository topology and the earliest immutable accepted Forge HTTP
exchange. Exactly one production consumer must have been temporally eligible.
When Forge already stores an expected consumer, it must match that EP-owned
result. For a legacy peer configuration without that field, the same route can
discover the consumer only when the complete installation/project/repository
evidence resolves exactly one active historical consumer. The Keychain account
name, a project match or a historical correlation ID is never treated as the
consumer identity.

The readback creates no registration or credential. Missing, ambiguous,
disabled, revoked, wrong-instance and wrong-scope states fail with distinct
secret-free codes.

## Recovery protocol

The discovery result is read-only and is not mutation authority. Forge must add
the consumer through guarded peer-configuration replacement. A recovery
operation that was prepared against the preceding peer digest can adopt exactly
that one new digest through
`owner-credential-recovery-adopt-peer-configuration`, but only before a
fingerprint or candidate has been bound and only while every other operation
identity remains unchanged.

`owner-credential-recover` requires a durable operation ID and binds it to the
EP instance, existing consumer/project/repository, Forge runtime, peer binding,
peer-configuration digest, installed owner UID and the fixed Keychain target.
Concurrent active or uncertain recovery for the same consumer/project is
rejected. A data-root-owned interprocess lock serializes the complete CENTRAL
and fixed-Keychain transition for both the same operation ID and competing
operation IDs. The kernel releases the lock after process loss; replay then
uses the durable operation state for reconciliation.

The native Security.framework adapter never places credential material in
arguments, environment, output, logs, exceptions, representations or files.
If the fixed Keychain item already contains an active credential for the exact
scope and authenticated HTTP readback succeeds, EP records `REUSED` and issues
nothing. A valid credential for another consumer or project fails closed and is
not overwritten.

For missing, revoked or unrecognized material, EP generates one operation-bound
secret, binds its fingerprint, writes it directly to the fixed Keychain item
and reads the stored value back. The candidate is durably staged outside the
production credential namespace with a bounded expiry. Normal submission and
operator authentication reject every pending recovery candidate. Only the
local installed-owner recovery probe accepts the exact candidate for the exact
`CENTRAL_ACTIVATED` operation and scope. Only after that proof does one CENTRAL
transaction promote the candidate to a production credential, enforce the
production limit and, when necessary, revoke precisely the pre-bound oldest
credential in the same consumer/project scope. Authentication failure revokes
the exact candidate and deletes the Keychain value only when its current
fingerprint is still owned by the operation; existing production credentials
remain untouched.

The operation states `PREPARED`, `CENTRAL_ACTIVATED`, `SUCCEEDED`,
`FAILED_SAFE` and `STOPPED_UNCERTAIN` make the two-store boundary explicit.
`STOPPED_UNCERTAIN` remains an occupied scope: another operation ID cannot use
it to issue another candidate. Replaying the same operation reconciles its
stored material and exact candidate, either resuming the proof or performing a
scoped rollback, without issuing a second candidate. Interruptions after
fingerprint binding, Keychain write and CENTRAL activation are all replayable.
If an older installation already contains both an uncertain receipt and the
active receipt that the prior policy allowed after it, migration preserves
both and permits only the oldest blocking receipt to reconcile; the successor
remains blocked until that receipt is terminal.
`owner-credential-recovery-status` returns only identities,
digests, timestamps, disposition, credential IDs/fingerprint and error code;
plaintext material is never returned.

## Qualification boundary

The isolated suite uses the real CENTRAL schema, registration, verifier,
operation journal, interprocess lock and recovery state machine. Separate
processes are synchronized at explicit transition edges to prove same-operation
replay, competing operations and interruption recovery without timing races.
Its Keychain object and failure callbacks are named test doubles with synthetic
material. The live HTTP suite uses the real EP server route and verifies that
two valid same-project credentials produce their own EP-derived consumer
identities, while a pending candidate is denied on ordinary routes. Forge
separately qualifies its real HTTP adapter against this contract, including
rejection of a same-project credential for a different expected consumer before
submission.
Installed-runtime and full Forge-to-EP-to-Forge evidence must still be recorded
separately; neither a source test nor successful recovery establishes the
historical cause of credential loss.
