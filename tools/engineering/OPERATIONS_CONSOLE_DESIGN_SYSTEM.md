# Engineering Operations Console — Design System

**Status:** Canonical code-derived baseline  
**Scope:** Private Engineering Operations Console (`tools/engineering/dashboard.py`, `assets/dashboard.css` and `assets/dashboard.js`)  
**Last reconciled:** 2026-08-08

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
| Selected-control border | `2px` orange outline with a `4px` soft ring | Inputs, selects and text areas only |
| Console blue | `--operations-console-blue: #0a6b9d` | Header/identity accent, not a replacement for semantic category colour |
| Mark blue | `--operations-console-mark-blue: #00b8f4` | Product mark only |

### Category accents

The accent belongs to the information domain and is used consistently for the
category border, heading/glyph, divider and focus treatment.

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
yellow/orange, failure red/rose and activity as the orange animated ring.

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
illegible columns. Selected history rows and sortable table headers show a
thin, unbroken `1px` selected edge inside their own cells. This keeps the
first row directly under the table header and sticky headers fully bounded
without drawing across adjacent cells.

Repeated compact evidence, such as specialist reviewer status, uses an
auto-fitting grid of at least `180px` tiles. It fills a row when space permits
and wraps cleanly at narrower widths; never hard-code a single wide tile per
reviewer.

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
stack vertically.

## 6. Modals and confirmations

Use the shared modal shell and contextual panel. Modal rules are:

1. The header is a tinted category surface with equal top and bottom visual
   padding, a category divider and the standard close control.
2. The document/content surface exactly matches the modal content surface;
   no contrasting “padding frame” may appear around an otherwise white or dark
   document.
3. Long content scrolls **below** the header. The scrollbar begins after the
   header divider, not against the modal top edge.
4. A state-changing action uses the shared confirmation dialog. Its copy says
   what changes, what remains, and any safe recovery path.
5. On an iPhone, the modal stays within safe areas and its actions cannot fall
   below browser chrome. Background scrolling is locked while open.

## 7. Responsive and accessibility contract

At narrow widths (the implementation breakpoint is generally `620px`):

- Preserve readable labels and at least 44px touch targets for primary
  actions; use the mobile title-bar options disclosure for global settings.
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
- localized labels in all five supported languages;
- selected, hover, focus and disabled states;
- sorting/filtering/pagination semantics for tables and logs.

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
