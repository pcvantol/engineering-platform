# EP Server system-domain multi-instance runtime and provisioner v1

**Status:** canonical product contract and installed-artifact qualification
boundary. This contract does not install or activate a production host.

```text
EP_SERVER_RUNTIME_DEPLOYMENT_CONTRACT=FROZEN
EP_FORGE_PLATFORM_PROVISIONER_CONTRACT=FROZEN
```

## Ownership boundary

Engineering Platform owns instance identity, installed runtime selection,
LaunchDaemon definition, CENTRAL/data, provider contexts, inventory,
identity-aware readiness, lifecycle locks, backup, migration, activation,
verification, cleanup, recovery and terminal receipts. Forge Platform may
select an exact published artifact and exact instance and invoke
`engineering-platform-system-provisioner`; it must not supply or rewrite a
service label, account, data root, interpreter, provider home, migration
command, cleanup target or product receipt.

The provisioner contract is
`engineering-platform.system-provisioner/v1`. Its machine-readable commands
are:

| Command | EP-owned result |
| --- | --- |
| `inventory` | Enumerates every declared EP Server instance and all real collisions. |
| `create` | Creates exactly one requested instance from an exact version/source/digest wheel. |
| `status` | Returns exact service, runtime, provider and identity-aware health/readiness. |
| `update-assess` | Returns `NO_CHANGE`, `UPDATE_AVAILABLE` or a fail-closed block for one exact candidate. |
| `update-execute` | Uses the existing `installation_update_*` preparation, admission, backup, migration, activation, verification and cleanup engine. |
| `update-resume` / `update-status` | Reopens the same durable operation identity; no new candidate or target is selected. |
| `repair` | Re-registers and verifies the exact recorded runtime without rebuilding CENTRAL or changing identity. |
| `remove` | Requires exact instance-ID confirmation, removes only that instance's service and mutable root, and retains shared immutable slots. |
| `provider-register` | Commits a non-secret provider bootstrap receipt for an already instance-owned executable and auth context. |

Requests bind operation ID, opaque `instance_id`, display label, service
account, loopback port, artifact version, wheel digest and source revision.
Receipts are stored outside the removable instance root beneath the product
receipt tree. A repeated receipt identity is accepted only byte-for-byte.
Ambiguous inventory, stale target identity and collisions fail before product
mutation.

## Identity and topology

`instance_id` is a lower-case opaque stable identifier. It is not derived from
a hostname, port, label, user, project or checkout. The human-readable label
is display metadata only. The LaunchDaemon label and default service account
are deterministic SHA-256-derived identities of the opaque ID; a caller
cannot choose an arbitrary label.

```text
<product-root>/
  runtimes/slots/sha256-<artifact-digest>/venv/   immutable exact bytes
  locks/artifacts.lock                            shared-slot coordination only
  receipts/<instance-id>/<operation-id>.json      terminal product receipts
  instances/<instance-id>/
    instance.json                                 selected identity/readback
    data/                                         CENTRAL and installation record
    operations/                                   durable instance operations
    recovery/                                     retained instance recovery
    backups/ cache/ logs/
    locks/lifecycle.lock                          exact-instance lifecycle lock
    providers/
      codex/runtime, home, context.json, auth-state.json
      github/runtime, config, context.json, auth-state.json
```

Identical wheel bytes may share one immutable digest slot. No `current`
symlink, PATH result, moved venv or operation-staging venv is runtime
authority. Each instance record selects an absolute final-slot interpreter.
An update of A acquires A's lifecycle/update locks and the shared artifact lock
only while proving or creating immutable bytes. It does not acquire B's lock,
quiesce B, migrate B, retarget B or restart B.

Inventory treats multiple healthy EP Server instances as valid. It detects
duplicate instance IDs, LaunchDaemon labels, service accounts, data roots,
endpoints, provider homes and lifecycle locks; malformed or tampered
descriptors/services are ambiguous. A PID, plist filename or HTTP 200 is not
instance evidence.

## Instance-specific LaunchDaemon

