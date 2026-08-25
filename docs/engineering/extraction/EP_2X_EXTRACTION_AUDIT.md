# Engineering Platform 2.x extraction audit

**Baseline commit:** `05583f229ad878c5c06f264a661b4d92eb33b128`

**Method:** static, read-only review of the manifest paths and their direct
imports, command targets, documentation and workflow references.

## Import dependency audit

| Class | Current evidence | Extraction result |
| --- | --- | --- |
| STANDARD_LIBRARY | `pathlib`, `json`, `sqlite3`, `subprocess`, `argparse`, `http.server`, `threading` | portable, subject to platform behavior review |
| EP_INTERNAL | relative imports across `tools.engineering` modules and `contracts` | move together under a neutral package namespace |
| EXTERNAL_PACKAGE | no required EP runtime package is declared; browser tests use Node/Playwright tooling | preserve as browser/test-only package decision |
| DJCONNECT_RUNTIME | repository state, prompt and release files; Home Assistant developer-host bootstrap | extraction-blocking implicit consumer boundary |
| HOME_ASSISTANT | no direct `homeassistant` import in EP product Python modules inspected | no direct runtime blocker found |
| REPOSITORY_LOCAL_SUPPORT | `.engineering`, repo-root documents, local Inbox/report paths, Git/GitHub CLI | extraction-blocking until explicit project/data-root contracts |
| OPTIONAL_TOOLING | Codex CLI, `gh`, launchd, Node/Playwright, shell tools | provider/platform integration boundary |
| TEST_ONLY | `unittest`, browser fixtures/snapshots, temporary repositories | package-test migration work |

Material import blockers are `EB-001` through `EB-004` below. The audit does
not refactor them.

## Product/repository coupling audit

| Occurrence class | Evidence | Classification |
| --- | --- | --- |
| `djconnect` launchd labels and dashboard build key | dashboard assets; onboarding; host runner scripts | EXTRACTION_BLOCKING_PRODUCT_COUPLING |
| DJConnect repository status, prompt records and release policy reads | execution/reporting/preflight modules | LEGITIMATE_CONSUMER_ADAPTER |
| Home Assistant developer-host setup | onboarding and `scripts/runner` | LEGITIMATE_CONSUMER_ADAPTER |
| Dashboard five-language display wording | locale asset | LOCALISATION_COPY |
| DJConnect names in EP historical reports | `docs/history` | DOCUMENTATION_ONLY / excluded |
| tests using DJConnect repo fixtures | `tests/engineering` | TEST_FIXTURE |

## Filesystem and CWD audit

| Assumption | Current evidence | Future concern |
| --- | --- | --- |
| repository root passed/resolved for host commands | execution host, dashboard, bootstrap | REGISTERED_PROJECT_PATH |
| `.engineering` holds database, status, reports, Inbox and runtime state | storage, watcher, dashboard, onboarding | INSTALLATION_DATA_ROOT / EXTRACTION_BLOCKER |
| relative `tools/engineering` module and asset locations | commands, dashboard installation and tests | PACKAGE_RESOURCE |
| repository-local Inbox and report paths | watcher, lifecycle, prompt history | CONSUMER_ADAPTER |
| LaunchAgent and user-specific paths | dashboard/onboarding/runner scripts | MIGRATION_TOOLING |
| temporary test roots | engineering tests | TEST_FIXTURE |

No user-specific absolute path is stored in the manifest or this audit.

## Public entry points and runtime naming

| Entry point / name | Target | Classification |
| --- | --- | --- |
| `python -m tools.engineering` | `tools.engineering.__main__` → execution host | NEUTRAL_EP_ENTRY_POINT, but namespace needs Phase 3 rename |
| `tools/engineering/engineering-execution-host` | execution host launcher | NEUTRAL_EP_ENTRY_POINT |
| `tools/engineering/dj-engineering-dashboard` | dashboard launcher | NEEDS_PHASE_3_PRODUCT_RENAME |
| dashboard `install`, `doctor`, server commands | `dashboard.py` | NEUTRAL_EP_ENTRY_POINT with repository/data-root coupling |
| Inbox watcher command/service | `inbox_watcher.py` | NEUTRAL_EP_ENTRY_POINT with consumer route coupling |
| `onboarding/dev_onboarding_macos.sh` and runner bootstrap | DJConnect host provisioning | DJCONNECT_CONSUMER_ADAPTER / LOCAL_BOOTSTRAP |
| `com.djconnect.*` LaunchAgent labels | dashboard/runner desired state | CONSUMER_ADAPTER_ONLY; LEGACY_COMPATIBILITY_REQUIRED |
| `.engineering` and dashboard client-state keys | storage/dashboard assets | NEEDS_PHASE_1_CONTRACT_DECISION |

