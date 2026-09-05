# P-TRANSPORT audit-closure register

**Scope:** exact-head technical closure before human UI review.  This register
turns the P-TRANSPORT consequence audit into normative, testable work; it is
not a roadmap.

| ID | Severity | Root cause / violated invariant | Repair and evidence | Status |
| --- | --- | --- | --- | --- |
| AC-01 | critical | Console exposed component restart without a Server-owned lifecycle operation. `VISIBLE_RESTART_WITHOUT_REAL_ROUTE=0` | Canonical model now marks every component `restart_supported=false`; real Console projections cannot render a false operation. | FIXED — focused Server/browser regression pending full gate |
| AC-02 | critical | A published File Inbox executable could claim an Inbox outside EP Server. `STANDALONE_FILE_INBOX_EXECUTABLE=0` | Removed the package entry point and module CLI entry; File Inbox is constructed only by `server.serve`. | FIXED — installed-wheel absence check pending full gate |
| AC-03 | high | A live child without admission capability was projected healthy. `FILE_INBOX_READY_IMPLIES_SUBMISSION_CAPABLE=TRUE` | Heartbeat differentiates `READY` from `RUNNING_NOT_READY`; platform projection maps the latter to `FILE_INGRESS_NOT_READY`. | FIXED — installed negative canary pending full gate |
| AC-04 | critical | One external project credential could not safely represent multi-project File Inbox authority. | Canonical security decision: Server-owned `FILE_INBOX` principal invokes `request_from_mapping` and `submission_service.submit` in-process. It bypasses only external caller authentication. | REGRESSION — 2026-09-05 installed ingress re-run failed `DEPENDABOT_MULTI_PROJECT_BINDING_FAILED`; requalify before closure. |
| AC-05 | high | Component consumers duplicated identity inventories. `PLATFORM_COMPONENT_MODEL_COUNT=1` | Route pattern, startup events and transport rendering derive from `platform_components`; browser logging now has one platform state and derives selectable component identities from that model. Remaining aliases/legacy inventory are tracked by AC-06. | IN_PROGRESS |
| AC-06 | critical | The legacy Dashboard direct wrapper remains packaged and has supported-looking root/worktree paths. `SUPPORTED_LEGACY_INBOX_WATCHER_RUNTIME=0` | `inbox_watcher.py` is absent, but the re-audit found `server.py → server_console_services._dashboard_html`, retained historical configuration writes and retained Finder helpers. Structural source guards are insufficient to prove physical retirement. | REOPENED BY EVIDENCE GAP |
| AC-07 | high | CENTRAL logging had a Server-bound checkout-file fallback. `REPOSITORY_LOCAL_COMPONENT_LOG_FALLBACK=0` | The Server-bound logger now emits only bounded stderr on CENTRAL failure; no local persistent fallback. All active writers use canonical `operations_console` or `file_inbox_ingress` identities. Historical reads/routes remain tied solely to the direct Dashboard-wrapper retirement tracked by AC-06. | IN_PROGRESS |
| AC-08 | high | Qualification lacked CLI invalid-Genesis, complete Human negatives and port isolation. | Installed matrix now uses OS-assigned ports and includes the missing negative cases. | FIXED — installed matrix PASS |
| AC-09 | high | Human receipt lacked complete audit-chain provenance. | Accepted receipt now records source/normalized digest, scope, requested mode, normalization method/version and submission identity. | IN_PROGRESS — run linkage and durable audit verification pending |
| AC-10 | high | Localization guard omits some Server-generated surface and stale watcher copy. | The Server-owned no-project title, explanation and selector now use five-locale keys; the broader Server-rendered surface audit remains in progress. | IN_PROGRESS |
| AC-11 | high | Presentation-level Console/log compatibility transforms concealed legacy source structure. | Browser logging no longer retains separate Inbox/Dashboard state, and both supported Server document paths consume canonical log markup directly. The unused parallel Console document, scoped document transform, workspace-identity rewrite and log-surface transform are physically removed with absence guards. `_console_project_boundary` is the sole retained, explicitly classified Server scope adapter (LR-14), with no authority beyond an already CENTRAL-validated project header. The direct Dashboard wrapper remains separately tracked by AC-06. | FIXED — focused Server regression PASS |
| AC-12 | high | Retroactive CENTRAL armed-repair end-to-end evidence is missing. | Recover contract, add installed positive/negative/restart gate. | IN_PROGRESS |

