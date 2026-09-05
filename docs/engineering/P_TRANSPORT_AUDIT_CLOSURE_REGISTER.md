# P-TRANSPORT audit-closure register

**Scope:** exact-head technical closure before human UI review.  This register
turns the P-TRANSPORT consequence audit into normative, testable work; it is
not a roadmap.

| ID | Severity | Root cause / violated invariant | Repair and evidence | Status |
| --- | --- | --- | --- | --- |
| AC-01 | critical | Console exposed component restart without a Server-owned lifecycle operation. `VISIBLE_RESTART_WITHOUT_REAL_ROUTE=0` | Canonical model now marks every component `restart_supported=false`; real Console projections cannot render a false operation. | QUALIFIED — focused Server/browser regression, fresh candidate and exact-head hosted validation PASS at `23c08d2200b56d237ff6afd4f254a98c54a85f9c`. |
| AC-02 | critical | A published File Inbox executable could claim an Inbox outside EP Server. `STANDALONE_FILE_INBOX_EXECUTABLE=0` | Removed the package entry point and module CLI entry; File Inbox is constructed only by `server.serve`. | QUALIFIED — installed-wheel absence and exact-head installed ingress matrix PASS at `23c08d2200b56d237ff6afd4f254a98c54a85f9c`. |
| AC-03 | high | A live child without admission capability was projected healthy. `FILE_INBOX_READY_IMPLIES_SUBMISSION_CAPABLE=TRUE` | Heartbeat differentiates `READY` from `RUNNING_NOT_READY`; platform projection maps the latter to `FILE_INGRESS_NOT_READY`. | QUALIFIED — negative readiness projection, fresh candidate and exact-head hosted validation PASS at `23c08d2200b56d237ff6afd4f254a98c54a85f9c`. |
| AC-04 | critical | One external project credential could not safely represent multi-project File Inbox authority. | Canonical security decision: Server-owned `FILE_INBOX` principal invokes `request_from_mapping` and `submission_service.submit` in-process. It bypasses only external caller authentication. Dependabot now retries a transient CENTRAL SQLite startup lock within one second instead of deferring another independently bound project for the normal scan interval. Exact-head installed 3×2 ingress matrix passed, including `DEPENDABOT_MULTI_PROJECT_BINDING`. | QUALIFIED — focused transient-lock regression, rebuilt candidate and exact-head hosted validation PASS at `382cecfe5d0dd59e9e278f725efb1f971a0018e3`. |
| AC-05 | high | Component consumers duplicated identity inventories. `PLATFORM_COMPONENT_MODEL_COUNT=1` | `platform_components.py` is the single supported identity source. The bounded retired alias input set is denied before project resolution; it cannot select, write, route, configure, restart or own lifecycle. All new component-log writers reject noncanonical identities. Fresh-candidate suite, coverage and exact-head hosted validation pass. | QUALIFIED — `655052adcb7a61ad45e2f107793a6273fd93da78`. |
| AC-06 | critical | The legacy Dashboard direct wrapper and historical configuration runtime authority had remained in the supported Console chain. | Server now calls the Console document renderer directly by its Server-owned semantic name; the active Console module no longer imports or reads `historical_dashboard_configuration`. Structural guards reject reintroduction; installed ingress, route, browser and exact-head validation pass. | QUALIFIED — `f1555a88edf206bf51362a6a5593af440ca9e9de`. |
| AC-07 | high | CENTRAL logging had a Server-bound checkout-file fallback. `REPOSITORY_LOCAL_COMPONENT_LOG_FALLBACK=0` | CENTRAL is the sole durable operational log authority. The local Execution Host Codex diagnostic writer/readers are retired; CENTRAL failure is bounded stderr only; LaunchAgent output is discarded rather than persisted. Static guard, fresh candidate (1156 tests/coverage) and exact-head hosted validation pass. | QUALIFIED — `af0cc0705de07a8700ef9d4e522d62fd87c1073b`. |
| AC-08 | high | Qualification lacked CLI invalid-Genesis, complete Human negatives and port isolation. | Installed matrix now uses OS-assigned ports and includes the missing negative cases. | FIXED — installed matrix PASS |
| AC-09 | high | Human receipt lacked complete audit-chain provenance. | Schema-52 makes `ep_receipt_run_provenance` the sole immutable CENTRAL receipt→run authority. It has one submission, one run, one project/repository scope and one active installation; SQLite triggers reject scope substitution, installation substitution, update and delete. Claim creation writes the binding in the same transaction; replay requires an exact current-installation binding; schema-51 imports backfill only verified dispatches and fail closed on incomplete imports. The installed Human Intent MANAGED/GENESIS matrix and physical replay prove the binding is present and unchanged. | QUALIFIED — fresh candidate: 1160 tests; 107 production modules at ≥80.20%; installed ingress matrix and hosted technical checks PASS at `965df37a8191a6e585d1846e0e9068f9142bfead`. |
| AC-10 | high | Localization guard omitted some Server-generated surface and stale watcher copy. | The Server-owned no-project title, explanation and selector use five-locale keys. Active status copy uses `Execution status`, `Lifecycle Worker`, `Server Relay` and `File Inbox ingress`; static guards reject stale active terminology and placeholder drift. CENTRAL-database mutation feedback uses catalog keys through the read-only Console translation bridge. Workspace unavailability is neutral Server data and GPU state is a machine code; the browser alone localizes both. Finder, local Inbox-location and runtime-directory actions are absent from supported browser code and rejected by retained routes with `410`. | QUALIFIED — fresh candidate (1162 tests), five-locale Console/browser matrix and exact-head hosted validation PASS at `23c08d2200b56d237ff6afd4f254a98c54a85f9c`. |
| AC-11 | high | Presentation-level Console/log compatibility transforms concealed legacy source structure. | Browser logging no longer retains separate Inbox/Dashboard state, and both supported Server document paths consume canonical log markup directly. The unused parallel Console document, scoped document transform, workspace-identity rewrite and log-surface transform are physically removed with absence guards. `_console_project_boundary` is the sole retained, explicitly classified Server scope adapter (LR-14), with no authority beyond an already CENTRAL-validated project header. The direct Dashboard wrapper remains separately tracked by AC-06. | FIXED — focused Server regression PASS |
| AC-12 | high | A component restart acknowledged `launchctl kickstart` without proving that the owned LaunchAgent process was running afterwards. | The only restartable component (`dashboard_relay`) records requested/completed/failed CENTRAL events and fails closed unless its canonical lifecycle owner becomes active within a bounded interval. On the installed Server, `relay-install` rebuilt the installation-owned binary and changed the running LaunchAgent PID from `90334` to `14411`; launchd reported `running`, the Tailnet listener returned on `100.108.178.11:8765`, and loopback health remained ready. A live restart request for non-restartable `operations_console` returned `409 COMPONENT_RESTART_UNAVAILABLE` without changing the relay PID. | QUALIFIED — installed positive, negative and restart canaries at `b50b4d37904937c86c796fab62ec26a69aebd8e4`. |

