# Engineering Operations Console — Design System

**Status:** Canonical code-derived baseline  
**Scope:** Private Engineering Operations Console (`tools/engineering/dashboard.py`, `assets/dashboard.css` and `assets/dashboard.js`)  
**Last reconciled:** 2026-08-16

This document makes the current console design explicit. It is the design and
review baseline for every future console change: a new control, section,
state, modal, table or responsive rule must either use these rules or update
this document, its implementation and its verification together.

The implementation remains the executable source of truth. This document
turns its stable, repeated decisions into a reviewable contract; it must not
be used to justify a visual divergence from the code.

## 1. Product character

The console is an operational instrument, not a marketing surface. It must be
calm under normal operation, strongly legible when something is blocked, and
deliberate before it performs a state-changing action.

Core principles:

1. **State before decoration.** Category colour communicates the kind of
   operational information; status colour communicates health or risk.
2. **Layered complexity.** The dashboard opens compact. Expandable categories,
   detail modals and tables expose evidence only when requested.
3. **Reversible by default.** Destructive or workflow-changing actions need a
   confirmation; safe reversible actions say what is retained and where.
4. **One visual language across themes and devices.** Light mode changes
   surfaces, never meaning. Phone layouts retain labels and touch targets
   rather than replacing them with unexplained glyphs.
5. **Evidence is readable and copyable.** Tables, reports and logs preserve
   semantic headers, meaningful empty states and selective copy/download.

## 2. Design tokens

Use the variables in `assets/dashboard.css`; do not introduce hard-coded
near-duplicates in a component.

| Token / role | Dark mode | Light mode / rule |
| --- | --- | --- |
| House-style orange | `--house-style: #f0b66a` | Same hue; interactive focus and shared operational affordance |
| Orange surface / contrast | `#4a321f` / `#fff0dc` | `#fff4e6` / `#653a13` |
| Page surface | `#121217` | `#e8edf4` / `#f4f7fb` depending on chrome or document context |
| Standard content surface | `#24242d` | `#fff` |
| Modal surface | `--dashboard-modal-surface` | White; document surface is `#f7fbff` |
| Modal radius / shadow | `18px` / `0 16px 50px #000a` | Same geometry, theme-appropriate shadow |
| Selected-control border | `1px` orange outline with a `4px` soft ring | Inputs, selects and text areas only |
| Console blue | `--operations-console-blue: #0a6b9d` | Header/identity accent, not a replacement for semantic category colour |
| Mark blue | `--operations-console-mark-blue: #00b8f4` | Product mark only |

### Category accents

The accent belongs to the information domain and is used consistently for the
category border, heading/glyph and divider. Keyboard focus is always
house-style orange; a category accent never becomes a competing focus colour.

### Modal geometry and glyphs

Every modal header is a full-width inner panel surface: its background and
divider begin and end exactly at the panel's inner border edge. Header inset
and panel padding share one token, so a modal family cannot leave an unfilled
rim beside its title bar. Browser coverage verifies that alignment for every
shared modal family.

Title glyphs describe purpose rather than severity: historical details use a
circled information glyph, AI analysis uses a circled sparkle, ordinary action
confirmations use a circled question mark, and destructive confirmations use a
warning triangle. Errors retain their close/error glyph. These glyphs are
decorative; the localized title remains the accessible name.

| Domain | Accent | Typical surface |
| --- | --- | --- |
| Active execution | pink `#f472b6` | `#321d2d` |
| Queue / execution context | indigo `#818cf8` / `#a78bfa` | `#25243a` / `#28263a` |
| Execution history | rose `#f29ab2` | `#36232d` |
| Capacity / resource | green `#54d6a0` | `#20332f` |
| Operational overview / evidence | cyan `#65c5d9` and blue `#8dc7ff` | `#202b34` / `#202a36` |
| Workspace | yellow `#f3d36a` | `#302d20` |
| Logs / diagnostics | orange `#f0b66a` | `#302a24` |
| AI conversation | purple `#d0a4ff` | `#292336` |
| Platform health | lime `#a3e635` | `#29331d` |

