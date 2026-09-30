# Replacement Forge consumer after product-owned revocation

**Status:** EP 2.3.106 published-wheel conformance for a bounded installer
producer dependency. No EP runtime change, schema change or release is required
for this route. This document does not grant a project, repository or consumer
scope; those must already be approved and read back from EP's own topology.

## Product route

`engineering-ep-consumer-credentials` is the installed EP-owned operator CLI.
Its `--repo` argument selects the exact disposable root. For an installed
EP Server, the operator first verifies the exact server instance and selected
`epdata.sqlite` CENTRAL path, then runs the CLI with
`EP_CENTRAL_OPERATIONAL_DATABASE` set to that path. This is the existing
EP-owned CENTRAL selector; an arbitrary project checkout or legacy
`.engineering` database is not the installed Server's credential authority.
For an existing approved project and repository attachment, the operator uses
the following sequence with exact reviewed IDs:

1. `consumer-status --consumer-id OLD --project-id PROJECT`, then
   `consumer-revoke --consumer-id OLD --project-id PROJECT`. Read back OLD as
   `REVOKED`. Its prior credential may remain as a historical record, but EP
   authorizes no request under that registration.
2. Choose a **new** explicit consumer ID bound to the same approved project.
   `consumer-register --consumer-id NEW --project-id PROJECT` creates `ACTIVE`.
   Repeating that registration returns `idempotent=true`. Reusing OLD is
   rejected; a revoked registration is never promoted back to `ACTIVE`.
3. `credential-issue --consumer-id NEW --project-id PROJECT` produces one
   disclosure. Store the value only through the authorized secure consumer
   destination; retain only `credential_id` and fingerprint in non-secret
   review evidence. Verify an authenticated EP producer readback derives NEW,
   the exact project, repository attachment and expected EP instance before
   configuring Forge. A same-project credential for a different consumer is
   not an equivalent binding.
4. If the issuance response is lost before secure storage, call
   `credential-status --consumer-id NEW --project-id PROJECT`, revoke every
   uncertain newly issued credential by exact `credential_id`, verify their
   inactive status, and issue once more. Do not blindly replay
   `credential-issue`: it is **not idempotent** and permits up to two active
   credentials in one scope. If status is ambiguous, stop for owning recovery.

The installer records the old REVOKED status and its prior non-secret
credential metadata as historical evidence. The replacement review binds
NEW, PROJECT, the unchanged EP instance, the exact repository attachment,
the new credential ID/fingerprint, and the Forge peer configuration generation.
A new consumer ID or credential generation is not a new EP product instance.
No direct CENTRAL write, old-secret reuse, provider-VERIFIED promotion or
project/repository scope expansion is part of this route.

## Published-byte qualification

Run `tools/qualification/replacement_consumer_published_wheel.py` with a fresh
venv containing the published `engineering-platform==2.3.106` wheel, outside
the source checkout and with no `PYTHONPATH`. The script checks the supplied
wheel digest, calls the installed CLI, captures disclosures only in memory,
tests REVOKED old authority, same-project NEW registration and a simulated
lost issuance response, and prints only non-secret results. It uses one
disposable EP root. The exact 2.3.106 wheel SHA-256 qualified for this record
is `9d25a53d75b61d43d665d9f8290a968dc3e63d12d2037eae8ef31ee810eb6694`.

The Forge producer's `scripts/qualification/qualify_pairing_published_wheels.py`
also selects the real disposable EP Server CENTRAL, uses the installed
consumer CLI, performs actual EP HTTP compatibility requests through the
installed Forge peer adapter, revokes OLD, configures NEW, and preserves both
product instance IDs. Its only test adapter supplies bearer material in
memory to Forge's secure-store boundary; EP authentication, scope and HTTP
readiness are real. This cross-product test must be rerun against the final
published Forge wheel, not treated as a source or local-wheel release claim.

The CLI-only test does not prove live EP HTTP or Forge peer readback; the
cross-product test above covers those named boundaries. Neither proves
Keychain delivery, process-crash recovery, system service or full installer
acceptance. No EP release is made solely for this documented existing route.