`UNRESOLVED_AUDIT_FINDINGS` remains non-zero until every `IN_PROGRESS` item
has a real product-boundary canary and exact-head installed-wheel evidence.

This register is therefore not evidence for human UI review or Owner
Authorization: remaining installed qualification and retirement items are
listed below.

## Coverage-contract handoff — 2026-09-05

The agreed quality contract is **every production module at least 80.20% branch
coverage**, measured by rebuilding the isolated candidate from the current
worktree and then running the complete Python suite with
`coverage run --branch --source=engineering_platform -m unittest discover -s tests/engineering -q`.

Do not use a candidate installed before source edits: it imports a stale wheel
copy and is not exact-worktree evidence. Rebuild with
`/private/tmp/ep-production-clean-current/bin/pip install --force-reinstall .`
before each final measurement.

The latest rebuilt-candidate run at exact head
`382cecfe5d0dd59e9e278f725efb1f971a0018e3` passed **1163 tests**. Complete
production branch coverage has **zero** modules below 80.20%; the minimum is
`execution_host.py` at **80.28%**. The installed 3×2 ingress matrix
and the hosted `validate` workflow were rerun at that same head and passed.

```ini
CANDIDATE_INSTALLATION_CLEAN = TRUE
FULL_TEST_SUITE = PASS (1147)
PRODUCTION_MODULE_COVERAGE_GATE = PASS
PRODUCTION_MODULES_BELOW_80_20 = 0
MINIMUM_PRODUCTION_MODULE_COVERAGE = 80.26% (central_store_migration.py)
AC04_DEPENDABOT_MULTI_PROJECT_BINDING = PASS
P_TRANSPORT_INGRESS_REGRESSION = PASS
HOSTED_CHECKS_EXACT_HEAD = TRUE
```

During this increment, a real production defect was found and fixed in
`legacy_inbox_migration.py`: a skipped historical symlink left its source
directory non-empty, after which an unconditional `rmdir()` aborted the safe
migration. The migration now preserves skipped evidence and only removes an
empty directory. Focused historical configuration/migration tests pass.

## Complete audit carry-forward — legacy retirement and technical closure

This is the complete handoff inventory for the audits performed before and
during the coverage increment.  A prior LR label is not closure evidence when
current source, installed-candidate, or mutation evidence contradicts it.

### Measurement freshness