Terminal status remains distinct from category colour: success green, warning
yellow/orange and failure red/rose. Ordinary lifecycle activity uses the
containing turquoise accent and its animated ring; orange is reserved there
for an operator action or blocked/waiting condition.

## 3. Typography, spacing and geometry

- Use the system UI stack for ordinary interface text. Use `Unispace` with a
  `ui-monospace` fallback only for log values, identifiers and code-like
  evidence.
- Body baseline is **14px**. Metadata is **12px**; labels are **11px**, bold,
  uppercase and letter-spaced.
- The page title is **28px / 1.1** desktop and **25px** at phone width.
- Main category headings are **17px / 1.25**, bold, uppercase and
  letter-spaced. Modal titles use the shared compact heading scale; do not let
  a particular modal invent a larger headline.
- Standard card padding is **14px**. Main panels use **18px** radius; nested
  cards use **14px** or **8px** where density requires it. Modal panel padding
  is **24px**.
- Keep a regular dashboard grid gap of **16px** and use the shared scroll,
  safe-area and section-gap tokens rather than per-view margins.

## 4. Surfaces and disclosure

### Title bar and identity

The title bar is the console identity surface. It contains the transparent
mark, localized title and global controls. It is sticky on wide layouts and
remains a single, readable ownership boundary from content below it. The mark
must blend into its surrounding title-bar surface; do not give it an unrelated
tile background.

Browser and installed-app identity use only the themed assets in
`assets/operations-console/`. The document has one versioned browser-tab icon
link, which is updated on a theme change. Do not retain or reintroduce an
Engineering Status icon, fallback route or parallel icon family.

### Main categories

Use native `<details>` / `<summary>` for major dashboard disclosure. A
category has a coloured border, category glyph, heading and one-sentence
description. When open, its header gets a subtle category-tinted surface while
the contained evidence uses the neutral content surface. Do not tint every
nested card heavily. Categories are navigation surfaces rather than selected
inputs: they do not receive the orange selected-control ring. Reserve that
orange border for actual inputs, selects and text areas.

### Cards and tables

Cards group one coherent evidence type. Tables retain headers, sortable states
and horizontal scrolling at narrow widths; they are not squeezed into
illegible columns. A selected data row is one contiguous treatment: a shared
tinted row surface with only its leading selection marker. Never draw a
separate focus or selection border around individual cells. Sortable headers
may use their own thin focus edge because they are independently interactive;
that edge must remain contained inside the sticky header cell.

The **Operationeel overzicht** card grid is one column by default. Once its
own container reaches **760px**, it uses two equal columns. This container
query keeps individual evidence cards readable in narrow side-by-side layouts
without tying their layout to the full browser width. The **Diagnose** card
spans both columns at that breakpoint so its operational recommendation is not
artificially constrained.
Its captions use the related light-turquoise secondary accent, never the
orange diagnostic secondary tone.

Repeated compact evidence, such as specialist reviewer status, uses an
auto-fitting grid of at least `180px` tiles. It fills a row when space permits
and wraps cleanly at narrower widths; never hard-code a single wide tile per
reviewer. In the live-execution area, reviewer captions use the same turquoise
accent family as the parent container; light mode uses its accessible dark
turquoise ink rather than an unrelated evidence-blue.

Interactive data rows use their parent table's category tint across every
cell on hover and selection, with only the shared leading selection marker.
Text actions inside such a row do not add an underline or a separate hover
colour: the row itself is the affordance.

Every table row divider uses the parent category's **secondary** accent at a
subtle opacity. Neutral black or unrelated grey dividers are not permitted;
the divider is supporting structure, not a separate visual language.

Prompt-history status width is measured from the rendered status labels on the
current visible page. It must accommodate the longest localized state,
including an operator-dismissed terminal state, without colliding with the
execution title. The title column yields proportionally, remains readable and
then truncates with an ellipsis. Filtering, sorting and pagination recompute
the allocation; a previous page may never determine the current page's width.

### Duration indications

Execution-duration indications are advisory operational evidence, never a
scheduling promise. When at least two completed executions share the exact
reported runtime profile, the console uses their recent measured Codex time,
adjusted sublinearly and with a capped factor for prompt-size differences.
That learned range takes priority over the coarse prompt-size fallback; the
fallback retains a small safety contribution. The UI must explain the sample
count and never report a live Codex-progress or token signal it does not have.