Every instance runs as a macOS system-domain `LaunchDaemon` with:

- a collision-resistant `com.engineeringplatform.server.instance-<digest>` label;
- one non-root instance service account;
- the exact final-slot venv interpreter;
- `serve --data-root <owned-root> --expected-instance-id <opaque-id>`;
- deterministic system PATH used only for fixed host tools;
- instance-owned `HOME`, `CODEX_HOME`, `GH_CONFIG_DIR`, Codex prefix and
  explicit GitHub executable;
- RunAtLoad, unexpected-failure restart, background process type and
  instance-owned stderr log.

Server startup compares `--expected-instance-id` with the persisted runtime
identity before opening the API. Runtime substitution or a wrong data root
therefore fails before readiness. The service neither runs as root nor depends
on `gui/<uid>`, an interactive terminal, an installer user's HOME or a login
Keychain session.

## Provider contexts and cold boot

Codex and GitHub each own an executable/runtime installation, home/config
root, durable auth state and non-secret bootstrap receipt within the selected
instance. Readback re-hashes the regular non-symlink executable and binds the
auth receipt to provider, instance and executable digest. The GitHub provider
uses `EP_GITHUB_CLI_EXECUTABLE` when a system service pins it and fails closed
instead of falling back to PATH. Codex retains the existing explicit managed
prefix selection and gains an instance-owned `CODEX_HOME`.

EP does not copy or interpret opaque credential files. A later Forge Platform
human login/fan-out flow may use only a provider-supported bootstrap mechanism
and must finish by recording and independently verifying each exact instance
context. Tokens, credential values and provider output are absent from
descriptors, receipts and diagnostics.

Product readiness is PASS only when the LaunchDaemon is loaded, CENTRAL and
the authenticated/versioned API answer for the expected instance and release,
the selected executable is exact, and every required provider context is
independently READY. This contract is cold-boot-capable before GUI login. A
real fresh-Mac reboot remains joint Forge Platform installation acceptance and
is not claimed by source or isolated qualification.

## Update-engine reuse and recovery

System updates call `installation_update_plan`,
`installation_update_preparation`, `installation_update_admission`,
`installation_update_composition`, `installation_update_executor`, backup,
migration, activation and operation-journal modules. The generalization is
limited to two injected product adapters: the exact system-service resolver
and system activation action. Legacy per-user LaunchAgent activation remains
available for legacy installations but is not authority for a new system
instance.

The candidate venv is built directly in the immutable final digest slot. Its
operation marker and `runtime-slot.json` bind version, artifact digest, source
revision and final venv. It is never moved. The durable state machine still
owns inventory, quiescence intent, backup, target-interpreter migration,
record compare-and-swap, verification, cleanup and interruption resume. A
failure retains the exact operation, backup and recovery evidence; a retry may
resume only that identity.

## Project Agent boundary

`engineering-project-agent` remains a per-Host/per-OS-user LaunchAgent. Its
repository state, provider installations, provider credentials and Agent
identity stay user-owned. Multiple users may each run an independent Agent;
an Agent binds to an exact EP Server instance through its own trust contract
and never borrows Server provider credentials. Servers may be healthy before
GUI login while Project Agents are absent. The system provisioner does not
import, install, stop, repair, remove or rewrite Project Agent state.

## Qualification and release

`tools/qualification/system_multi_instance_installed_artifact.py` installs an
exact wheel into a fresh isolated venv, changes away from the source checkout,
and creates two Server instances from installed package bytes. It proves
distinct identities, roots, services, ports, provider homes/auth, locks and
receipts; shared immutable runtime selection; exact inventory; stop/restart A
without B; and remove A without B. Unit coverage adds duplicate/collision,
wrong-target, missing auth, executable substitution and Agent-scope guards.

The protected EP release workflow runs that qualification on both the built
wheel and the wheel downloaded again from PyPI before terminal
`RELEASE_COMPLETE`. Publication is not production installation or activation.

## Frozen acceptance

