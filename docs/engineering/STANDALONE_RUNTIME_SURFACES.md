# Standalone runtime surfaces (B8C-0)

The Engineering Platform Server role is an installed artifact. It owns the
CENTRAL database, loopback API, health/readiness endpoints, and the integrated
Operations Console at the Server listener root. `engineering-platform-server`
is the sole lifecycle entrypoint: `init`, `start`, `stop`, `status`, and
`health` all use its installation-owned data root. A launch must use the
installed executable, not `python -m` from a Git checkout.

The Server child starts from its data root with no inherited `PYTHONPATH` or
user-site import path. Its Python module must therefore resolve from the
installed artifact's site-packages. The Console uses only the CENTRAL's
secret-free topology projection; browser selection is presentation state and
is never request authority.

The Project Agent remains a separate per-user installed role. It owns local
checkout observation and authenticated attachment reporting, while the Server
owns logical topology and CENTRAL state. Either role can be installed alone;
same-host deployment uses distinct data roots and identities.

## Inbox disposition

The historical checkout-bound Inbox watcher is **FORMALLY_RETIRED** from the
standalone Server role. It relies on repository-local workspace state and has
no schema-42, project-scoped CENTRAL ingress contract. It must not be started
as `com.djconnect.engineering-inbox` or substituted for the Server API. A
future project-aware Server ingress capability requires its own governed
contract and EP-owned lifecycle.

## Forge Platform handoff

Forge Platform installs qualified Server and Project Agent artifacts
independently. For the Server role it needs the installed executable, data
root, lifecycle commands, health endpoint, and the integrated Console surface;
it must not recreate EP service internals. For the Agent role it uses the
existing per-user lifecycle contract.