**Last authoritative candidate measurement:** rebuilt from exact head
`382cecfe5d0dd59e9e278f725efb1f971a0018e3`; **1163 Python tests passed** and
every production module met the 80.20% branch-coverage contract. The minimum
was `execution_host.py` at **80.28%**. The hosted `validate` workflow,
including the installed ingress matrix, passed at that exact head.

**Current governance state:** `TECHNICAL_CLOSURE=BLOCKED`,
`HUMAN_UI_REVIEW=NOT_READY`, `OWNER_AUTHORIZATION=NOT_REQUESTED`, and
`MERGE=PROHIBITED`.

| Area | Current finding | Evidence already observed | Closure criterion |
| --- | --- | --- | --- |
| Direct Dashboard retirement | **QUALIFIED** | Server has no `_dashboard_html` reference; supported Console rendering uses `render_console_document`, and the active Console module has no historical Dashboard-configuration import or read. Exact-head guards, browser checks, installed ingress and validation pass. | Preserve the structural regression guard. |
| Legacy Inbox / watcher removal | **QUALIFIED** | The fresh installed ingress matrix proves the wheel has no standalone watcher module or executable; the Server-owned File Inbox is the only ingress adapter. Historical migration input and retired `410` routes remain non-operational by construction. | Preserve installed-wheel and retained-route guards. |
| File Inbox ingress | **QUALIFIED** | Exact-head installed ingress matrix passed, including `DEPENDABOT_MULTI_PROJECT_BINDING`, HTTP/API, CLI, File Inbox, Human Intent, replay, negative-ingress and storage-authority canaries. | Preserve the qualified contract under future ingress changes. |
| Host Admin boundary | **QUALIFIED** | Fresh-candidate registry, containment, symlink-escape, bounded inventory, audit and removed-mutation-gate tests pass. No UI or route can advertise the retired worktree/lock mutation authority. | Preserve the explicit registered-target and audit gates. |
| Relay ownership | **QUALIFIED** | The installed Server rebuilt the relay only under its data root, rendered the one canonical LaunchAgent, and launchd started that binary on the Tailnet listener. The focused installation/negative-checkout/route suite passed. | Preserve the Server-owned binary path and canonical LaunchAgent label. |
| Route ownership | **QUALIFIED** | The route-ownership guard reported zero platform project delegation, checkout dependency and ambiguous ownership. The live unsupported-component restart route returned the bounded Server rejection rather than falling through to project or Dashboard authority. | Preserve the route-ownership guard and retained `410` retirement boundaries. |
| Configuration authority | **QUALIFIED** | The migration matrix is complete across installation, Server, project and historical settings. Every supported Console mutation is persisted through CENTRAL and emits a structured `configuration_changed` event with scope, key, previous value and new value; historical dashboard/Inbox settings have no operational writer. | Preserve the central configuration and audit-log tests. |
| Local data authority | **QUALIFIED** | The fresh candidate passed the local-data retirement matrix and CENTRAL-core authority guard. No execution, queue, project, browser, checkout or root-local fallback is a supported authority; retained historical artifacts are input-only. | Preserve the local-data and authority guards. |
| Migration to CENTRAL | **QUALIFIED** | The rebuilt installed candidate ran a disposable full cutover canary: source inventory/identity, freeze, quiescence, schema and copy equivalence, durable receipt, one CENTRAL authority, pre-write rollback, and post-write rollback denial. Malformed, stale, conflicting and interrupted sources fail closed. The historical Inbox archive mover remains explicitly historical-only. | Preserve `p_central_store_installed_canary.py` and the migration contract suite. |
| Component aliases and logging | **QUALIFIED** | AC-05 and AC-07 establish the single canonical component inventory, reject every retired alias before resolution, and retain CENTRAL as the only durable operational log authority. | Preserve alias-retirement and logging guards. |
| Armed repair/restart | **QUALIFIED** | AC-01 prevents false restart advertisement; AC-12 now has installed positive, negative and restart evidence for the one supported lifecycle owner. | Preserve the bounded lifecycle postcondition and CENTRAL audit events. |
| Localization / Console | **QUALIFIED** | Browser and Server Console surfaces use the five-locale catalog for visible unavailable/mutation feedback; supported browser code contains no Finder, local Inbox-location or watcher action. Retained historical endpoints are explicit `410` rejections only. | Preserve the localization and retired-action guards. |
| Human receipt / provenance | **QUALIFIED** | AC-09 schema-52 provides an immutable, installation-bound receipt→run relation; direct SQLite canaries cover substitution, scope mismatch, rewrite rejection and schema-51 backfill/fail-closed import. Installed Human Intent MANAGED/GENESIS and physical replay verify `receipt_run_provenance=PRESENT`. | Preserve the invariant under future submission/lifecycle changes. |
| LR-09 isolation | **UNRESOLVED, isolated** | Dependabot successor is explicitly out of scope; it must not retain unrelated legacy authority. | `LR09_CAPABILITY_LOSS=0` and `UNRELATED_LEGACY_RUNTIME_RETAINED_FOR_LR09=0`, without successor implementation. |
| Production coverage | **QUALIFIED** | Rebuilt exact-head candidate: 1147 tests; zero modules below 80.20%; minimum `central_store_migration.py` at 80.26%. | Preserve the contract under future production changes. |

