# Configuration migration matrix

**Status:** LR-04 closure inventory. Every supported setting has one owner.

| Setting family | Classification | Canonical owner |
| --- | --- | --- |
| bind host/port, Server identity/runtime, file inbox and managed runtime | `INSTALLATION` | EP Server data root |
| Console intervals, retention, capacity policy and database maintenance | `SERVER` | CENTRAL Server configuration tables |
| project identity, repository attachment and execution/submission policy | `PROJECT` | CENTRAL project records, selected by explicit project id |
| `dashboard_configuration.*`, Inbox root, checkout/root config, browser preferences | `HISTORICAL` | no supported operational read/write path |

There is no project context inference from a checkout, CWD, Git remote,
browser state, root-local setting, or local configuration fallback. Browser
storage is presentation state only.