Log copy means the **currently visible result set**: after filtering, sorting
and current-page pagination. It includes headers and no hidden rows.

Percentages are locale-formatted with exactly **one fractional digit**. This
applies to live metrics, limits and telemetry alike, so precision does not
vary by panel or by refresh.

Estimated execution time is advisory. When at least two completed runs share
the exact reported runtime profile, the dashboard may use their persisted
phase timings for the current and remaining operational phases. Operator merge
and external-check waiting are excluded; the result remains a range, never a
promise or scheduler input.

### Execution lifecycle flow

In the active-execution card, identify the run first: **Execution title** and
**Filename** precede the **Execution** identity card (Run-ID and start time).
The **Estimated execution time** card follows identity, then the lifecycle
flow, execution status, execution context and local Codex processes. This
keeps the estimate adjacent to the run it describes.

Step labels name their operational boundary rather than only its generic
phase: **PR-controleherstel** identifies bounded repair of failed PR checks,
**Implementatie-merge** identifies the implementation PR hand-off, and
**Finalisatie-merge** identifies the separate finalization PR hand-off. These
three labels are localized as a related set in every supported language.

Within the active-execution container, the lifecycle and execution-context
blocks use the same card surface as status and operational blocks. Their
turquoise border and headings provide the distinction; a lighter inner fill is
not used.

Lifecycle steps use fixed-width slots, a visible connector element on a layer
behind the circular nodes, and equal connector length between every adjacent
pair. The connector centre aligns exactly with the circle centre. Long labels
wrap within their own slot rather than changing the topology. Labels always
inherit the standard interface text colour and weight. Ordinary active and
previously completed intermediate circles use the containing turquoise; only
the terminal **complete** result uses semantic green with its check glyph. An
active operator merge wait uses house-style orange with a dark `⌛` glyph, so
it retains the same warning meaning as a blocked historical result.

The neutral **Start** boundary uses one decorative rocket glyph. Its accessible
name remains the localized Start label, so the glyph adds a friendly visual
cue without adding a second spoken state.

Lifecycle status glyphs use a deliberate `20px` size inside the fixed circles;
the decorative Start rocket is `22px`. Neither changes the circle or connector
geometry.

Each lifecycle circle is an interactive detail control. On pointer hover its
circle border switches to house-style orange, without changing its diameter or
the connector alignment.

On the first render of an active run, a horizontally clipped current step is
centred in the lifecycle scroller. Subsequent server refreshes preserve the
operator's own horizontal position instead of pulling the flow back.

Destructive confirmation dialogs make the safe secondary action the initial
keyboard focus. Standard confirmations may focus their explicit primary action;
informational dialogs open without a selected control.

The flow is part of its enclosing category, not an independent blue surface.
Its title, border, non-terminal completed circles and connectors leading to a
reached step inherit the containing category accent. Connectors leading to an
unreached, blocked or pending step remain neutral grey. In the
active-execution container the accent is monitoring cyan (`#65c5d9`). Terminal
success, blocked and failed states keep their dedicated semantic colours.
When an operator merge is pending, its persistent pull-request handoff card
spans the active-execution grid and sits directly below the lifecycle flow;
status, context and local-process cards follow it.
Lifecycle state, connector geometry, node interaction and the touch-safe
no-glass treatment are maintained as one stylesheet bundle.

The surrounding active-execution blocks remain one column while their own
container is narrower than **760px**. From that available width onward they
use two equal columns; the lifecycle still spans both columns. This is a
container query rather than a page-width rule, so the cards return to their
desktop layout as soon as their actual parent has room, including beside other
dashboard content.
The lifecycle step-detail modal uses that same monitoring-cyan accent for its
header, divider and border. Its field labels and phase names use the related
light-turquoise secondary accent, never the default purple label colour; it is
not a generic blue evidence modal. In dark mode, factual field values use the
shared modal ink, so they remain clearly readable against the dark surface.

