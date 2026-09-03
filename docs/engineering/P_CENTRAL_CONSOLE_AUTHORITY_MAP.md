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
| Telemetry and timing detail | CENTRAL_NATIVE (read projection) | Daily telemetry and day detail join CENTRAL telemetry to canonical project/run lineage. |
| Provider usage | HISTORICAL_DASHBOARD_DELEGATE | Slice C remainder. |
| Evidence report downloads | CENTRAL_NATIVE | Report index and artifact path are authorized by `(project_id, run_id)` in CENTRAL. |
| Prompt chat history | CENTRAL_NATIVE (read projection) | Immutable transcript lookup is scoped by CENTRAL project/run lineage. |
| Provider-backed chat mutation and report analysis | RETIRED/UNREACHABLE from migrated routes | No CENTRAL Server authority is invented for historical root-backed mutation. |
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

## Slice C telemetry rule

The telemetry trend and day-detail route read `execution_runs` only through
`ep_parity_lifecycle_dispatches.project_id`.  Thus a project cannot read,
clear, or discover another project's telemetry through these read routes, and
checkout deletion does not alter the projected history.

## Slice D evidence rule

The report-download and chat-history routes no longer inspect a checkout.
They first prove the requested run belongs to the selected CENTRAL project,
then resolve only CENTRAL-indexed artifact/transcript records.  The deletion
canary covers both paths.
