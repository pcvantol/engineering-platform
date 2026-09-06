# P-TRANSPORT Dashboard responsibility matrix

**Status:** extraction inventory. This is the source-of-truth worklist for
LR-02, LR-03, LR-04 and LR-12; it is not completion evidence.

The historical Dashboard was a combined listener, Console renderer and
checkout-local service module. LR-02 removed that direct runtime; the
remaining Server-owned helpers have explicit target boundaries below.

| Responsibility / symbols | Old owner | Target boundary | Target owner | Migrated | Legacy path remaining | Qualification |
| --- | --- | --- | --- | --- | --- | --- |
| HTML shell, installed assets, manifest and locale bootstrap (`_dashboard_html`, asset constants) | Dashboard wrapper | `CONSOLE_PRESENTATION` | EP Server | yes | `console_presentation.py` and `server_console_services.py` are Server-owned modules | Server document and browser shards |
| Platform component health, details and component logs | Dashboard wrapper | `PLATFORM_SERVICE` | EP Server + CENTRAL | yes | historical helper implementations remain unused by supported routes | route ownership and component/log tests |
| Provider readiness, login/logout, runtime inspection and capacity projection | Dashboard wrapper | `PLATFORM_SERVICE` | EP Server | partial | Server currently imports bounded provider/runtime helpers | Server route tests |
| Project runs, report/chat/telemetry projections and event stream | Dashboard wrapper | `PROJECT_SERVICE` | EP Server + CENTRAL | partial | root-local helper readers remain in `dashboard.py` for transitional compatibility | Server project-route tests |
| Disk/runtime observations | Dashboard wrapper | `HOST_ADMIN` | EP Server Host Admin | partial | old dashboard helpers remain; `/api/host-admin/diagnostics` is canonical | Host Admin and Server tests |
| Git lock/worktree diagnostics and mutations | Dashboard wrapper | `HOST_ADMIN` | EP Server Host Admin | yes | explicit opaque target registry; legacy destructive operations are `UNSUPPORTED_REMOVED` | Host Admin containment, inventory, audit and negative tests |
| Finder/open-local-path helpers | Dashboard wrapper | `DELETE` | none | route/UI yes; source no | dead helpers/tests remain pending deletion | source guard asserts remote Finder routes are absent |
| Root-local reports, chats, workspace snapshots, status and analysis files | Dashboard wrapper | `HISTORICAL_READ_ONLY` | retirement/migration only | no | helper readers remain | local-data retirement matrix required |
| `dashboard_configuration` reads/writes and local Inbox location | Dashboard wrapper | `HISTORICAL_READ_ONLY` | migration/forensics only | partial | direct-wrapper helper usage remains | Server routes reject Inbox location; config migration required |
| `dashboard` / `inbox` component aliases | Dashboard wrapper | `HISTORICAL_READ_ONLY` | compatibility reader only | partial | direct-wrapper alias maps remain | canonical writer/log guard |
| Direct listener class, handler, listener factory, bootstrap, dashboard LaunchAgent and relay wrappers | Dashboard wrapper | `DELETE` | none | yes | physically removed | source absence guard and Server browser harness |
| Relay compilation/install/uninstall | Dashboard wrapper | `INSTALLATION` | EP Server installation | partial | `server_relay.py` is canonical; dashboard wrappers remain | relay tests |

The source guard and the Server browser harness qualify the absence of a
direct Dashboard runtime. Other retirement-register items remain separate
from LR-02 and cannot become Dashboard runtime entry points.
