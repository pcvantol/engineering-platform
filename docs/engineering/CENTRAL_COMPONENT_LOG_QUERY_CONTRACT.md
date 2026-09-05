# CENTRAL component-log query contract

Status: `P-TRANSPORT AWAITING_HUMAN_UI_REVIEW`

The Console has exactly one Platform-owned log projection. It reads redacted
records from the installation's CENTRAL `engineering_component_logs` table;
it does not read a selected project's checkout, local dashboard database, or
legacy rotating file. A selected project is therefore presentation context
only and has no effect on the log route's owner or health semantics.

## Routes and ownership

| Route | Owner | Result |
| --- | --- | --- |
| `GET /api/logs/all` | PLATFORM | One combined page across all canonical EP components. |
| `GET /api/logs/{component}` | PLATFORM | One page for a canonical component. `dashboard`, `inbox`, and `execution-host` remain read aliases for historical records. |
| `GET /api/logs/{component}?format=ndjson` | PLATFORM | Complete filtered export, rather than the visible page. |
| `POST /api/logs/all` with `{"component":"all"}` or a canonical component | PLATFORM | Clears only the named CENTRAL projection. |

The supported canonical component identifiers are `ep_server`,
`platform_database`, `lifecycle_worker`, `operations_console`,
`dashboard_relay`, `http_ingress`, `cli_ingress`, and `file_inbox_ingress`.

## Query processing

All filters are applied in CENTRAL **before** counting, sorting, and page
selection. This prevents a historical match from disappearing because newer
records happen to fill a client-side sample.

| Parameter | Meaning | Constraint |
| --- | --- | --- |
| `page`, `page_size` | One-based page and rows per JSON response | `page >= 1`; `1 <= page_size <= 200`; default `1`, `50` |
| `start`, `end`, `inclusive_end=1` | UTC timestamp range over `created_at` | End is exclusive unless explicitly inclusive |
| `search` | Case-insensitive literal search over the redacted JSON payload | At most 160 characters; `%`, `_`, and `\` are escaped |
| `level` | Minimum severity | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `event` | Repeated exact event filter | At most 50 values, each at most 160 characters |
| `sort`, `direction` | `line`, `timestamp`, `level`, `event`, `runId`, or `details`; `asc`/`desc` | Defaults to `timestamp desc`; `id` breaks ties deterministically |

The JSON response includes `entries`, `total`, `page`, `page_size`, and the
available `events` under the active component/time/text/level constraints
(but before the selected event values are applied). This keeps the event
picker useful without broadening the result set. Invalid values return
`400 {"error":"LOG_QUERY_INVALID"}`; unknown component paths return `404`.

`format=ndjson` preserves the active component, timestamp, text, level and
event filters but deliberately omits `page` and `page_size`, exporting up to
the retained 5,000 matching CENTRAL records. Every exported record retains
the structured fields, canonical `component`, timestamp, level, event,
run identifier, diagnostic, and the remaining redacted payload fields.

## Qualification

The contract is enforced by:

- `test_central_log_route_filters_sorts_and_paginates_before_responding`:
  real HTTP route coverage for range, text, severity, event options, sort,
  two pages, NDJSON export, and invalid-query rejection.
- `dashboard.spec.mjs` browser coverage: the EP-component picker, range,
  search, level, event filter, sort, pagination, copy, clear and download
  controls each emit the CENTRAL contract rather than client-side filtering.
- `tools/qualification/console_route_ownership_guard.py`: confirms logging is
  PLATFORM-owned and has no selected-project or checkout dependency.
