# P-CENTRAL-CONSOLE authority map

**Status:** Slice A in progress.

This map classifies the installed Server-to-Console boundary.  A checkout is a
physical execution binding only; it is not Console authority.

| Surface | Current classification | Slice A disposition |
| --- | --- | --- |
| Console shell at `<geen>` | CENTRAL_NATIVE | The Server renders it without resolving a checkout. |
| Project selector | CENTRAL_NATIVE | Logical active project identities are read from CENTRAL. |
| Static CSS, JS, locales and icons | STATIC_ASSET | The Server serves package assets directly, before scope selection. |
| Platform status | SERVER_PLATFORM_NATIVE | `/api/platform-status` uses Server runtime and CENTRAL metadata only. |
| CENTRAL database controls | CENTRAL_NATIVE | Already served by `central_database`. |
| Provider capacity | SERVER_PLATFORM_NATIVE | Already scoped to the installed runtime/CENTRAL policy. |
| Selected-project queue | CENTRAL_NATIVE | `/api/dashboard-snapshot` reads CENTRAL submissions by `project_id`. |
| Active execution, history, lifecycle | CENTRAL_NATIVE (read projection) | Snapshot, prompt history and run detail resolve `(project_id, run_id)` from CENTRAL. |
| Telemetry, usage and timing | HISTORICAL_DASHBOARD_DELEGATE | Slice C. |
| Evidence, downloads, prompt history and chat | HISTORICAL_DASHBOARD_DELEGATE | Slice D. |
| Configuration and component logs | HISTORICAL_DASHBOARD_DELEGATE | Slice E. |
| Worktree, provider-login and update actions | HISTORICAL_DASHBOARD_DELEGATE | To retire or explicitly govern; not available at `<geen>`. |

`AMBIGUOUS active routes = 0` for the Slice A boundary.  Selected-project
routes remain explicitly classified as transitional and cannot be described as
CENTRAL-native until their respective slice is complete.

## Slice A no-project rule

At `<geen>`, the Server accepts only explicit platform endpoints.  Unsupported
project endpoints fail closed with `CONSOLE_PROJECT_UNAVAILABLE`; the Server
does not select a first project or call `_console_root`.  This is covered by
`test_no_project_console_never_resolves_a_checkout_for_shell_assets_or_platform_data`.

## Slice B project rule

`/api/dashboard-snapshot`, `/api/status`, `/api/prompt-history`, run detail,
and the initial events payload now resolve from CENTRAL before a historical
route could request a checkout.  The bounded run projection is keyed by
`(project_id, run_id)`.  It remains available after checkout deletion and the
focused server test verifies that project A cannot read project B's history.

The selected-project HTML document itself is still a historical delegate in
this intermediate slice; that is the remaining Slice A/B delegate boundary,
not authority for the migrated read routes.
