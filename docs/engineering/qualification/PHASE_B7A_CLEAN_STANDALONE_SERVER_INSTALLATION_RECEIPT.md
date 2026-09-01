# Phase B7A — clean standalone Server installation receipt

**Recorded:** 2026-09-01

**Base/source revision:** `e32df761f03a7f581acd2859010b34c3a49f21d8`

**Worktree:** `/Users/pcvantol/Documents/GitHub/engineering-platform-b7a`

**Branch:** `codex/b7a-clean-server-install`

## Clean-host and historical-evidence revalidation

Before installation, no legacy EP LaunchAgent, legacy EP process, listener on
ports 8765/8766, active legacy source-checkout pointer, standalone Server,
Project Agent, or new CENTRAL database was present. The preserved DB remained
read-only historical evidence:

`/Users/pcvantol/Documents/GitHub/djconnect/.git/engineering-platform/engineering.db`

Its SHA-256 was the required
`b639722042b3c2100483dd9505920a4b3db1d86773bb36fce0a6b29aaa111891`
before and after B7A, and `PRAGMA integrity_check` returned `ok`. It was not
opened by the Server, migrated, seeded, copied, or imported.

## Installed artifact and data root

| Property | Value |
| --- | --- |
| Artifact | `engineering_platform-2.0.0-py3-none-any.whl` |
| Artifact SHA-256 | `5684f9631b4384c680dbd001c93fc0c19a6402603f7594d8e27364c5db05ecff` |
| Installed package | `engineering-platform 2.0.0` |
| Installed runtime entrypoint | `/Users/pcvantol/Library/Application Support/Engineering Platform Server Runtime/venv/bin/engineering-platform-server` |
| Installed package location | `/Users/pcvantol/Library/Application Support/Engineering Platform Server Runtime/venv/lib/python3.14/site-packages/engineering_platform` |
| Data root | `/Users/pcvantol/Library/Application Support/Engineering Platform Server` |
| CENTRAL DB | `/Users/pcvantol/Library/Application Support/Engineering Platform Server/engineering.db` |
| Installation identity | `1ab95e79-a98a-485e-8c97-b7f391e381ac` |
| CENTRAL DB fingerprint | `79c4b70212c39d433440f7cc4765169da2b6a9cdd5967461ef90e7398a1de37e` |

The installed runtime was invoked from `/` with an empty inherited environment
apart from its installed virtual-environment path. Its imports resolved from
installed site-packages, not either source checkout.

## Official schema-41 qualification

The Server previously created an isolated schema-1 foundation store. B7A adds
a product-owned, fail-closed fresh schema-41 bootstrap. Its authority is
`engineering_schema_migrations`; readiness requires version 41, all required
tables and indexes, matching installation metadata, and SQLite integrity.

`PRAGMA integrity_check` returned `ok`; schema version was `41`; all required
credential/consumer-registration, project, execution/lease, Prompt History,
Agent-trust, installation, and immutable-control-provenance structures were
present. The new DB is mode `0600`; the data root and artifact-runtime parent
are mode `0700`; the service binds only `127.0.0.1`.

The valid fresh operational row counts were:

| Structure | Count |
| --- | ---: |
| Paired or registered Agents | 0 |
| Project registrations | 0 |
| Execution runs | 0 |
| Execution leases | 0 |
| Prompt History | 0 |
| Consumer credentials | 0 |
| Consumer registrations | 0 |

No legacy rows were imported. No Agent pairing/registration, DJConnect
attachment/registration, consumer credential provisioning, or production
engineering execution was performed.

## Lifecycle qualification

The installed Server initialized, started, answered `/healthz` and `/readyz`
with ready/healthy status, stopped, and restarted. After restart it used the
same DB and installation identity, retained schema 41 and empty state, and
again passed integrity and readiness. No persistent macOS LaunchAgent was
installed. Because current migration governance does not explicitly authorize
leaving a qualified candidate process running, it was stopped after
qualification. B8 can start the preserved installed candidate with:

```text
/Users/pcvantol/Library/Application Support/Engineering Platform Server Runtime/venv/bin/engineering-platform-server start --data-root "/Users/pcvantol/Library/Application Support/Engineering Platform Server"
```

It must then re-check health/readiness before it owns pairing, Agent
registration, and first project attachment.

**Final classification:** `B7A CLEAN SCHEMA-41 CENTRAL READY FOR B8`
