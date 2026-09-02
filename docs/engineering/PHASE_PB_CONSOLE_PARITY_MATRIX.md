# Phase P-B Console parity matrix

The standalone Server root reuses `engineering_platform.dashboard.handler` and
the installed package's `dashboard.js`, `dashboard.css`, locale catalog,
status store, and Operations Console icons.  It does not duplicate the legacy
route table.  CENTRAL validates the selected project through its schema-44
local repository binding before each historical dashboard request.

The former topology page is retained at `/diagnostics/topology`.  It is an
installation diagnostic, not an Operations Console replacement.

| Historical surface | P-B disposition | Scope / evidence |
| --- | --- | --- |
| Queue, ordering, defer and recovery controls | RESTORED | Preserved dashboard routes; selected schema-44 project binding on every request. |
| Current execution and lifecycle | RESTORED | Preserved status projection and SSE route. |
| Run history | RESTORED | Preserved historical state projection. |
| Prompt History, details, chat and deep links | RESTORED | Preserved routes, modal and browser assets. |
| Reports, receipts and evidence | RESTORED | Preserved report/detail/analysis routes. |
| Provider/model usage and timing telemetry | RESTORED | Preserved usage and telemetry routes. |
| Validation, qualification and recovery | RESTORED | Preserved status, preflight and recovery actions. |
| Configuration, logs and component health | MINIMALLY_ADAPTED | Historical surface remains; raw bound local-root strings are removed from the root document. |
| CENTRAL identity, schema and topology | MINIMALLY_ADAPTED | Installation-scoped diagnostic at `/diagnostics/topology`. |
| Five locales and responsive/mobile behavior | RESTORED | Existing package locale, CSS and browser suite are reused unchanged. |
| Project selector and two-tab isolation | MINIMALLY_ADAPTED | Root query selects the project; fetches carry a project header and SSE carries the same query, so tabs do not share browser scope. |

**UNACCOUNTED: 0.**

This parity accounting does not mean that every restored surface is already
CENTRAL-native.  The retained local-root dependencies, their current
disposition, and the required replacement evidence are tracked separately in
the [Phase P migration-gaps register](PHASE_P_MIGRATION_GAPS_REGISTER.md).

P-B is a presentation/action adapter only.  It does not invent lifecycle
semantics, dispatch a provider, or start Project Agent execution.
