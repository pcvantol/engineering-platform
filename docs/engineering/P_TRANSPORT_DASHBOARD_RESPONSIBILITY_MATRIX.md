# P-TRANSPORT Dashboard responsibility matrix

**Status:** extraction inventory. This is the source-of-truth worklist for
LR-02, LR-03, LR-04 and LR-12; it is not completion evidence.

`dashboard.py` was a combined listener, Console renderer and checkout-local
service module. The direct listener is no longer an entry point, but its code
is still present. Each remaining responsibility has the final classification
below; `MIGRATED = no` means the item blocks physical deletion of the module.

| Responsibility / symbols | Old owner | Target boundary | Target owner | Migrated | Legacy path remaining | Qualification |
| --- | --- | --- | --- | --- | --- | --- |
| HTML shell, installed assets, manifest and locale bootstrap (`_dashboard_html`, asset constants) | Dashboard wrapper | `CONSOLE_PRESENTATION` | EP Server | partial | Server still imports the helpers from `dashboard.py` | Server document and browser shards |
| Platform component health, details and component logs | Dashboard wrapper | `PLATFORM_SERVICE` | EP Server + CENTRAL | yes | historical helper implementations remain unused by supported routes | route ownership and component/log tests |
| Provider readiness, login/logout, runtime inspection and capacity projection | Dashboard wrapper | `PLATFORM_SERVICE` | EP Server | partial | Server currently imports bounded provider/runtime helpers | Server route tests |
| Project runs, report/chat/telemetry projections and event stream | Dashboard wrapper | `PROJECT_SERVICE` | EP Server + CENTRAL | partial | root-local helper readers remain in `dashboard.py` for transitional compatibility | Server project-route tests |
| Disk/runtime observations | Dashboard wrapper | `HOST_ADMIN` | EP Server Host Admin | partial | old dashboard helpers remain; `/api/host-admin/diagnostics` is canonical | Host Admin and Server tests |
| Git lock/worktree diagnostics and mutations | Dashboard wrapper | `HOST_ADMIN` | EP Server Host Admin | no | checkout-root helpers remain with no supported Server route | target registry, containment and mutation qualification required |
| Finder/open-local-path helpers | Dashboard wrapper | `DELETE` | none | route/UI yes; source no | dead helpers/tests remain pending deletion | source guard asserts remote Finder routes are absent |
| Root-local reports, chats, workspace snapshots, status and analysis files | Dashboard wrapper | `HISTORICAL_READ_ONLY` | retirement/migration only | no | helper readers remain | local-data retirement matrix required |
| `dashboard_configuration` reads/writes and local Inbox location | Dashboard wrapper | `HISTORICAL_READ_ONLY` | migration/forensics only | partial | direct-wrapper helper usage remains | Server routes reject Inbox location; config migration required |
| `dashboard` / `inbox` component aliases | Dashboard wrapper | `HISTORICAL_READ_ONLY` | compatibility reader only | partial | direct-wrapper alias maps remain | canonical writer/log guard |
| Direct `DashboardHTTPServer`, `handler`, `create_servers`, `run`, `main`, dashboard LaunchAgent and relay wrappers | Dashboard wrapper | `DELETE` | none | no | code and historical direct-runtime tests remain | physical removal and absence guard required |
| Relay compilation/install/uninstall | Dashboard wrapper | `INSTALLATION` | EP Server installation | partial | `server_relay.py` is canonical; dashboard wrappers remain | relay tests |

## Deletion order

1. Extract the first three partially migrated service/presentation groups from
   `dashboard.py` into explicit Server modules.
2. Finish the Host Admin target registry or retire its mutations.
3. Classify/migrate every local file reader and configuration key.
4. Retire component aliases after their direct-wrapper reader is removed.
5. Delete the direct listener, its wrappers and their direct-runtime tests.

Until step 5 completes, `DIRECT_DASHBOARD_RUNTIME = 0` is not claimable.
