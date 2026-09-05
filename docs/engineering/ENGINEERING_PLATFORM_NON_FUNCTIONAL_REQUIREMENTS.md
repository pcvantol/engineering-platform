# Engineering Platform — Non-functional requirements

**Status:** Canonical EP quality and operational contract

This document is the single review entry point for Engineering Platform (EP)
non-functional requirements. It complements lifecycle and consumer contracts;
it does not redefine their authority. A change to EP must satisfy every
applicable requirement below and retain the named verification evidence.

## Normative sources and precedence

This matrix consolidates the existing authoritative contracts. The source
documents remain normative where they define a more specific rule:

- [Engineering Operations Console Design System](../../tools/engineering/OPERATIONS_CONSOLE_DESIGN_SYSTEM.md)
- [DJConnect Localization Standard](../../LOCALIZATION_STANDARD.md)
- [Platform Quality Standard](../../PLATFORM_QUALITY_STANDARD.md)
- [Local Dashboard Supervisor](LOCAL_DASHBOARD_SUPERVISOR.md)
- [Engineering Platform Architecture Handbook](ENGINEERING_PLATFORM_ARCHITECTURE_HANDBOOK.md)
- [Execution Host Operations](EXECUTION_HOST_OPERATIONS.md)
- [EP 2.x extraction and migration plan](../development/ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md)

## Release requirements

| ID | Requirement | Mandatory verification |
| --- | --- | --- |
| NFR-UX-001 | Every Console control, section, modal, table and responsive rule uses the Operations Console Design System. Shared tokens and components are used; a visual exception updates the design system, implementation and regression coverage together. | Focused Playwright regression and design-system review. |
| NFR-LOC-001 | All new or changed operator-visible EP copy, including dynamic phase/status labels, errors, installer copy and localized accessibility names, ships simultaneously for `en`, `nl`, `de`, `fr` and `es`. Raw identifiers/localization keys are never displayed. Documentation is not qualification evidence: `UI-GOLDEN-LOCALIZATION` is authoritative. | Exact EN key parity, strict no-fallback runtime mode, raw machine-code and classified-literal guards, and the five-locale browser contract. |
| NFR-A11Y-001 | The private Console targets WCAG 2.2 AA: semantic controls, keyboard operation, visible focus, live status updates, 44×44 CSS-pixel action targets, reduced-motion and forced-colour support. | Automated browser accessibility assertions plus manual keyboard, screen-reader and contrast review before claiming conformance. |
| NFR-SEC-001 | EP remains token-free at its UI/projection boundary. Diagnostics, logs, exports and evidence are bounded and redacted; secrets, raw prompts, raw command output and provider credentials are not persisted or rendered. | Redaction/security regression tests and required CI security gates. |
| NFR-REL-001 | SQLite/checkpoint and current repository/GitHub evidence are authoritative. Projections are read-only; missing required evidence fails closed. One writer owns an installation/store and recovery is idempotent, bounded and checkpointed. | Lifecycle, storage, recovery and migration tests. |
| NFR-OPS-001 | Host readiness is phase-aware and token-free. Missing CLI/auth never admits work or consumes credits. Installer and repair actions are explicit, single-flight and verified before success is shown. | Host/preflight/provider-readiness tests and installer verification evidence. |
| NFR-PERF-001 | Console layout remains responsive without semantic loss on supported narrow viewports. Wide evidence remains locally scrollable rather than forcing page-wide horizontal scrolling; browser suites use isolated roots/workers. | Responsive Playwright regressions and targeted performance evidence when changed. |
| NFR-QUAL-001 | EP's required CI checks pass: unit, browser where applicable, localization, packaging/migration/recovery and security gates. The four protected core modules — `server.py`, `platform_bootstrap.py`, `providers.py` and the Server-owned `file_inbox.py` adapter — each maintain at least **80.20% branch coverage**. | `engineering-platform-validation` coverage contract and required GitHub checks. |
| NFR-PKG-001 | A published standalone EP wheel is built only from a clean tagged checkout in explicit production release mode. Its installed contents and runtime dependencies are allowlisted; tests, fixtures, traces, coverage, local data, caches, source metadata, debug tooling/assets and development-only dependencies are absent. Debug-only endpoints/defaults/instrumentation are disabled or excluded. | Fresh-environment wheel install/smoke test, artifact manifest allowlist, dependency audit, debug-profile assertion, SBOM and checksum/provenance evidence. |
| NFR-TDE-001 | TDE is currently observation evidence, not a release blocker. Its executed capabilities, assessment decision and repository qualification are retained as artifacts. A future blocking TDE gate must name each enforced metric, threshold, version and fail-closed rule in this document before activation. | `tde-observe` artifact review; a future required TDE workflow and threshold tests. |
| NFR-INSTALL-001 | The standalone EP installer permits one installation per macOS user. Existing data is explicitly reused, backed up before replacement, or removed only after a second destructive confirmation. `engineering-platform-host --verify` is read-only and reports token-free, actionable repair state. | Clean-machine, upgrade, backup/restore, uninstall and singleton-lock qualification. |

## CI enforcement status

`NFR-QUAL-001` is a current blocking CI contract. `NFR-LOC-001` and the
Console's accessible/responsive behaviour are exercised by the dashboard
browser suite. `NFR-TDE-001` is deliberately non-blocking until the architect
approves concrete thresholds. The standalone-installer requirements are
migration release gates: they become blocking with the first standalone wheel
`2.0.0`, not through a DJConnect developer-host bootstrap. `NFR-PKG-001` is
also a blocking standalone-wheel release gate from `2.0.0`; it cannot be
waived by a source-tree test result or a successful development build.

## Change checklist

For each EP pull request, identify the applicable NFR IDs in the engineering
report or PR evidence, run their named verification, and add/adjust a focused
regression when behaviour changes. If a requirement cannot be met, the run is
blocked for an explicit architect/maintainer decision; no dashboard projection
or CI omission may silently waive it.