No row above is evidence for human UI review, Owner Authorization, merge, or
final technical closure until its stated criterion is demonstrated on the
exact candidate head.

### Final retirement re-audit — 2026-09-05

The direct Dashboard-wrapper/configuration dependency is closed: the Server
has no `_dashboard_html` call, and its supported Console module neither imports
nor reads `historical_dashboard_configuration`. The historical checkout-bound
runtime-directory action remains explicitly unreachable (`410`). The
local-data, component-model, route-ownership and new Dashboard-retirement
guards pass, as do the exact-head installed ingress, browser and hosted
validation checks. Final retirement remains pending only for the other rows in
this register; AC-06 is no longer its blocker.

### AC-05 component alias authority audit — 2026-09-05

The sole supported component inventory is `platform_components.PLATFORM_COMPONENTS`.
Its canonical identifiers are: `ep_server`, `platform_database`,
`lifecycle_worker`, `operations_console`, `dashboard_relay`, `http_ingress`,
`cli_ingress`, `file_inbox_ingress`, and `dependabot_producer`. The same model
provides display-key, lifecycle-label, route-pattern, restart-capability and
log-identity data; no Server, Console, lifecycle or logging-specific identity
map is supported.

The bounded retired input aliases are `dashboard`, `dashboard_service`,
`dashboard_watcher`, `finder`, `inbox`, `inbox_service`, `inbox_watcher` and
`watcher`. They are not normalized. Component detail/restart routes reject each
with `410 LEGACY_COMPONENT_AUTHORITY_RETIRED` before project resolution; legacy
log routes remain `410`; the CENTRAL log writer rejects every noncanonical
component ID. The Execution Host now records under `lifecycle_worker`, rather
than creating an ad-hoc `execution-host` log identity.

Remaining legacy lexical references are classified as one of: Console product
presentation (`assets/dashboard.*`, Console document and localization keys),
immutable historical evidence reads (`server_console_services.py` and
`prompt_history.py`), one-shot archive/migration input
(`legacy_inbox_migration.py` and `central_store_migration.py`), or negative
regression fixtures. None supplies active component selection, persistence,
lifecycle, configuration, route, dispatch or log authority. The executable
guard `tools/qualification/component_alias_retirement_guard.py` verifies this
authority boundary in addition to the route and writer matrices.

Fresh candidate evidence: 1154 Python tests passed; 107 production modules;
zero modules below 80.20% branch coverage; minimum 80.26%
(`central_store_migration.py`). Exact-head hosted validation passed at
`655052adcb7a61ad45e2f107793a6273fd93da78`; AC-05 is qualified.

### AC-07 logging retirement — 2026-09-05

The canonical durable operational log authority is the CENTRAL
`engineering_component_logs` table, through `component_logging.component_logger`
and `log_event`. CENTRAL failure is bounded stderr only; it cannot create a
checkout-local persistent fallback. The former Execution Host
`.engineering/logs/codex/<run>.log` writer now records a redacted,
run-bound `codex_cli_diagnostic` under canonical `lifecycle_worker`; all three
legacy Console file readers are retired. The Project Agent LaunchAgent now
discards its unpaired process output to `/dev/null`, rather than retaining
per-agent stdout/stderr files.

Legacy workspace logs are migration/forensic input only; local log files are
not imported into current component logs or exposed as supported operational
truth. The AC-07 static guard and focused logging matrix pass. Fresh-candidate
validation passed with 1156 tests and zero production modules below 80.20%; all
hosted checks passed at `af0cc0705de07a8700ef9d4e522d62fd87c1073b`.

## Legacy branch successor reconciliation

```ini
LEGACY_BRANCH_SUCCESSOR_RECONCILIATION = PASS
ACTIVE_INTEGRATION_BRANCH_COUNT = 1
WHOLESALE_LEGACY_MERGE_REQUIRED = FALSE
TARGETED_MISSING_INVARIANT_COUNT = 0
FORENSIC_BRANCHES_ARE_RUNTIME_AUTHORITY = FALSE
```

`codex/phase-p-transport` remains the sole active integration branch.  This
records successor reconciliation only; it does not change any outstanding
technical-closure, installed-candidate, ingress, migration, or coverage gate.
