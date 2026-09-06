# Local data retirement matrix

**Status:** LR-04 closure inventory. A local dataset is never promoted to
runtime authority when CENTRAL is unavailable.

| Dataset family | Classification | Runtime disposition |
| --- | --- | --- |
| CENTRAL SQLite run, queue, retry, lifecycle, reports, chats, telemetry, logs and provenance | `CENTRAL_OPERATIONAL_AUTHORITY` | Server reads only CENTRAL projections |
| Server identity/runtime files, installation disk and managed-runtime inspection | `HOST_DIAGNOSTIC_STATE` | installation-only observation; no execution, project or queue authority |
| Root `.engineering` status, runs, reports, chats, runtime diagnostics, caches, watcher state and historical Dashboard service readers | `HISTORICAL_RETIREMENT_INPUT` | no Server route or event stream reads them |
| historical watcher, direct Dashboard listener, Finder/path actions and root-local Inbox location | `DELETE` | removed/unreachable; no supported successor fallback |
| browser theme, locale and section preferences | `HISTORICAL_RETIREMENT_INPUT` | presentation-only; never operational authority |

The Server console snapshot and event stream are CENTRAL-only. CENTRAL failure
returns an unavailable result; it never falls back to a checkout dataset.