## Test, workflow and documentation ownership

- `tests/engineering/*.py`: `EP_PRODUCT_TEST`, except repository-layout cases
  (`SHARED_BOUNDARY_TEST`) and browser files/snapshots (`EP_BROWSER_TEST`).
- Storage/contract/lifecycle tests are `EP_MIGRATION_TEST` or `EP_CONTRACT_TEST`
  candidates; HA integration tests outside `tests/engineering` are
  `DJCONNECT_PRODUCT_TEST`.
- `engineering-platform-validation.yml` is `EP_OWNED_MOVE`; golden,
  trusted-delivery and owner-authorization workflows are
  `SHARED_CURRENTLY_NEEDS_SPLIT`; product/release workflows are
  `DJCONNECT_RETAINED` or `CONSUMER_COMPATIBILITY`.
- The migration plan, consumer contract, qualification registry, protocol,
  Operations Console design system and this baseline are EP documentation.
  Bootstrap, onboarding and current-state records are DJConnect consumer
  guidance/adapters. Prompt History and reports are immutable consumer/project
  evidence and are excluded.

## Consumer adapter and shared-contract boundary

| Candidate | Responsibility | Expected owner |
| --- | --- | --- |
| onboarding and `scripts/runner` | DJConnect developer-host and launchd provisioning | DJCONNECT |
| repository state/preflight adapters | establish current consumer repository truth | DJCONNECT, via future EP adapter boundary |
| delivery/release workflows | DJConnect artifact and release authority | DJCONNECT |
| `tools/engineering/contracts` | run-context, allowed-action, policy-decision projection | EP package |
| producer submission envelope | admission metadata contract | SHARED_CONTRACT; later version/transport decision |
| consumer contract document | project registration and compatibility principles | EP package or small EP client package; architecture decision required |

## Package dependency inventory

| Dependency group | Classification |
| --- | --- |
| Python standard library and SQLite | required runtime |
| Codex CLI, Git, GitHub CLI | optional provider integration |
| Node, Playwright, package-lock dependencies | browser/test-only |
| PyYAML, coverage, pytest | development-only |
| Home Assistant onboarding and release scripts | DJConnect-specific |
| launchd, macOS security/Tailscale integrations | platform-specific |
| future wheel metadata | unknown until Phase 3 packaging |

## Extraction blocker matrix

| ID | Category | Evidence | Severity | Required phase | Blocks extraction? |
| --- | --- | --- | --- | --- | --- |
| EB-001 | FILESYSTEM_COUPLING | `.engineering` is a repository-local database/runtime root | P1 | 2 | YES |
| EB-002 | CWD_COUPLING | host, watcher and dashboard resolve repository-relative source/assets | P1 | 3 | YES |
| EB-003 | CONSUMER_COUPLING | repository status/prompt/release governance is read by EP lifecycle | P1 | 1 | YES |
| EB-004 | RUNTIME_NAME | `tools.engineering`, `dj-engineering-dashboard`, `com.djconnect.*` | P1 | 3 | YES |
| EB-005 | ENTRY_POINT | current commands mix neutral EP operation and DJConnect bootstrap | P2 | 1 | NO |
| EB-006 | TEST_COUPLING | engineering tests use checkout-layout fixtures | P2 | 3 | NO |
| EB-007 | WORKFLOW_COUPLING | qualification and delivery workflows share DJConnect policy | P2 | 3 | NO |
| EB-008 | PACKAGE_DEPENDENCY | standalone runtime/package metadata does not yet exist | P2 | 3 | NO |
| EB-009 | HISTORY/PROVENANCE | immutable Prompt History must remain consumer-owned | P2 | 3 | NO |

There is no P0 blocker: the baseline can be captured truthfully. There are four
P1 blockers (`EB-001`–`EB-004`), deliberately left unresolved in this increment.