```text
EP_SERVER_SYSTEM_RUNTIME_V1=QUALIFIED
EP_SERVER_MULTI_INSTANCE=PASS
EP_SERVER_INSTANCE_ISOLATION=PASS
EP_SYSTEM_LAUNCHDAEMON_CONTRACT=QUALIFIED
EP_INSTANCE_AWARE_INVENTORY=PASS
EP_PRODUCT_OWNED_INSTALL=QUALIFIED
EP_PRODUCT_OWNED_UPDATE=QUALIFIED
EP_PRODUCT_OWNED_REPAIR=QUALIFIED
EP_PRODUCT_OWNED_REMOVE=QUALIFIED
EP_UPDATE_ENGINE_REUSED=PASS
EP_INSTANCE_OWNED_CODEX_CONTEXT=PASS
EP_INSTANCE_OWNED_GITHUB_CONTEXT=PASS
EP_COLD_BOOT_CAPABILITY_CONTRACT=PASS
EP_PROJECT_AGENT_USER_OWNERSHIP=PRESERVED
INSTALLED_ARTIFACT_QUALIFICATION=PASS
EP_SERVER_RUNTIME_DEPLOYMENT_CONTRACT=FROZEN
EP_FORGE_PLATFORM_PROVISIONER_CONTRACT=FROZEN
```

These values describe the protected source/installed-artifact contract. Live
Mac service-account creation, privileged LaunchDaemon installation, provider
login/fan-out, real reboot and Forge/EP pairing acceptance remain deferred to
the authorized Forge Platform joint installer qualification. No production
Mac, production credential, Mission, reset or T0 is affected.


## Product-owned preserve / purge / restore lifecycle

**Assignment:** `L2-PRODUCT-PRESERVE-PURGE-RESTORE-V1-20260928`

**Extension contract:** `engineering-platform.system-instance-lifecycle/v1`

This contract is additive to `engineering-platform.system-provisioner/v1`.
The existing `remove` operation remains destructive and backwards compatible:
it removes the exact instance service and mutable instance root and preserves
shared immutable runtime slots. Existing remove receipts are never reinterpreted
as preserve receipts.

New exact-instance commands are `preserve`, `purge`, `restore`, and
`lifecycle-status`.

### PRESERVE

Before any service or provider-state mutation, EP proves exclusive ownership of
the exact mutable instance tree. The selected instance root and every traversed
directory/regular file must not be group/world writable; regular files must
have exactly one hardlink. Symbolic links, special entries and foreign/unsafe
tree shapes continue to fail closed. This same ownership admission is reused by
RESTORE validation and explicit PURGE preparation/recovery.

EP removes/quiesces the exact product-owned LaunchDaemon but does not remove the
instance root. It retains the opaque instance ID, descriptor, CENTRAL/data,
configuration, logs/cache/recovery/backups and provider contexts under the same
product ownership. Every provider authentication receipt is deliberately moved
from `READY` to `PRESERVED_REQUIRES_REVERIFICATION`; credential references
and bootstrap receipt references remain product-owned bytes but are not treated
as current authentication proof.

The instance root is link-free and content-hashed after service removal and
provider-state demotion. Durable lifecycle evidence is stored under the EP
product root, outside the preserved instance tree.

Terminal semantics include:

```text
lifecycle_state = UNINSTALLED_DATA_PRESERVED
instance_identity = PRESERVED
mutable_instance_data = PRESERVED
restorable = true
service_state = REMOVED_OR_INACTIVE
shared_immutable_runtime_slots = PRESERVED
provider_auth_state = PRESERVED_REQUIRES_REVERIFICATION
```

### PURGE

`purge` is the explicit permanent lifecycle projection. It delegates the
actual destructive instance removal to the existing qualified `remove`
operation, then commits a product-owned purge tombstone outside the removed
instance root. A lost response between destructive remove and purge projection
is recovered with the same operation ID.

Terminal purge has `lifecycle_state = PURGED`, removed mutable instance data,
a retired instance identity and `restorable = false`. Shared immutable runtime
slots remain subject to the existing product cleanup contract. A prior preserve
receipt never authorizes restore after the purge tombstone exists.

