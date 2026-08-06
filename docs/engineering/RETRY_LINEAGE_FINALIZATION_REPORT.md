# Retry Lineage Finalization — Engineering Report

## Outcome

**YES. Retry Lineage Finalization is operational.**

Historical terminal executions remain immutable evidence. A retry creates a
separate child execution, and the parent cannot create a second child once that
relationship is present in Prompt History or pending in the Inbox.

## Delivered projection

- Prompt History derives retry parent, child, chain and current run from the
  immutable `retry_of` relationship.
- Historical blocked rows no longer expose Retry after a child exists.
- Retry submission rejects duplicate children before a new Inbox prompt is
  created.
- The dashboard shows a compact Run-ID suffix and keeps the historical table
  horizontally scrollable on iPad portrait.
- The five-character column is intentionally tablet/desktop-only, preserving
  the established iPhone Prompt History layout.
- Producer type labels are localized for `en`, `nl`, `de`, `fr` and `es`.
- Prompt History now uses consistently styled glyph actions, compact dates and
  responsive columns; component-log Details uses remaining table width.
- The title bar offers the same safe browser refresh through pull-to-refresh
  and the localized circular **Page refresh** control. iPhone and iPad prevent
  accidental double-tap zoom while keeping pinch zoom available.

## Verification

- Focused Python retry, history and dashboard regressions cover the immutable
  parent/child relationship and duplicate-child rejection.
- Browser regressions cover all five localized headers and the five-character
  iPad Run-ID projection, Details-column width allocation and the localized
  Page refresh accessibility label.
- JavaScript syntax and `git diff --check` are required before review.

## Boundary

No Forge, execution lifecycle, Execution Host scheduling or runtime behavior
was changed. Retry remains distinct from Resume.
