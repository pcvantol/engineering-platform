# macOS Project Agent service lifecycle (B6B)

## Installable role boundary

Engineering Platform owns the **Project Agent** artifact and its macOS
per-user service lifecycle. The package entry point is
`engineering-project-agent`; the service calls its installed executable with
`service run --config <installed-config>`. The LaunchAgent never points at an
Engineering Platform, DJConnect, or other source checkout.

The artifact metadata envelope identifies `product: Engineering Platform`,
`component_role: project-agent`, version, source repository/SHA, artifact
identity/SHA-256, macOS platform and architecture, compatibility, release
channel, qualification reference, and signature/attestation fields. B6B
defines this boundary only; qualified publication, checksums, signing and
attestation remain release-work concerns.

## macOS locations and ownership

All paths are per current OS user:

| Purpose | Location | Ownership/removal policy |
| --- | --- | --- |
| Config and local Agent identity | `~/Library/Application Support/Engineering Platform/Project Agent/` | 0700 directory; JSON 0600; preserved by uninstall |
| B6A credential integration | B6A-provided credential *reference* in config | no credential value is stored by B6B |
| Transient state/cache | `~/Library/Caches/Engineering Platform/Project Agent/` | installation-owned; bounded cleanup on uninstall |
| Lifecycle logs | `~/Library/Logs/Engineering Platform/Project Agent/` | installation-owned; current stdout/stderr logs cleaned on uninstall |
| Service definition | `~/Library/LaunchAgents/com.engineeringplatform.project-agent.plist` | installation-owned; removed on uninstall |

No operation scans a home directory or repository list. Repository inventory
and attachment remain B4/B5 explicit-input semantics. Uninstall never deletes
repositories, repository config, local Agent config/identity, credentials, or
Server data/evidence.

## LaunchAgent model

The stable label is `com.engineeringplatform.project-agent`. It is a user
LaunchAgent loaded in `gui/<uid>`, uses `RunAtLoad`, and restarts only after an
unexpected non-zero exit. It runs as the logged-in user; no root runtime or
privilege grant is needed. stdout and stderr go to the user log location.

The plist contains only an installed executable path, an installed
configuration path, log paths, and a deterministic `PATH`:

`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`

Optional absolute `toolchain_paths` may be prepended in config. This avoids
interactive-shell inheritance and developer-specific paths. No credential,
token, password, or secret environment value is accepted into the plist.

## Commands

Run from the installed package on macOS:

```sh
engineering-project-agent install
engineering-project-agent start
engineering-project-agent stop
engineering-project-agent restart
engineering-project-agent status
engineering-project-agent uninstall
```

`install` creates/validates private directories and config, writes one
deterministic plist, and bootstraps it with `launchctl`. `stop` unloads the
running service while retaining its plist; `start` loads it again. Repeating
installation rewrites the same plist and treats an already-loaded service as a
successful repair.
`status` reports `not-installed`, `misconfigured`, `running`, or `stopped`.
Installation does not contact an EP Server, so an unpaired Agent-only host is
valid and fails safely rather than performing reconnect work.

## B6A extension surface

The config schema is intentionally narrow:

```json
{
  "version": 1,
  "toolchain_paths": ["/absolute/toolchain/bin"],
  "b6a": {
    "server_endpoint": "B6A-owned endpoint value",
    "paired_agent_identity": "B6A-owned identity reference",
    "credential_reference": "B6A-owned secure-storage reference",
    "protocol_version": "B6A-owned protocol version"
  }
}
```

All B6A fields are optional references/strings. They are neither pairing
semantics nor authentication logic, and a plaintext credential field is not
part of this schema. B6A selects a secure credential store and owns the
meaning of its reference.

## Deployment topologies and Forge handoff

An Agent-only macOS host needs only this artifact and the current developer
user context; the Workspace Client and EP Server are optional. A same-host
Server plus Agent has no path or label conflict: Server data is under
`~/Library/Application Support/Engineering Platform Server`, while Agent data
is under `~/Library/Application Support/Engineering Platform/Project Agent`.
Localhost does not create an authentication exception.

Forge Platform can compose this role independently of **Engineering Platform
Server**. Its installer needs: artifact role identity (`project-agent`), macOS
support, a per-user installation context, the six lifecycle primitives above,
the service status values, optional B6A config inputs, and the preservation
boundaries. Forge owns role selection, distribution, update UX and cross-role
composition; B6B does not implement Forge installation. Windows Services and
Linux systemd are expressly deferred.