The flow renders the server lifecycle projection as one coherent update: the
server-reported current step is the sole active circle. During an operator
merge wait, **Merge** is the active circle and the summary states the same
current step. An owner-authorized managed run has two distinct merge hand-offs:
the implementation PR's **Merge** circle, then, after **Finalization**, a
separate **Finalization merge** circle for its finalization PR. Each hand-off
uses its own pull-request number and reopens the handoff modal for that PR,
even though both belong to the same run. The projection omits merge circles
when persisted lifecycle evidence proves that the run did not require that
pull request: a no-PR run has none, an implementation-only run has one, and a
run with a finalization PR has two. Snapshots carry a source-scoped monotone
revision; the client
must apply them atomically and discard an older revision from the same source,
so it cannot retain a completed-state glyph from an older snapshot. Until GitHub
reports the pull request as merged and the Execution Host advances, an open
operator merge wait therefore renders **Merge** as the orange active circle,
with the summary explicitly saying that it waits for an operator merge, never
as a completed check or generically “active”. This remains true while GitHub
checks are queued or running: internal `WAIT_FOR_TERMINAL_EVIDENCE` polling is presented as
`WAIT_FOR_OPERATOR_MERGE`, and the persistent handoff card, **Open pull
request** and **Abort execution** controls must not disappear or flicker
between status updates. The handoff modal may open once per run and may be
dismissed by the operator; that does not hide the persistent card or controls.
The modal identifies the exact hand-off before its actions: whether it is the
implementation or finalization merge, its pull-request number, the run ID and
the submitted prompt title (with filename only as a fallback). This context is
compact, wraps safely on phone widths and uses the same localized terminology
as the lifecycle flow.

Each lifecycle node is an accessible detail control, not a glass or raised
card. Its modal presents only
persisted, run-scoped evidence: lifecycle state, observed start and finish
timestamps, repair iterations where recorded, and a split of the recorded
Execution Host phase durations. Repeated runtime spans are compacted to one
row per phase, with the accumulated duration and final recorded outcome; raw
span evidence remains available to telemetry and audits. Missing evidence is
explicitly shown as unavailable; the console never derives an end time or
duration from prompt content, polling cadence or UI state. Repair iterations
appear in this detail modal, never as a floating badge on the flow itself.

## 5. Controls

### Size and form

There are only three circular control sizes:

| Size | Use |
| --- | --- |
| **25px** | Inline message-copy affordance |
| **32px** | Compact glyph actions: copy, download, report, details, close where appropriate |
| **44px** | Primary touch controls, title-bar controls, modal actions and touch targets |

All round controls have the shared elevation shadow. Glyphs and button text
are non-selectable. A button uses a semantic class (`--download`, `--copy`,
`--destructive`, etc.) rather than a one-off colour override.

### Glyphs

Glyphs are a shared control language, not ordinary body text. Every
icon-only action, close control, category glyph, disclosure arrow and
decorative modal-title glyph uses the shared **bold** glyph weight. This makes
compact controls equally legible in both themes and at phone scale.

Neutral confirmation and evidence dialogs use the subdued information glyph
`ⓘ`, never an error-like exclamation mark. A real error dialog uses its own
`×` glyph, so error severity stays semantic rather than leaking into normal
operator hand-offs.

Keep the glyph weight scoped to the glyph itself: the adjacent action label
stays at its normal text weight. When an action has both a glyph and a label,
they form one horizontally and vertically centred group; the glyph may create
only the small leading gap required for recognition. Do not use a glyph alone
for a non-obvious action, and never make a text label bold merely because it
sits next to a glyph.

### Meaning and interaction

- **Orange** is the shared operational action and focus colour. Standard input
  focus borders and focus rings are orange in both themes.
- **Purple** identifies copy/AI interaction; **red/rose** identifies a
  destructive action; **green** identifies a healthy/restart action; **blue**
  identifies evidence/details. Hover fills preserve this semantic meaning.
- Touch controls use the temporary elevated glass press state, scale subtly
  while pressed and respect `prefers-reduced-motion`.
- Never use an icon alone where the action is not universally obvious or where
  a phone layout has enough room for its label. Tooltips and accessible labels
  are required for glyph-only controls.
