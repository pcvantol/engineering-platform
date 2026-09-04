# P-TRANSPORT audit-closure register

**Scope:** exact-head technical closure before human UI review.  This register
turns the P-TRANSPORT consequence audit into normative, testable work; it is
not a roadmap.

| ID | Severity | Root cause / violated invariant | Repair and evidence | Status |
| --- | --- | --- | --- | --- |
| AC-01 | critical | Console exposed component restart without a Server-owned lifecycle operation. `VISIBLE_RESTART_WITHOUT_REAL_ROUTE=0` | Canonical model now marks every component `restart_supported=false`; real Console projections cannot render a false operation. | FIXED — focused Server/browser regression pending full gate |
| AC-02 | critical | A published File Inbox executable could claim an Inbox outside EP Server. `STANDALONE_FILE_INBOX_EXECUTABLE=0` | Removed the package entry point and module CLI entry; File Inbox is constructed only by `server.serve`. | FIXED — installed-wheel absence check pending full gate |
| AC-03 | high | A live child without admission capability was projected healthy. `FILE_INBOX_READY_IMPLIES_SUBMISSION_CAPABLE=TRUE` | Heartbeat differentiates `READY` from `RUNNING_NOT_READY`; platform projection maps the latter to `FILE_INGRESS_NOT_READY`. | FIXED — installed negative canary pending full gate |
| AC-04 | critical | One external project credential could not safely represent multi-project File Inbox authority. | Canonical security decision: Server-owned `FILE_INBOX` principal invokes `request_from_mapping` and `submission_service.submit` in-process. It bypasses only external caller authentication. | FIXED — installed multi-project canary PASS |
| AC-05 | high | Component consumers duplicated identity inventories. `PLATFORM_COMPONENT_MODEL_COUNT=1` | Route pattern, startup events and transport rendering derive from `platform_components`; browser logging now has one platform state and derives selectable component identities from that model. Remaining aliases/legacy inventory are tracked by AC-06. | IN_PROGRESS |
| AC-06 | critical | Legacy watcher/dashboard runtime remains packaged and has supported-looking code paths. `SUPPORTED_LEGACY_INBOX_WATCHER_RUNTIME=0` | The canonical Server import boundary now proves that it does not load `inbox_watcher`; retired direct-dashboard compatibility loads it only on an old direct-handler request. Structural removal of that handler and the remaining lifecycle implementation is still required. | IN_PROGRESS |
| AC-07 | high | CENTRAL logging had a Server-bound checkout-file fallback. `REPOSITORY_LOCAL_COMPONENT_LOG_FALLBACK=0` | Server-bound logger now emits only bounded stderr on CENTRAL failure; no local persistent fallback. Legacy writers retire with AC-06. | IN_PROGRESS |
| AC-08 | high | Qualification lacked CLI invalid-Genesis, complete Human negatives and port isolation. | Installed matrix now uses OS-assigned ports and includes the missing negative cases. | FIXED — installed matrix PASS |
| AC-09 | high | Human receipt lacked complete audit-chain provenance. | Accepted receipt now records source/normalized digest, scope, requested mode, normalization method/version and submission identity. | IN_PROGRESS — run linkage and durable audit verification pending |
| AC-10 | high | Localization guard omits some Server-generated surface and stale watcher copy. | The Server-owned no-project title, explanation and selector now use five-locale keys; the broader Server-rendered surface audit remains in progress. | IN_PROGRESS |
| AC-11 | high | Presentation-level Console/log compatibility transforms conceal legacy source structure. | Browser logging no longer retains separate Inbox/Dashboard state, and both supported Server document paths now consume the canonical log markup directly. The unused historical helper remains for the broader structural dashboard deletion in AC-06. | IN_PROGRESS |
| AC-12 | high | Retroactive CENTRAL armed-repair end-to-end evidence is missing. | Recover contract, add installed positive/negative/restart gate. | IN_PROGRESS |

`UNRESOLVED_AUDIT_FINDINGS` remains non-zero until every `IN_PROGRESS` item
has a real product-boundary canary and exact-head installed-wheel evidence.
