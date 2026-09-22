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
