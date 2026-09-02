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

## Project-scoped Operations Console

The integrated Console has one CENTRAL-selected project at a time. Its selector
lists only `ACTIVE` projects with a currently valid `BOUND` local repository;
the selector label is the registered `project_id`, never a hardcoded repository
name or a historical workspace title. Switching project reloads the Console
with the selected scope. The Server resolves that scope before every
project-sensitive dashboard request, and rejects an unknown, inactive or
unbound scope with `CONSOLE_PROJECT_UNAVAILABLE`.

Browser `fetch` requests carry the selected scope in
`X-Engineering-Platform-Project`. Since `EventSource` cannot set that header,
its request carries `?project=<project_id>` instead. The Server consumes that
parameter for scope resolution and removes it before delegating to the
preserved dashboard routes. This preserves historical routes such as
`/api/events` while ensuring their projection uses only the selected bound
repository. Other query parameters, such as evidence-download audit flags,
are retained. The separate `/diagnostics/topology` surface remains a
CENTRAL-wide, secret-free installation diagnostic and is not the project view.

## Validation environment status

The Console's Configuration section shows a separate **Validation
environment** block below provider login status. It reports the installed
Server Python readiness, its resolved executable path and its exact Python
version. The projection is token-free and derives from the same interpreter
that runs the installed Server. A missing or indeterminate runtime displays a
sticky alert and the existing repair/recheck action; a `READY` runtime has no
runtime alert. A running Server alone is not end-to-end validation evidence:
qualification still verifies that child validation commands resolve this
installed environment.

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
