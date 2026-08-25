# Engineering Platform 2.x extraction baseline

**Status:** Phase 0 / Increment 1 control artifact

**Manifest:** `EP_2X_EXTRACTION_MANIFEST.json` (version `1`)

**Baseline source:** `pcvantol/djconnect` at `05583f229ad878c5c06f264a661b4d92eb33b128`
**Baseline date:** 2026-08-25

## Identity

| Identity | Canonical value | Evidence |
| --- | --- | --- |
| Engineering Platform | `2.0.0` | `tools/engineering/ENGINEERING_PLATFORM_VERSION.json` |
| Runner / watcher / Operations Console | `2.0.0` | Version manifest |
| Storage schema | `29` | Version manifest and `storage.py` |
| Consumer Contract | `1.0` | `tools/engineering/contracts/models.py` |
| Producer submission envelope | `1.0` | `tools/engineering/producer.py` |
| Bootstrap contract | `2026.12` | Version manifest |
| Supported locales | `en`, `nl`, `de`, `fr`, `es` | Dashboard locale resource and localization policy |
| Runtime identity | Python 3.12 target; stdlib-first Python package plus browser JS/CSS assets | `pyproject.toml`, source inventory |

The source commit is the immutable rollback reference. The current repository
release convention tags reconciled `main` commits as `internal-ha-<commit>`;
the matching existing tag is `internal-ha-05583f229ad878c5c06f264a661b4d92eb33b128`.
No extraction-specific tag was created: this managed transaction does not
authorize tagging or release actions, and Phase 0 exit approval remains later
governance work.

## Scope and no-extraction proof

This increment adds only this control baseline, its manifest/audit, and focused
test coverage. It does not create a repository, move EP source, rename a Python
namespace, change imports, migrate SQLite, establish an installation data root,
alter launchd, change the active writer or Inbox routing, introduce `project_id`,
an HTTP API, authentication, or retire commands. Prompt History and historical
reports are explicitly excluded and unchanged.

## Complete path classification

`EP_2X_EXTRACTION_MANIFEST.json` is the authoritative deterministic inventory.
Each candidate path has one classification from the closed vocabulary:
`EP_PRODUCT_SOURCE`, `EP_TEST`, `EP_DOCUMENTATION`, `EP_WORKFLOW`,
`EP_RELEASE_ASSET`, `CONSUMER_ADAPTER`, `DJCONNECT_RETAINED`,
`GENERATED_LOCAL_ONLY`, or `EXCLUDED`. Directory entries are used only where
their descendants share responsibility; exceptions are explicit.

EP product source consists of the runtime modules, contracts, dashboard and
five-language Operations Console assets in `tools/engineering`. EP tests are
in `tests/engineering`, including dashboard browser regression coverage.
The dashboard is EP-owned: Run table/mobile behaviour, sticky layout, status
refresh, approval projection, Inbox controls, exports, backups, telemetry,
retention safeguards, AI-capacity trends, worktree projection and persisted
configuration all remain in the EP product boundary. Current repository-
specific hosting and launch integration remains a consumer adapter until a
later contract decision.

## Reproduction and drift detection

Run from the repository root:

```sh
python3 scripts/engineering/audit_ep_extraction_baseline.py --check
python3 scripts/engineering/audit_ep_extraction_baseline.py --projection
```

The audit is read-only, repository-relative and deterministic. It fails for
duplicate, missing or unsafe paths (including POSIX and Windows absolute
paths), unknown classifications and malformed entries. The sorted projection
can be compared with a later commit to expose added/removed/classification-
changed candidate paths. The accompanying audit records import and filesystem
assumptions so future changes can also be reviewed for new extraction blockers.

## Readiness projection

| Evidence | Status |
| --- | --- |
| Baseline identity | COMPLETE |
| Path manifest | COMPLETE |
| Import audit | COMPLETE |
| Filesystem/CWD audit | COMPLETE |
| Entry-point audit | COMPLETE |
| Test ownership | COMPLETE |
| Workflow ownership | COMPLETE |
| Operations Console baseline | COMPLETE |
| Rollback reference | AVAILABLE |
| Phase 0 Increment 1 | PASS |

Phase 0 as a whole is not passed. Its roadmap exit still requires approved
baseline-tag/release governance and removal or bounded resolution of the
identified product, filesystem and consumer-coupling blockers.
