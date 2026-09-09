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

## Operational-installation diagnosis scope

`operational-diagnose` reads the selected Server LaunchAgent interpreter (not
`PATH`), the registered EP installation identity and the selected interpreter's
installed package metadata. `operational-inventory` separately reports only
explicitly supplied candidate interpreters and service references, normalized
as data paths while retaining each venv launcher spelling as a runtime
identity. Thus two venv launchers that share a base Python remain distinct
candidate installations.

The product invariant is `PER_PRODUCT_MAX_OPERATIONAL_RELEASE_INSTALLATIONS =
1` with required scope `MACOS_MACHINE`. The current read-only inventory can
observe only the current OS user's explicit references; it reports
`CURRENT_OS_USER_EXPLICIT_REFERENCES_ONLY` and `INCOMPLETE` rather than
claiming Mac-wide uniqueness. No conflict in that bounded inventory is not
`SINGLE_OPERATIONAL_INSTALLATION_VERIFIED`. Cross-account service discovery
and any removal/cutover remain an explicitly authorized future operation.

## Explicit development profile

A source-development Server is an explicit `development` runtime profile, not
an alternate operational installation. It must name a separate data root, a
non-operational loopback port, the exact interpreter inside its own venv, and
a development-scoped credential *reference* (never a credential value):

```text
engineering-platform-server init \
  --runtime-profile development \
  --data-root /absolute/development-root \
  --bind-port 8876 \
  --development-venv /absolute/development-venv \
  --development-credential-reference development:keychain/example
```

The profile records a private, token-free `development-profile.json` beneath
that root, plus contained development log and cache directories. `status` and
the health projection visibly report its development identity without exposing
the credential reference. A root with that marker cannot be launched later as
the default operational profile.

The profile rejects a selected canonical operational data root (including
normalized symlink aliases), a registered operational installation or its
interpreter, the operational default port, inherited operational credential or
data environment, and the Server/relay production service-label commands. An
unrelated inherited `EP_SERVER_DATA_ROOT` cannot select a development process:
the explicit `--data-root` is used and is propagated to its child. It also
rejects consumer-credential issuance and Agent credential reset/pairing from a
development runtime. The Server child and restart command carry the same
explicit profile arguments, so a process restart cannot silently revert to the
operational mode.

This is a local EP Server development boundary only. It neither installs a
LaunchAgent nor creates a second installer, and it is not proof of a
Mac-wide single operational installation. Forge Platform remains the owner of
cross-product composition; EP retains the operational Server installation
contract.

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

## EP-database configuration

Configuration presents the installation-owned **EP-database** separately from
project-scoped controls. The card reports the CENTRAL database's ownership,
location, size, schema version and integrity, and provides its governed
download, Finder and maintenance actions. It is a structural group only: its
background remains transparent so it inherits the Configuration section's
surface, while its border and maintenance divider retain the visual grouping.
No project-local database path or project ownership is presented there.

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