- Disabled is an intentional state: keep the control visible, lower emphasis,
  use the wait cursor where an action is in progress and retain the reason in
  adjacent status or error copy.

### Forms and choice controls

Inputs, selects and text areas use the shared surface, border, orange focus
border and orange focus ring. Selected native select options should use the
house orange where platform styling permits. On phone widths, global options
live in the expandable title-bar panel; rows keep labels visible and switches
stack vertically. Switches draw their orange focus ring around the compact
track only, never around the full label row.

## 6. Modals and confirmations

Use the shared modal shell and contextual panel. Modal rules are:

1. The header is a tinted category surface with equal top and bottom visual
   padding, a category divider and the standard close control.
   A modal launched from a category must use that category's accent through
   the shared `--modal-parent-accent` contract. Dialogs are promoted outside
   their source DOM, so they cannot rely on CSS inheritance; the opening
   control resolves and supplies the source accent. A modal without a source
   retains its contextual default accent. The shared shell derives its
   secondary text and subcontainer surface/border colours from that same
   accent (`--modal-secondary-accent`, `--modal-subcontainer-surface` and
   `--modal-subcontainer-border`); it must never fall back to the global
   purple label colour. Thus a telemetry popup uses rose/pink secondary
   details, a monitoring popup turquoise details, and a conversation popup
   purple details. These secondary colours are exclusively for captions,
   dividers and subordinate surfaces: factual field values always use the
   standard modal document ink (dark in light mode, light in dark mode).
2. The document/content surface exactly matches the modal content surface;
   no contrasting “padding frame” may appear around an otherwise white or dark
   document.
3. Long content scrolls **below** the header. The scrollbar begins after the
   header divider, not against the modal top edge.
   Historical execution evidence is split into an **Execution** summary card
   and an **Execution context** card. On laptop/desktop widths, the summary
   and its supporting **Duration**, **Runtime**, **Git commit** and
   **Execution evidence** cards form the left column; context occupies the
   adjacent right column. They stack in source order at phone widths.
4. A state-changing action uses the shared confirmation dialog. Its copy says
   what changes, what remains, and any safe recovery path.
5. On an iPhone, every modal shell supplies at least `16px` outer padding
   (or the larger safe-area inset), its panel stays inside that area and its
   actions cannot fall below browser chrome. A family may widen that outer
   gutter only through a shell token (telemetry uses `24px`); it must not
   recreate a separate viewport, panel or header implementation. Background
   scrolling is locked while open.
6. Opening an evidence-only modal puts no control in focus. A standard
   confirmation may focus its available primary action, but a destructive
   confirmation focuses its safe secondary action. Close controls, titles and
   dialog shells never receive initial focus. The orange selected-control
   treatment is reserved for actual form inputs, selects and text areas.
7. User-facing errors use the shared dashboard error dialog. Do not use a
   browser-native `alert`, `confirm` or `prompt`: those surfaces are not
   themed, localizable or consistent with the operational focus contract.
   The dialog provides a localized title, error and recovery text, plus a
   standard dismiss control. Known preflight failures are translated by their
   meaning; unexpected redacted diagnostics use the generic localized error
   template.
8. An AI conversation modal has one purple divider beneath its descriptive
   copy. The embedded conversation component must not reintroduce its generic
   dark top border, margin or secondary ruler inside that modal.

## 7. Responsive and accessibility contract

At narrow widths (the implementation breakpoint is generally `620px`):

- Preserve readable labels and at least 44px touch targets for primary
  actions; use the mobile title-bar options disclosure for global settings.
- The expanded mobile title-bar options are flat rows. They and the locale
  picker never receive the generic direct-touch glass/card shadow; only the
  switch thumb retains its compact control elevation.
- Tables use a deliberate horizontal scroll region with styled scrollbars,
  rather than collapsing identifiers or action columns beyond recognition.
- Avoid fixed viewport-height panels that hide action buttons. Modals must
  retain a scrollable content region and reachable close/confirm controls.
- Respect safe-area insets, including iPhone landscape.

For every interaction:

- keyboard focus is visible and uses the shared orange contract;
- labels, title and `aria-label` are localized;
- glyphs have a text equivalent;
- motion has a reduced-motion fallback;
- contrast and state do not rely only on colour;
- selectable text is limited to evidence/content, not controls or glyphs.

