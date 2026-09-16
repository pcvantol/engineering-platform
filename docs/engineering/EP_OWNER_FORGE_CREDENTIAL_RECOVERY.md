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
The expected consumer supplied from Forge's observed peer state must match that
EP-owned result; the Keychain account name, a project match or a historical
correlation ID is never treated as the consumer identity.

The readback creates no registration or credential. Missing, ambiguous,
disabled, revoked, wrong-instance and wrong-scope states fail with distinct
secret-free codes.

## Recovery protocol

`owner-credential-recover` requires a durable operation ID and binds it to the
EP instance, existing consumer/project/repository, Forge runtime, peer binding,
peer-configuration digest, installed owner UID and the fixed Keychain target.
Concurrent active recovery for the same consumer/project is rejected.

The native Security.framework adapter never places credential material in
arguments, environment, output, logs, exceptions, representations or files.
If the fixed Keychain item already contains an active credential for the exact
scope and authenticated HTTP readback succeeds, EP records `REUSED` and issues
nothing. A valid credential for another consumer or project fails closed and is
not overwritten.

For missing, revoked or unrecognized material, EP generates one deterministic
operation-bound verifier and writes its material directly to the fixed
Keychain item. The candidate is durably staged outside the production
credential namespace. This allows the real authenticated HTTP route to prove
the stored material while every existing production credential remains active.
Only after that proof does one CENTRAL transaction promote the candidate to a
production credential, enforce the production limit and, when necessary,
revoke precisely the pre-bound oldest credential in the same consumer/project
scope. Authentication failure revokes the candidate and removes the new
Keychain value without revoking either pre-existing production credential.

The operation states `PREPARED`, `CENTRAL_ACTIVATED`, `SUCCEEDED`,
`FAILED_SAFE` and `STOPPED_UNCERTAIN` make the two-store boundary explicit.
Replaying the same operation reconciles an interruption without issuing a
second candidate. `owner-credential-recovery-status` returns only identities,
digests, timestamps, disposition, credential IDs/fingerprint and error code;
plaintext material is never returned.

## Qualification boundary

The isolated suite uses the real CENTRAL schema, registration, verifier,
operation journal and recovery state machine. Its Keychain object and failure
callbacks are named test doubles with synthetic material. The live HTTP suite
uses the real EP server route and verifies that two valid same-project
credentials produce their own EP-derived consumer identities. Forge separately
qualifies its real HTTP adapter against this contract, including rejection of a
same-project credential for a different expected consumer before submission.
Installed-runtime and full Forge-to-EP-to-Forge evidence must still be recorded
separately; neither a source test nor successful recovery establishes the
historical cause of credential loss.
