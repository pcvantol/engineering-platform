# Phase B7-0 — legacy local-installation retirement receipt

**Recorded:** 2026-09-01T15:54:49Z  
**Host scope:** local macOS user domain (`gui/501`) only  
**Canonical source revision:** `engineering-platform` `main` checkout at
`5b4f1ad861b7cf7168476109f5e12c6281acba33`

## Authority confirmation

The current canonical migration runbook and ADR-0026 were reviewed before
host mutation.  They confirm that LEGACY is suspended and historical-only,
that no legacy operational data is migrated into standalone CENTRAL, that the
future CENTRAL is a fresh official schema-41 store with newly established
identities, and that native Codex CLI remains the development executor until
`STANDALONE_EP_VERIFIED`.

## Inventory and disposition

| Surface discovered | Classification | Disposition |
| --- | --- | --- |
| `com.djconnect.engineering-dashboard-relay` | REMOVE_SERVICE | Booted out; plist relocated from `~/Library/LaunchAgents`. |
| `com.djconnect.engineering-dashboard` | REMOVE_SERVICE | Booted out; plist relocated from `~/Library/LaunchAgents`. |
| `com.djconnect.engineering-inbox` | REMOVE_SERVICE | Booted out; plist relocated from `~/Library/LaunchAgents`. |
| `com.djconnect.engineering-local-api` | REMOVE_SERVICE | Booted out; plist relocated from `~/Library/LaunchAgents`. |
| Dashboard, Local API, and dashboard-relay processes (PIDs 67775, 67751, 67790) | REMOVE_RUNTIME | Stopped by the service bootout; no matching process or listeners on ports 8765/8766 remained. |
| `~/Library/Application Support/Engineering Platform/engineering.db` | PRESERVE_HISTORY | Contaminated pre-clean-slate CENTRAL forensic DB, relocated from the future runtime default to the historical-evidence archive. |
| `~/Library/Application Support/Engineering Platform/backups/legacy-schema40-20260831T123201Z-41feb31e-2e25-42c4-bca1-bbfc97dde6f4.db` | PRESERVE_HISTORY | Historical legacy backup retained in the historical-evidence archive. |
| `~/Documents/GitHub/djconnect/.git/engineering-platform/engineering.db` | PRESERVE_HISTORY | Canonical suspended legacy schema-40 store; left in its provenance path and marked read-only. |
| Existing migration receipts and authority-pointer record | PRESERVE_HISTORY | Relocated with the historical-evidence archive; not altered. |
| `~/Documents/GitHub/djconnect/.engineering` source-checkout symlink and `bin/engineering-dashboard-relay` | REMOVE_POINTER | Relocated from their active source-checkout paths into the archive. |
| `~/.local/share/engineering-platform/codex-cli` and its now-empty parent runtime root | REMOVE_RUNTIME | Relocated from the active local-share path into the archive. |

`UNRESOLVED = 0`.  The unrelated DJConnect LaunchAgents (including verification
artifact cleanup and CI tooling), product configuration, credentials, Home
Assistant state, and component repositories were not touched.

## Preserved database fingerprint

Canonical preserved legacy database:

`/Users/pcvantol/Documents/GitHub/djconnect/.git/engineering-platform/engineering.db`

| Property | Value |
| --- | --- |
| File identity | `60189782` |
| Size | `31,666,176` bytes |
| Modified time | `2026-08-31T14:32:01Z` |
| Product schema | `40`, recorded in the canonical migration receipt; SQLite `PRAGMA user_version` is `0` |
| SQLite integrity | `ok` before and after retirement |
| SHA-256 before | `b639722042b3c2100483dd9505920a4b3db1d86773bb36fce0a6b29aaa111891` |
| SHA-256 after | `b639722042b3c2100483dd9505920a4b3db1d86773bb36fce0a6b29aaa111891` |

The file is read-only and is classified `LEGACY_HISTORY_ONLY`.  It is not a
standalone seed, is not automatically reachable by the new standalone runtime,
and was not migrated, copied into a new CENTRAL store, reconciled, or otherwise
modified.

The historical archive is:

`/Users/pcvantol/Library/Application Support/Engineering Platform Historical Evidence/retired-b7-0-20260901T000000Z`

It retains the old contaminated CENTRAL forensic DB (SHA-256
`a629482106d4f16621ddfc4309dea14b789ac4aefdc756f531cce1c84e76963f`), the
legacy backup (SHA-256
`b32cb0025e919a8999b738eab927bb72628873bd4600dbca916c7e184eaaa9ca`), and
the historical receipts.  Those files are no longer under the future default
runtime path.

## Final clean-host canary

| Check | Result |
| --- | --- |
| Legacy EP launchd jobs loaded | `0` |
| Legacy EP LaunchAgent plist definitions in active launch paths | `0` |
| Legacy EP runtime processes | `0` |
| Legacy runtime listeners, PID files, locks, or sockets | `0` discovered |
| Active source-checkout runtime pointers | `0` |
| Mutable legacy runtime configuration affecting a new EP install | `0` |
| New standalone EP Server installed | `NO` |
| `com.engineeringplatform.project-agent` installed/running | `NO` |
| New clean CENTRAL database or installation identity created | `NO` |
| Preserved legacy history DB | `YES` |
| Legacy DB checksum unchanged | `YES` |
| Production application execution performed | `NO` |

**Final classification:** `HOST_READY_FOR_CLEAN_EP_INSTALL`

This phase stops at the clean baseline.  It does not install, activate, pair,
or register the standalone Server or Project Agent.
