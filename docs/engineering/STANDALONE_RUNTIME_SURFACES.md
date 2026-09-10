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

`operational-diagnose`, `operational-qualify`, `operational-readback` and
`operational-update-assess` all resolve the same system-domain Server service
descriptor. They never treat `PATH`, `sys.executable`, a source checkout or a
legacy per-user LaunchAgent as the official runtime. An absent, malformed or
observer-mismatched system descriptor yields a machine-readable `UNKNOWN`
result with bounded system-service evidence rather than a guessed runtime.

`operational-inventory` reports that same canonical system-service evidence as
the official resolver surface. Explicit candidate interpreters and legacy
service references remain optional diagnostic evidence beneath a separate
`explicit_candidate_inventory` field; they cannot select the runtime or
upgrade the scope. Their paths are normalized as data paths while each venv
launcher spelling stays a distinct runtime identity. Thus two venv launchers
that share a base Python remain distinct candidate installations.

The product invariant is `PER_PRODUCT_MAX_OPERATIONAL_RELEASE_INSTALLATIONS =
1` with required scope `MACOS_MACHINE`. The current read-only observer covers
the system LaunchDaemon surface, shared LaunchAgents and only caller-declared
user LaunchAgent surfaces. It reports that bounded observation as `INCOMPLETE`
rather than claiming Mac-wide uniqueness. No conflict in this bounded inventory
is not `SINGLE_OPERATIONAL_INSTALLATION_VERIFIED`. Authoritative cross-account
service discovery and any removal/cutover remain explicitly authorized future
operations.

`operational-readback` is the product-owned, machine-readable consumer surface
for this same resolver. It uses only the fixed interpreter from the EP Server
system-domain service descriptor. If that owned service reference is absent,
malformed or differs from the bounded observer, it returns an explicit
`UNKNOWN` observation and never falls back to the invoking shell,
`sys.executable`, `PATH` or the legacy LaunchAgent. A registered runtime is
`ACTIVE` only after the actual health response proves its service, instance,
package and executable identity and classifies the live artifact-verification
state against the registered digest. A reachable response from a different
instance is `UNHEALTHY`, not evidence for the selected installation.

The readback embeds the EP-issued system-service inventory, with separate
outer evidence for the system, shared-user and caller-declared user surfaces.
Current coverage remains `PARTIAL`; a caller cannot supply a `MACHINE_WIDE`
mapping or turn a no-conflict observation into a single-installation claim.
The response keeps release-source facts separate: the installation record owns
version, digest, source revision and observed channel, while a release source
locator and qualification receipt remain with the qualified release evidence.

## System-domain Server service foundation

The future operational Server supervisor is one product-owned macOS
`LaunchDaemon`, in the `system` domain, rather than a per-user `LaunchAgent`.
Its service definition binds the fixed label
`com.engineeringplatform.server`, a normalized absolute EP data root, the
structural installed-venv launcher identity, and an explicit non-root service
account. It never selects an interpreter through `PATH`, runs the Server as
`root`, or accepts an arbitrary command. Exact EP-package and artifact identity
remain evidence that only the later provisioner may bind to this descriptor.
That provisioner must never follow a service-account-owned runtime-log symlink
while performing a privileged update.

The source-level definition and observer reject relative/PATH-like runtime
identity, a non-venv launcher, root as the service account, changed plist
content, a symlinked/nonregular plist, a missing configured account, and a
different data-root binding. Its read-only observer normalizes aliases such as
`/tmp` versus `/private/tmp`, opens directories and plist files through pinned
descriptors without following links, and records unreadable or raced surfaces
rather than associating stale bytes with a current path. No daemon-write,
`launchctl`, stop, replace or uninstall API is exported by this increment. A
later EP-owned provisioner must first bind its root-authorized
controller to durable machine-scope inventory, legacy-service quiescence,
exact artifact, backup/migration, health and cleanup evidence; it must use a
trusted directory-descriptor implementation for every privileged filesystem
mutation. This foundation does not create an account, choose an artifact,
perform a database migration, delete an old environment, or make a live
cutover decision.

`engineering-platform-server system-service-inventory --data-root <absolute-root>`
is the read-only, machine-readable evidence surface for this boundary.
`--declared-user-home <absolute-home>` may add explicit user LaunchAgent
locations to its inspection; it always also inspects the shared
`/Library/LaunchAgents` surface. It reports canonical system references,
conflicting system/user references, malformed EP service records, inspected
locations, and inaccessible locations. Declared homes are not an account
directory authority, so this inventory always reports `INCOMPLETE` Mac scope
and `single_operational_installation: false`. A root caller and an empty
inspection result therefore still do not establish
`SINGLE_OPERATIONAL_INSTALLATION_VERIFIED`.

The existing `service-install` command and `server_service.py` remain legacy
per-user LaunchAgent compatibility only; they are not authority for an
operational readback, diagnosis, qualification or development-profile
protection. The new source contract neither starts a LaunchDaemon nor removes
that legacy agent by itself; both actions require a qualified, authorized EP
installation operation with backup/migration/health/cleanup evidence.

## Operational update recovery boundary

The EP-owned update plan, lock, journal and executor are source-level
primitives for one registered installation. Before its first durable
`INVENTORIED` transition, the executor re-hashes the exact wheel named by the
plan; a delayed or modified artifact fails before inventory or service
quiescence. It then permits only the ordered lifecycle
`INVENTORIED → QUIESCED → BACKED_UP → MIGRATED → ACTIVATED → VERIFIED →
CLEANUP_PENDING|COMPLETE`. An interruption retains the last durable
transition; restart resumes only the remaining idempotent actions under the
same installation lock. Cleanup can resume from `CLEANUP_PENDING` without
repeating runtime activation.

This update primitive refuses a lower EP version. Rollback needs compatible
software *and* data evidence and is therefore a separate, explicitly
authorized recovery operation; an older wheel is never silently treated as a
rollback. The source contracts do not run an update, construct a runtime,
stop a service, or delete a machine artifact by themselves.

`operational-update-assess` is a read-only precondition for a composition
consumer. It first obtains the product-owned `ACTIVE`/`HEALTHY` readback, then
re-hashes the exact named wheel against the candidate version, digest and
source revision. It reports `UPDATE_AVAILABLE`, `UP_TO_DATE`, `INCOMPATIBLE`
or `UNKNOWN`; a same version/digest from another source revision is
incompatible. The assessment is not update authorization or execution: the
EP-owned installation lock, backup, migration, activation, postflight and
cleanup remain required at execution time.

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
system service nor creates a second installer, and it is not proof of a
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

Forge Platform consumes qualified Server and Project Agent artifacts without
recreating EP service internals. For the Server role its future product adapter
uses the EP-owned readback and exact-candidate assessment surface, retaining
the coordinator's own source locator and qualification evidence for
correlation. It may not pass an interpreter, `PATH`, service label, data root,
migration, credential or cleanup instruction back into EP. A product-owned
execution/resume adapter is still required before any installation action; no
source command above performs a live update. For the Agent role Forge Platform
uses the existing per-user lifecycle contract.