## 8. Localization and content rules

All user-facing console strings are catalogued in
`assets/dashboard_locales.mjs` for **en, nl, de, fr and es**. New copy must
ship in all five language blocks and must not introduce a visible string
literal into `dashboard.js`. Keep operation language concrete:

- Say **execution**, **queue**, **Execution Host**, **workspace** and
  **operational evidence** rather than legacy `prompt` language.
- State failure reason and safe recovery in the UI/log detail; never expose
  secrets, prompt bodies or raw sensitive diagnostics.
- Use one sentence for category descriptions. Use direct verb labels for
  actions: for example *Retry execution*, *Defer*, *Restore* and *Copy visible
  log entries*.
- Error-dialog title, dismissal and preflight recovery copy are part of the
  same five-language contract. A browser-supplied default label is never an
  acceptable fallback for console feedback.

## 9. Required design-review checklist

Before accepting a console UI change, review it against this list:

1. Does it use an existing semantic surface, accent, control size and action
   class? If not, is the new token documented here?
2. Are dark and light mode equally intentional, including hover, focus,
   selected, disabled and error states?
3. Does it work in collapsed and expanded category states, on desktop and
   iPhone portrait/landscape?
4. Is the action reversible or confirmed? Does its copy state what is retained?
5. Are aria labels, keyboard focus, non-selectable glyphs and reduced-motion
   behavior correct?
6. Are all five localizations present and free of legacy terminology?
7. Does a table/log action operate on exactly the state the user can see?

## 10. Required verification

Every console change must include focused tests and run the appropriate full
checks before review:

```sh
CI=1 npm run test:engineering-dashboard -- --reporter=line
git diff --check
```

The current regression layers are deliberately complementary:

- `tests/engineering/test_inbox_watcher.py` verifies the safe filesystem and
  queue projection contract, including collision-safe deferral.
- `tests/engineering/test_dashboard.py` verifies dashboard HTTP validation,
  response codes and audit-log payloads.
- `tests/engineering/dashboard.spec.mjs` verifies the rendered interaction:
  confirmation, cancellation, localization, responsive/touch behavior and the
  state the operator can actually see.

Add or extend Playwright coverage for the changed state and, when applicable:

- dark and light rendering;
- mobile rendering and direct touch;
- confirmation and error/retry paths;
- dashboard-native error feedback, including an assertion that no native
  browser dialog was invoked and no `alert()` call can be reintroduced;
- localized labels in all five supported languages;
- selected, hover, focus and disabled states;
- sorting/filtering/pagination semantics for tables and logs.
- the prompt-history page with its longest rendered status, ensuring the
  status column fits and the title contracts before it overlaps.
- selected rows in every affected table: verify the contiguous row treatment,
  the single leading marker and the absence of per-cell focus/selection
  outlines;
- all modal close controls and title/disclosure glyphs: verify the shared bold
  glyph weight in both themes;
- lifecycle flow geometry: verify connector visibility, its layer behind the
  node, fixed connector length, exact vertical centre alignment, inherited
  containing-category accent, standard label colour and a coherent
  active/completed/pending projection, including that an open operator merge
  wait renders the applicable Merge circle as active rather than completed, retains its handoff
  controls while checks are queued or running, and lifecycle nodes stay free
  of generic touch glass/transitions;
- AI conversation modals: verify the purple descriptive divider remains and
  no inherited secondary divider is rendered;
- numeric precision: verify locale-aware percentage output with exactly one
  fractional digit.

For a visual change, capture and review at minimum: desktop dark, desktop
light, iPhone dark expanded, and iPhone light expanded. A passing test suite
does not override an obvious visual regression.

## 11. Change protocol

1. Start from the tokens and patterns above; design the smallest coherent
   extension.
2. Implement semantic markup, localized copy and the shared CSS/JS component
   pattern together.
3. Add behavioral and visual regression coverage.
4. Review against sections 7–10.
5. If a stable new pattern is introduced, amend this document in the same
   change. Otherwise, remove the one-off pattern before merge.