`UNRESOLVED_AUDIT_FINDINGS` remains non-zero until every `IN_PROGRESS` item
has a real product-boundary canary and exact-head installed-wheel evidence.

The 2026-09-05 re-audit also records AC-04 and AC-06 as blocking evidence
gaps. This register is therefore not evidence for human UI review or Owner
Authorization.

## Coverage-contract handoff — 2026-09-05

The agreed quality contract is **every production module at least 80.20% branch
coverage**, measured by rebuilding the isolated candidate from the current
worktree and then running the complete Python suite with
`coverage run --branch --source=engineering_platform -m unittest discover -s tests/engineering -q`.

Do not use a candidate installed before source edits: it imports a stale wheel
copy and is not exact-worktree evidence. Rebuild with
`/private/tmp/ep-production-clean-current/bin/pip install --force-reinstall .`
before each final measurement.

The latest rebuilt-candidate run passed **1092 tests**. It still has these
production modules below 80.20%, so this PR is not at the coverage gate and is
not eligible for human UI review:

| Module | Coverage | Remaining focus |
| --- | ---: | --- |
| project_topology.py | 71.79% | Agent attachment registration/conflict paths |
| provider_process_identity.py | 72.46% | process evidence failures and canonicalization |
| agent_state.py | 72.68% | checkpoint decode, StateStore atomicity and retention |
| provider_readiness.py | 75.00% | unavailable executable/process-error branches |
| parity_lifecycle_dispatcher.py | 75.09% | operator mutation denial/retry lifecycle |
| execution_host.py | 75.98% | supported orchestration failure/recovery branches |
| server_relay.py | 76.00% | build/render/install lifecycle failures |
| local_api_credentials.py | 76.09% | credential state and HTTP verification failures |
| validation_identity.py | 76.47% | malformed transport and shell-control command parsing |
| central_store_migration.py | 76.57% | migration precondition, rollback and verification paths |
| evidence_projection.py | 76.88% | missing/corrupt evidence projections |
| provider_interruption.py | 77.27% | terminal lease-release failure paths |
| local_api.py | 77.84% | authenticated loopback rejection paths |
| telemetry.py | 77.98% | malformed/absent telemetry evidence |
| storage.py | 79.05% | persistence, projection and migration fail-closed branches |
| __main__.py | 80.00% | installed module-entrypoint branch |

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

**Last authoritative candidate measurement:** rebuilt from the current
worktree at the time of measurement; **1092 Python tests passed**.  The table
below reports that measurement, not an inferred percentage from focused tests.
Subsequent uncommitted changes add targeted tests and the historical Inbox
symlink fix; they require a fresh candidate rebuild and full suite before any
row may move to `QUALIFIED`.

**Current governance state:** `TECHNICAL_CLOSURE=BLOCKED`,
`HUMAN_UI_REVIEW=NOT_READY`, `OWNER_AUTHORIZATION=NOT_REQUESTED`, and
`MERGE=PROHIBITED`.