### RESTORE

Restore names one exact preserve operation and accepts only the exact preserved
release identity. It fails closed on absent/tampered/foreign lifecycle evidence,
changed instance-tree bytes, changed instance identity, changed selected
artifact/source/version, a purge tombstone, unsafe links or a foreign runtime
slot.

After evidence validation, EP may recreate the exact immutable runtime slot from
the supplied exact artifact and re-register the same instance LaunchDaemon.
Restore deliberately **does not start the service** and **does not promote
preserved provider state**. Its terminal state is:

```text
lifecycle_state = RESTORED_REQUIRES_PROVIDER_REVERIFICATION
instance_identity = PRESERVED
mutable_instance_data = PRESERVED
service_state = REGISTERED_INACTIVE
provider_auth_state = PRESERVED_REQUIRES_REVERIFICATION
ready = false
```

The normal product-owned provider registration/bootstrap path must independently
re-establish current provider evidence before `repair` can start and qualify
the restored service. Forge Platform may consume these receipts; it may not
edit provider state, recreate service/data roots itself or infer restore from
filesystem presence.

### Qualification

The protected product qualification covers same-instance preserve/restore,
exact-release restore, provider-state demotion and re-verification, byte-level
tamper rejection, explicit permanent purge, sibling-instance non-interference,
lost-response recovery for preserve/restore/purge, receipt replay and existing
destructive `remove` compatibility. Qualification uses isolated product
fixtures only; no production Server/CENTRAL/credential mutation is authorized.

### Abrupt process-stop qualification (revision 26)

The historical 2.3.104 installed-wheel qualification above covered controlled
interruption and lost responses. The separate LANE_2 revision-26 qualification
found a real SIGKILL gap in `PURGE`: the legacy `REMOVE` receipt is published
before recursive deletion. If the process dies after deleting `instance.json`
but before the instance root and lifecycle tombstone are removed, the normal
same-operation `purge` retry cannot reopen the descriptor. The exact published
2.3.104 wheel reproduces this failure under Python 3.14; it remains historical
`GAP_PROVEN` evidence and is not relabeled.

The bounded recovery correction retains the prepared root's device/inode and
minimal topology outside the mutable instance tree. After a terminal exact
`REMOVE` receipt, recovery admits only that same root, rejects a replacement
root or changed surviving descriptor, checks the remaining tree for unsafe
entries, and finishes the original deletion before publishing the purge
tombstone and lifecycle receipt. The existing lifecycle request and receipt
identities remain unchanged. The reproducible installed-wheel SIGKILL harness
is `tools/qualification/system_instance_purge_sigkill.py`; it reports durable
`PURGE_READY`, physical partial deletion, exact mutator signal exit, surviving
descendants, same-operation recovery, terminal replay and sibling byte identity.

`tools/qualification/system_instance_lifecycle_sigkill.py` exercises the
product-owned process-stop matrix through the installed provisioner using two
disposable instances and a filesystem-only service adapter. Its 2.3.104
published-wheel runs observed `PREPARED`, `VERIFIED`, receipt-before-`COMPLETE`
and `COMPLETE` for PRESERVE and RESTORE. They observed `PREPARED`, `PURGE_READY`,
terminal REMOVE receipt, tombstone-before-receipt, lifecycle receipt-before-
`COMPLETE` and `COMPLETE` for PURGE. Each of these cells resumed the same
operation to the expected terminal status with identical replay and unchanged
sibling bytes. The separate partial physical deletion cell remains
`GAP_PROVEN` on published 2.3.104 and passed only with the local correction
candidate. A missed short phase is recorded as `NOT_HIT`, never promoted to a
hit based on timing.

Source tests and a locally built candidate-wheel SIGKILL run have passed this
specific partial-deletion cell. The new protected source, any required normal
release and requalification against its published bytes are still required
before this process-crash subset may be called qualified. The remaining
conflict, foreign-request and published-fixed-wheel checks must complete
before the entire revision-26 subset can be called qualified. No physical
reboot, power-loss, production service or installer acceptance is inferred.
