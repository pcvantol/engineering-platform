# Phase 3 / Increment 1 extraction receipt

## Source and export

- Source repository: `pcvantol/djconnect`
- Source branch/SHA: `origin/main` / `3668eb77fc89418003ae60eeb72c8391e90c3055`
- Export date: 2026-08-31
- Export tool: `git-filter-repo a40bce548d2c` with Git 2.55.0
- Manifest: version 2; candidate digest `9d07f6ab57ed3cb07cabc3ab037db6233b9618d2fdbfbcf30e21d0e305c00515`; semantic digest `02820842f946990a2e65f4302b3fd55c7d2a0f143a77b944ae389a502a502ecf`

The filtered history retains only manifest-owned EP source, tests,
documentation, release assets and EP workflows. `docs/engineering/runs/**`
is excluded as immutable DJConnect evidence. Consumer adapters, DJConnect
product code, host onboarding, and all other workflows remain in DJConnect.
No database, credential, registration, authority pointer, freeze, or service
state was exported.

## Mechanical mapping

| Source | Target | Rewrite |
| --- | --- | --- |
| `tools/engineering/**` | `src/engineering_platform/**` | path relocation and `tools.engineering` → `engineering_platform` imports only |
| `tests/engineering/**` | `tests/engineering/**` | matching import/path rewrite only |
| `docs/engineering/**` | `docs/engineering/**` | copied; run evidence excluded |
| selected EP ADR/development docs | `docs/adr/**`, `docs/development/**` | copied with original provenance |
| EP workflows | `.github/workflows/**` | copied pending standalone CI normalization |

`tools/extraction/verify_phase3_equivalence.py` is the deterministic
file-level digest and rewrite-boundary validator. Its receipt records each
source/target Python path, source digest, pre-rewrite digest, final digest and
the permitted rewrite category.

## Qualification contract

The source extraction audit reported 301 candidates: 100 product source, 70
tests, 37 documentation files, 4 release assets and 5 workflows, with zero
blocking imports. Target qualification runs only with temporary HOME and
temporary Engineering Platform roots. It must not resolve DJConnect runtime
imports or access the installed authority.

Production service action: **NONE**. Production DB migration: **NONE**.
Consumer cutover: **NONE**.