| Area | Current finding | Evidence already observed | Closure criterion |
| --- | --- | --- | --- |
| Direct Dashboard retirement | **OPEN / contradictory evidence** | `server.py` still delegates to `server_console_services._dashboard_html`; historical Dashboard configuration services remain imported; the wrapper has supported-looking Console paths. | Remove the direct wrapper/runtime path or prove every remaining helper is presentation-only with permanent absence guards and installed-candidate proof. Do not report LR-02 as requalified until this exact evidence is resolved. |
| Legacy Inbox / watcher removal | **OPEN evidence gap** | `inbox_watcher.py` is absent, but historical Inbox configuration and wrapper-related paths remain. `legacy_inbox_migration.py` is historical-only; its symlink-retention defect is fixed. | Prove no supported watcher/InBox alias, local Inbox location, or wrapper route has execution/configuration authority. |
| File Inbox ingress | **REGRESSION** | Installed ingress rerun reported `DEPENDABOT_MULTI_PROJECT_BINDING_FAILED` (AC-04). | Diagnose/fix the exact multi-project binding failure and rerun the installed ingress matrix. |
| Host Admin boundary | **RETAINED, requalification required** | Registry/containment/mutation gates were previously designed; removed worktree/lock mutations must not be advertised by any Console route/UI. | Re-run target registry, containment, inventory, audit and mutation-gate tests against the installed candidate. |
| Relay ownership | **REQUALIFY** | Relay is Server-owned by design; source has Server relay lifecycle and no checkout launcher evidence was accepted. | Re-run installation-owned relay build/install/uninstall, route, and negative checkout-authority tests. |
| Route ownership | **OPEN evidence gap** | Console wrapper is still reached from `server.py`; no project fallback must be restored. | Route ownership guard and installed route canary show zero ambiguous ownership, zero project delegation for platform routes, and zero Dashboard route owner. |
| Configuration authority | **OPEN / contradictory evidence** | Historical Dashboard configuration module remains a Server Console dependency. | Central Server/Project/Installation setting inventory is complete; historical configuration has no supported write/read authority or runtime fallback. |
| Local data authority | **REQUALIFY** | Migration-only historical Inbox artifacts are retained; CENTRAL-only authority was the intended model. | Re-run local-data retirement matrix and prove no execution, queue, project, browser, checkout, or root-local authority remains. |
| Migration to CENTRAL | **OPEN** | `central_store_migration.py` remains below the coverage contract (76.57%); earlier migration audit evidence includes abort/pre-handoff and historical-source paths, but not a complete exact-head cutover qualification. The historical Inbox archive mover is not a CENTRAL authority migration and must remain classified as historical-only. | Inventory each legacy source; prove bounded source identity, quiescence/admission freeze, schema compatibility, copy/import verification, durable receipt, rollback before cutover, and CENTRAL-only authority after cutover. Run positive, stale-source, malformed-source, conflict, interruption, rollback and postcondition canaries. |
| Component aliases and logging | **OPEN** | Canonical component model exists, but AC-05/AC-07 remain in progress and historical Dashboard/Inbox log surfaces require retirement proof. | One component inventory; zero writable/selectable/lifecycle legacy aliases; CENTRAL-only logs with no local fallback. |
| Armed repair/restart | **OPEN** | AC-01 prevents false restart advertisement; AC-12 lacks retroactive installed end-to-end evidence. | Each visible action has Server route → service → owner → mutation → postcondition → audit; otherwise absent from Console. |
| Localization / Console | **OPEN** | AC-10 retains a broader Server-generated-surface and stale watcher-copy audit. | Browser/localization suite proves zero supported localization violations and no stale Dashboard/Watcher/Finder action. |
| Human receipt / provenance | **OPEN** | AC-09 has receipt fields but lacks run linkage and durable audit verification. | Positive/negative durable provenance canary proves complete receipt-to-run chain. |
| LR-09 isolation | **UNRESOLVED, isolated** | Dependabot successor is explicitly out of scope; it must not retain unrelated legacy authority. | `LR09_CAPABILITY_LOSS=0` and `UNRELATED_LEGACY_RUNTIME_RETAINED_FOR_LR09=0`, without successor implementation. |
| Production coverage | **OPEN** | Rebuilt candidate: 1092 tests; 16 modules below 80.20%. | Every production module meets or exceeds 80.20% branch coverage under a freshly rebuilt candidate. |

No row above is evidence for human UI review, Owner Authorization, merge, or
final technical closure until its stated criterion is demonstrated on the
exact candidate head.
