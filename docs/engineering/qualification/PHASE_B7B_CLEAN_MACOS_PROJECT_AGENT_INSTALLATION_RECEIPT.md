# Phase B7B — clean macOS Project Agent installation receipt

## Scope and isolation

- Worktree: `/Users/pcvantol/Documents/GitHub/engineering-platform-b7b`
- Branch: `codex/b7b-clean-project-agent-install`
- Main baseline after fetch: `e32df761f03a7f581acd2859010b34c3a49f21d8`
- Qualified artifact source: `b210d04af14e5a1a210ea9d765b1c434690c1990`

The pre-install check found no `com.engineeringplatform.project-agent`
LaunchAgent, no canonical Project Agent configuration/identity/cache/log root,
and no Project Agent runtime process.  This phase did not inspect, modify, or
migrate the legacy schema-40 database or CENTRAL forensic evidence.  A legacy
database checksum was not recomputed because no explicit canonical legacy
runtime root was supplied; filesystem discovery is intentionally forbidden.

## Installed artifact

| Field | Value |
| --- | --- |
| Component role | `project-agent` |
| Package | `engineering-platform` 2.0.0 |
| Wheel SHA-256 | `a81ad399e8b52d1e9d8d8be5b13ebe6469d9370467bdb8676c7385bf19c2c7ea` |
| Installed executable | `~/Library/Application Support/Engineering Platform/Project Agent/runtime/bin/engineering-project-agent` |
| LaunchAgent label | `com.engineeringplatform.project-agent` |
| LaunchAgent plist | `~/Library/LaunchAgents/com.engineeringplatform.project-agent.plist` |

The wheel was built from the isolated worktree and installed in a per-user
runtime.  The service plist and configuration contain no source-checkout path.

## Qualification outcome

- The Agent configuration and identity were newly generated.  Both are mode
  `0600`; their parent root is mode `0700`.
- `launchctl` reports the per-user service as running.  The plist is valid and
  its deterministic PATH is
  `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`.
- The service executes the installed artifact, has exactly one runtime process,
  and creates its canonical stdout/stderr log files.
- Capability discovery reported macOS/Darwin on arm64, with Git and Codex CLI
  available.  This was discovery only; no engineering work was run.
- The observed repository inventory is zero.
- No pairing configuration or paired Server credential exists, and no Server,
  CENTRAL, or DJConnect endpoint was configured.  The required terminal trust
  state is therefore **UNPAIRED**.
- A canonical stop reported `stopped`; reinstall/start reported `running`.
  Restart preserved the same local Agent identity and still left exactly one
  LaunchAgent process.  The automated lifecycle suite also covers the
  `misconfigured` status.

No logout or reboot was required: the validated per-user plist uses `RunAtLoad`
and the canonical `gui/<uid>` lifecycle.
