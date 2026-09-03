# P-CENTRAL-CORE browser qualification boundary

## Decision

The local `dashboard_browser_validation` four-shard batch is
**DEFERRED_TO_P_CENTRAL_CONSOLE** for P-CENTRAL-CORE qualification. Its sole
Playwright target is `tests/engineering/dashboard.spec.mjs`, which validates
the historical Operations Console presentation and its installed assets.

It is not evidence for the P-CENTRAL-CORE storage-authority exit gate. The
CORE gate is instead proven by the installed Server and canonical-ingress
canaries, CENTRAL-backed lifecycle/evidence tests, project-scope rejection
tests, and the no-local-storage filesystem checks.

## Classification of the timed stage

| Timed responsibility | Phase owner | Classification |
| --- | --- | --- |
| Four-shard Playwright launcher and dashboard screenshots | P-CENTRAL-CONSOLE | `P_CENTRAL_CONSOLE_DEFERRED` |
| Dashboard UI catalog, controls, settings, Finder and worktree actions | P-CENTRAL-CONSOLE | `P_CENTRAL_CONSOLE_DEFERRED` |
| Historical status, queue, Prompt History, lifecycle and telemetry rendering | P-CENTRAL-CONSOLE | `P_CENTRAL_CONSOLE_DEFERRED` |
| Logs, reports/downloads, chat/transcript presentation and route parity | P-CENTRAL-CONSOLE | `P_CENTRAL_CONSOLE_DEFERRED` |
| Localization, accessibility, responsive CSS, modal and mobile layout | P-CENTRAL-CONSOLE | `P_CENTRAL_CONSOLE_DEFERRED` |
| Dashboard-side operations-health rendering, including its browser HTTP probe | P-CENTRAL-CONSOLE | `P_CENTRAL_CONSOLE_DEFERRED` |

No responsibility timed out in the browser stage that is classified
`P_CENTRAL_CORE_REGRESSION`. Installed Server health, CENTRAL availability,
canonical ingress, project ownership, lifecycle state and local-storage
absence retain non-browser CORE qualification requirements.

## Hosted-check policy

The repository workflow currently makes `browser-dashboard` run for the
`RUNTIME` validation profile and makes Trusted Delivery evidence depend on it.
That is CI-policy coupling, not authorization to implement P-CENTRAL-CONSOLE
inside this increment. A future bounded, phase-aware workflow/profile change
must be owned by P-CENTRAL-CONSOLE (or repository CI governance), preserve
branch protection, and restore the full browser suite for that lane.

## Required P-CENTRAL-CONSOLE follow-up

P-CENTRAL-CONSOLE must migrate the retained Dashboard delegation to
CENTRAL-native project/no-project projections and then qualify the complete
installed Console browser suite, including assets, status/history/config/logs,
downloads, chat and responsive presentation. This increment does not start
that work.
