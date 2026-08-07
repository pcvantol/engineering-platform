# Local Dashboard Supervisor

The repository-owned Engineering Dashboard is the supported macOS service for
the private status page. Its per-user LaunchAgent starts the dashboard on
loopback only. A separate repository-owned relay binds port `8765` only on the
workstation's explicit Tailscale IPv4 address and forwards it to that loopback
listener. Neither component uses a wildcard, LAN or public listener.

`dashboard_supervisor.swift` is compiled locally as the relay during the
canonical dashboard installation. Use the repository-owned dashboard commands
instead:

```sh
./tools/engineering/dj-engineering-dashboard doctor
./tools/engineering/dj-engineering-dashboard install
```

The dashboard never creates public listeners, Funnel configuration, ACLs,
port-forwarding, pull requests, releases or deployments. The **Engineering
Execution Host** is the repository-owned local service that claims prompts,
executes the bounded Codex workflow and produces the resulting local reports.
Its Inbox watcher is the transport and queue-facing part of that host; it has
no authority beyond the normal Engineering Platform lifecycle.

The watcher admits at most one execution and then starts its runner as a
separate local process. It does not wait for that runner: subsequent polling
cycles keep scanning the Inbox and publishing the ordered queue while the
admission record prevents a second execution from starting. A terminal
checkpoint clears that admission record; the historical run and its report
remain the source of evidence.

The detached runner never owns the polling lock for its engineering lifetime.
The watcher may therefore refresh only the bounded queue projection while the
runner is active, preserving the active Run ID and runner phase in the same
status snapshot. This scan is read-only: a newly discovered Inbox prompt is
shown immediately in **Inbox-wachtrij**, but is not claimed until the active
execution reaches a terminal checkpoint. During an in-place upgrade, a watcher
may encounter one older runner that still owns the previous lock; it still
performs that read-only queue refresh and never starts a second runner.

## Dashboard module boundaries

`tools/engineering/dashboard.py` is the intentionally thin dashboard façade:
it owns the private HTTP endpoints, security headers, server lifecycle and
LaunchAgent commands. `tools/engineering/dashboard_state.py` owns the
read-only status and server-sent-event snapshot composition. The façade passes
its bounded repository readers into that state module, preserving the existing
endpoint contracts while keeping the dashboard state model independently
testable. The page markup remains in the façade; `tools/engineering/assets/
dashboard.css`, `dashboard.js` and `dashboard_locales.mjs` own presentation,
browser interaction and user-facing copy. The locale module is the canonical
catalog for the five supported language families (`en`, `nl`, `de`, `fr`,
`es`); it normalizes a browser preference, persists an operator selection and
falls back explicitly to English. These assets are served only as same-origin
dashboard assets. Neither module has authority
to start engineering work or change a target repository. The small
`dashboard_status_store.mjs` module owns only status/snapshot normalization and
single-path delivery to the renderer; it has direct Node unit tests. Browser
behaviour, markup and styling remain covered by Playwright rather than fragile
source-text assertions.

`dashboard_locales.mjs` also owns the dashboard locale service. Renderers use
that single service for UI copy, Amsterdam date/time formatting, numeric
formatting, casing, pluralisation and collation; they must not select a locale
or construct an `Intl` formatter themselves.

The Promptgeschiedenis detail endpoint follows the same boundary: its lookup
reads one immutable terminal history row and bounded companion data, while a
small dashboard projector owns the JSON presentation and report-derived
Evidence Bundle summary. The projector works only with data for that selected
Run ID, copies the stored history before adding display-only provenance, and
does not write storage or modify a report. A missing or non-readable report
therefore yields no derived evidence rather than a fallback from another run.

The Execution Host continues to publish stable machine enum values such as
`PASS`, `RETRYABLE_AFTER_HOST_REPAIR` and `CAPABILITY` in its local status
contract. The dashboard translates those values only at the presentation
boundary through the same locale catalog, so the operator sees a human-friendly
label in the selected language while API and storage values remain stable.

The dashboard assets also own the presentation contract: the sticky title bar
uses the same `18px` outer corner radius as the top-level categories, while
the category colour remains the visual source for interactive actions. A
hovered action fills with the colour of its containing category and uses the
matching readable foreground. Light mode gives evidence and log actions a
light resting surface before that hover fill. This includes the circular
download controls; their stroke is a single, solid border in both themes.
The Engineering Status SVG and touch PNG are same-origin assets. They are
served with `no-store` so an actively running local dashboard never needs to
reuse an outdated visual asset after an upgrade.

The title bar also contains a compact circular **Page refresh** glyph. It uses
the same reload operation and visible refreshing feedback as pull-to-refresh;
it is a browser-surface refresh only and never changes Engineering execution
state, scheduling or evidence. Its accessible name is supplied by the
five-language dashboard catalogue. On iPhone and iPad, `touch-action:
manipulation` prevents accidental double-tap zoom; the dashboard viewport is
also held at its initial scale while handling text input.

Top-level dashboard categories are separated by a `24px` rhythm and do not
use elevation shadows; their coloured borders and spacing provide hierarchy.
The Dashboard UI component layer groups header, main-section, scrolling/focus,
log-control and modal refinements, with shared spacing, focus and surface
variables rather than late one-off overrides. Its shared modal shell owns the
header ruler, neutral close control and contextual accent; individual modal
families provide only their geometry and content layout.
The dedicated scroll region has a symmetric content gutter. The document body
alone owns the iOS safe-area inset, so landscape rendering never reserves that
right-hand space a second time for an overlay scrollbar. On short mobile
landscape viewports the extra desktop inset is removed: the notch or Dynamic
Island retains its system safe area plus an `8px` content gap only.
Every dialog type uses those same left and right safe areas independently in
landscape, so an asymmetric Dynamic Island can never cover a dialog border or
its panel padding. One compact landscape contract also owns the component
modal width and the AI chat's single-line composer and action-strip spacing;
those responsive rules are not repeated as late per-modal overrides.
The compact confirmation panel is centred within that safe width rather than
being aligned to the left edge of its full-width dialog shell.
The read-only AI chat composer uses a single-line input with an adjacent send
button on short iPhone landscape viewports, preserving room for the
conversation rather than reserving the portrait multi-line composer height.
Prompt History keeps its terminal status column at a readable fixed width in
iPhone landscape, including when a Retry or Dismiss action button is visible.
When a terminal report contains a Forge Mission Recommendation Handoff, its
Prompt History detail renders a compact read-only recommendation card. The
card uses the five-language catalogue, has a textual status and confidence,
wraps long artefact paths and dependencies, and exposes alternatives through a
native keyboard-accessible disclosure. It contains no approval, Mission
creation or Mission-start control.
Touch input controls use a `16px` text size and the iOS viewport is locked to
its initial scale, so Safari cannot zoom the entire dashboard when the keyboard
opens.
The original scroll position is restored after an iPhone keyboard is dismissed,
so focusing a field does not leave the dashboard stranded down the page.
On iPhone portrait the dashboard uses native document scrolling, so the title
bar scrolls out of view with the content instead of competing with a nested
scroll region.
When any dashboard modal is open, its scroll area remains usable while the
background page is fixed in place and restored to its prior position on close.
In the AI conversation modal, the new-question label sits directly above the
composer, while the transient thinking status is right-aligned beside the
used-model metadata beneath the send button.
The chat action strip provides download, copy-to-clipboard and destructive
clear actions; copying uses the same iOS-safe clipboard fallback as individual
chat messages. These actions use the shared semantic `download`, `copy` and
`destructive` variants, so reports, chat and component logs cannot drift into
ID-specific presentation. Every download glyph uses the same generic orange
transport colour, while destructive clear glyphs and their confirmation title,
glyph and ruler use red. Modal close controls are intentionally neutral grey
so they do not compete with an operational action. Repository and workspace
state codes are rendered as readable labels through the five-language
catalogue; the raw protocol values remain unchanged in Engineering data.
At narrower widths the title-bar controls move to their own wrapping row
before they can overlap the dashboard title. Labels above vertical input and
select controls retain an `8px` gap before a focus outline. Component logs
provide text search, level and multi-select event filtering; the adjacent
reset glyph clears those filters without changing the selected sort order.

Component logs use one client-side pipeline: parse once, derive the available
event values, apply the current filters, sort per table column and then
paginate. There is no separate legacy renderer or global sort control to
override that result.

During an active execution, the Execution Host publishes a bounded reviewer
runtime projection with only reviewer identity, capability, selection reason,
state and lifecycle timestamps. The dashboard uses this projection to show the
live specialist-reviewer count (for example, `2 of 3` active). It does not
infer reviewer activity from the user's unrelated Codex processes. Reviewer
observations remain read-only advisory input; the primary runner alone retains
lifecycle authority.

CPU and process-count telemetry is also run-bound. When the Execution Host
starts the foreground Codex runner, it records that runner's PID and dedicated
process group; the record is removed when the runner exits. The dashboard
counts only processes in that recorded group. Other Codex sessions on the
operator's machine are deliberately excluded.

AI-provider usage follows the same exact-run rule. While a run is active, its
usage card remains empty until the CLI has published usage for that active Run
ID; usage from the previously completed run is never reused as a live value.

## ESET Firewall

When ESET Cyber Security controls the macOS firewall, it must explicitly allow
the locally compiled relay to accept incoming TCP traffic on port `8765` from
authorized Tailnet devices. The relay path is stable for this checkout:

```text
/Users/pcvantol/Documents/GitHub/djconnect/.engineering/bin/engineering-dashboard-relay
```

Use an inbound allow rule scoped to the Tailscale address range
`100.64.0.0/10`, or to the trusted Tailscale network zone where ESET offers
that scope. Do not disable the firewall and do not add a wildcard, LAN or
public rule. The loopback dashboard itself does not need a network exception.

After changing the rule, open `http://<this-mac-tailscale-ip>:8765/` from the
iPhone. On the Mac itself, use `http://127.0.0.1:8765/`.

## Local status, reports and logs

The dashboard reads only canonical local Engineering Platform state. It does
not need iCloud Drive to render a current or completed run:

- `.engineering/status/status.json` supplies bounded watcher status;
- `.engineering/status/current.json` adds live execution detail only while its
  Run ID has no terminal watcher result or terminal checkpoint. A terminal
  watcher result for the same Run ID always wins, so a stale live checkpoint
  cannot leave a completed, blocked or failed run visible as **Actieve
  prompt**;
- `.engineering/status/host_preflight.json` supplies the compact Execution Host
  Preflight outcome and latest timestamp; it never exposes check internals;
- `.engineering/status/workspace_preflight.json` supplies the compact Workspace
  Preflight projection.
- `.engineering/status/capability_preflight.json` supplies bounded Capability
  Preflight outcome, Recoverability, Failure Origin and operator recommendation.
- `.engineering/reports/` supplies the Engineering Report and its advisory
  analysis for the matching terminal run; and
- `engineering_component_logs` in `.engineering/engineering.db` supplies
  bounded, redacted component-log tails that are automatically refreshed when
  their server-pushed revision changes.

iCloud Drive is solely an Inbox transport source for the separate watcher.
The dashboard does not read iCloud reports, status or archived prompts.

Prompt History and its read-only execution-detail dialog expose the stored
Producer ID, Type, Version, Correlation ID and optional Mission and Engineering
Action IDs for the selected run only. Producer metadata is audit evidence: it
cannot be edited in the dashboard and never affects execution or scheduling.

At every page load, the dashboard first retrieves one complete, read-only
same-origin status snapshot. This fills all categories even when the
browser-local **Automatisch vernieuwen** preference is off. The page then
receives subsequent status changes through server-sent events only while that
preference is enabled; periodic polling is not the source of truth. A browser
refresh remains safe. The dashboard shows the Engineering Platform version in
its status bar. The
latest server snapshot is held in one browser-local status store. Each update
uses one explicit render path for status, component logs, chat context,
component details, execution evidence and health invalidation; browser-local
controls do not wrap or replace that path. The
component-health category shows the dashboard and watcher versions beside their
live health state, so a maintainer can distinguish a stale local service from a
stale browser page. The dashboard, Engineering Execution Host and dashboard
relay also report their locally observed uptime. Storage has no process-owned
uptime metric, so the dashboard intentionally does not invent one for it.
Private external access is not a dashboard component or an information-dialog
item.

## Browser state and evidence views

The title bar provides four browser-local controls: page refresh, theme,
section expansion and automatic refresh. Page refresh is deliberately the
same operation as pull-to-refresh, so both paths show the same feedback before
reloading the current browser page. Their values, open category state, table
filters, sorting and pagination remain in the browser during a server-pushed
status update. With automatic refresh disabled, the visible state remains
static until the maintainer refreshes or re-enables it. These are presentation
preferences; they never alter an Engineering run or its evidence.

On iPhone, the theme, section-expansion and automatic-refresh switches are
separate direct-touch controls. Each control has `touch-action: manipulation`
and persists only its own browser-local setting. Playwright covers them with
real touch input one at a time, checks the visible state after every touch,
checks the persisted client state and verifies the same state after reload.
This guards against a visual pressed state that does not actually change the
Operations Console preference.

## Git workspace lock status

**Operationeel overzicht** exposes the Git index-lock state as a compact,
read-only operational signal. **Vrij** means no `.git/index.lock` is present.
**Actief** means a lock exists and new executions may be waiting while Git
finishes another action. The card is intentionally not a general Git-process
manager and never exposes process details or arbitrary repository commands.

The optional recovery action appears only when the lock is demonstrably stale:
it must be at least five minutes old and `lsof` must confirm that no process
owns that exact lock file. If `lsof` is unavailable or cannot determine
ownership, the dashboard fails closed: it reports the lock as active and does
not offer recovery. The confirmation action removes only that stale
`.git/index.lock`; it does not switch branches, restart a service, mutate an
Inbox item or alter the queue.

**Promptgeschiedenis** is the sole entry point for terminal execution detail.
Selecting a table row opens a near-fullscreen, read-only detail dialog with
the evidence, timing, runtime provenance, token usage, commits and reviewer
results that belong to that exact Run ID. Engineering reports and advisory AI
analyses stay separate row actions: they open their own Markdown dialogs and
can be downloaded without duplicating their content in the detail dialog.
The dashboard has no separate or hidden **Laatst uitgevoerde prompt** card;
the selected Promptgeschiedenis row is the canonical presentation of a
terminal execution.
The adjacent AI-chat glyph opens a separate near-fullscreen, read-only
question-and-answer context for that same Run ID. It receives only the selected
run's bounded evidence and cannot start engineering work or alter repository
state. Its browser-session history is isolated per Run ID.
When an artifact does not exist, the dashboard states that explicitly and does
not present its action. Copy confirmation is a local toast only; it does not
send report content to another service. On iPhone, a legacy clipboard fallback
places its temporary selection inside the active dialog, because iOS marks the
page behind a modal as inert; chat, report and detail copy actions therefore
remain available without changing the evidence data.

The **Actieve prompt** category represents only a live, non-terminal
execution. Terminal evidence belongs in **Promptgeschiedenis**. When an Inbox
predecessor blocks later queued work, the category remains available for that
queue-recovery context and shows the predecessor recovery action and retry
control.

A retry in the Inbox is shown as queued without a Run ID. The watcher assigns
and exposes its immutable Run ID only after it has passed admission and the
retry has become an active execution.

The **Actieve prompt** category is always the first dashboard category, ahead
of the Inbox queue and Prompt history.

The bottom status bar contains the Engineering Platform version, the most
recent refresh timestamp and the server-push connection state. The active
prompt category contains no separate time card.

On desktop and iPad, the title bar and bottom status bar remain visible while
dashboard categories scroll in one dedicated content area. On iPhone portrait,
the title bar joins that content area and scrolls out of view to recover
vertical reading space; the bottom status bar remains visible. Workspace
metadata remains a top-level, collapsible operational category immediately after **Technische
details**, preserving its own independent status and
evidence boundary.
The title-bar section switch persists the deliberate all-open or all-closed
choice across a browser reload; a later status update cannot reverse it.

## Confirmed actions

Every state-changing dashboard action uses the in-app confirmation dialog. The
dialog is modal; a backdrop click does not dismiss it, while **Escape** has the
same explicit negative result as **Annuleren**. It opens with focus on the
dialog shell rather than on either action, so neither button is preselected.
For ordinary confirmations, the primary action and dialog ruler/border use the
triggering category accent. A destructive confirmation switches the shared
modal-shell accent to red, including its title glyph, ruler, border and action
hover treatment. The header close control is the same negative result as
**Annuleren**. This applies to reset-credit use, component restarts, log
clearing, predecessor retry and clearing the AI conversation. Native browser
confirmation and alert popups are not part of the supported interaction
contract.

The AI conversation's **Chat wissen** glyph clears only its browser-session
view. It never changes Promptgeschiedenis, a delivered report or any Inbox
item. Download and clear controls reserve their own row above the first chat
bubble so they cannot overlap evidence content. Informational component and
report dialogs likewise receive programmatic focus on their neutral dialog
shell when opened; only a deliberate keyboard interaction highlights a
control. Report download, copy and close controls remain pinned in the modal
header while its document content scrolls.

## Component lifecycle audit trail

The owned dashboard and Inbox watcher write an `INFO` lifecycle record when
they start and when their orderly shutdown completes. Each record carries only
bounded component identity: component version, short repository build commit,
fixed LaunchAgent label and LaunchAgent plist path. A dashboard-initiated
component restart also records the requested fixed component name before the
owned `launchctl kickstart -k` call is made. Signal receipt is recorded before
the normal shutdown record when macOS asks an owned component to stop.

This audit trail is operational evidence, not repository truth. It is stored
through the same redacted SQLite logging contract as other component events.
It never includes prompt bodies, credentials, account data, arbitrary commands
or browser-supplied executable paths. The process-level LaunchAgent output
streams remain the fallback source for failures that occur before the SQLite
logging layer is available.

## Component health endpoint

`GET /health` returns JSON for unattended checks. It is healthy only when the
dashboard process, Engineering Execution Host LaunchAgent, private relay
LaunchAgent, local status storage and relay connectivity are all available. It returns HTTP
`200` with `"health":"ok"` when all components are healthy, otherwise HTTP
`503` with `"health":"degraded"` and a per-component diagnostic. The endpoint
is read-only and does not repair a component.

The matching **Platformonderdelen** dashboard category exposes a per-component
information dialog. It obtains its bounded metadata from
`GET /api/components/<component>/details`: the local host, executable or
LaunchAgent configuration, current build commit, observed process memory and,
when a component owns a local process, its uptime.
Each complete component row opens this same dialog; the trailing information
glyph is its visual affordance rather than a separate required target. The row
is keyboard-operable with Enter and Space.
For only `dashboard`, `inbox_watcher` and `dashboard_relay`, an explicitly
confirmed `POST /api/components/<component>/restart` schedules a local
`launchctl kickstart -k` for the fixed owned label. The request accepts only an
empty JSON object, is same-origin, and never accepts a browser-supplied command
or executable. Storage and Tailscale entries are diagnostic-only. The restart
control cannot run engineering, claim Inbox work, or affect repository,
release, deployment or publication state.

Restarting the dashboard itself first shows the same full-page loading splash
used for an ordinary dashboard load, then reloads the browser after the owned
restart request has been accepted. This only refreshes the private browser
surface; it neither changes Execution Host work nor broadens the restart
authority.

## Browser validation

The Engineering Platform validation workflow runs a Playwright Chromium smoke
test against a locally started dashboard. It uses an iPhone-sized viewport and
checks the private status surface, workspace category and collapsed completed
prompt category. Its localization coverage switches through all five supported
languages and verifies that visible interface copy, template and web-app-title
bindings, dynamic dialogs, pull-to-refresh and title-bar refresh feedback,
downloadable chat labels, accessibility names and rendered preflight enum
labels change with the selected language. Run the same validation locally with:

```sh
npm ci
npx playwright install chromium
npm run test:engineering-dashboard
npm run test:engineering-dashboard-logic
```

CI runs the browser suite with four isolated workers. Each worker starts its
own temporary dashboard root and local server, so status fixtures, browser
preferences and retry projections never leak between tests. Local runs retain
Playwright's default worker count for straightforward debugging.

The same workflow also runs the Engineering Python suite under branch coverage.
The required core files are `dashboard.py`, `platform_bootstrap.py`,
`providers.py` and `inbox_watcher.py`. Each must remain
strictly above 80%; an exactly 80.00% result fails the quality gate. To
reproduce the measurement locally:

```sh
coverage run --branch -m unittest discover -s tests/engineering
coverage report --include='tools/engineering/dashboard.py,tools/engineering/platform_bootstrap.py,tools/engineering/providers.py,tools/engineering/inbox_watcher.py'
```

## Dashboard interpretation and interaction

The status page uses category accents to make unrelated evidence visually
distinct: green for local capacity and usage, blue for completed evidence,
orange for diagnosis and application logs, and purple variants for execution
context, technical details and the advisory conversation. A colour never
changes lifecycle meaning; the prompt status indicator remains the authoritative
visual outcome.

The **Logs** section automatically keeps the redacted JSON records current
through server-push revisions and parses them locally into selectable, copyable
tables. Search and level filtering are client-side. Clicking a column heading
sorts that table and shows the active ascending or descending direction; the
subtle line number is not a server-side log identifier. Startup, restart and
orderly-shutdown lifecycle events are visible there as `INFO` records.
Each watcher and dashboard table has independent filtering, sorting and
pagination, and its own download and confirmed-clear controls. Downloaded logs
remain redacted NDJSON. A missing log is presented as an empty log, never as an
invalid JSON record.

All dashboard actions use the same interaction language: the shared semantic
download, copy and destructive glyph variants retain their own readable
resting surfaces and hover fills across chat, reports and component logs.
The log actions therefore do not inherit the dark log-card surface in light
mode. This is presentation-only and has no effect on the download or clear
endpoint contracts.

## Codex resetcredit

When the local Codex account reports one or more available resetcredits, the
**Resterend gebruik** category shows a **Gebruik reset** button. It invokes the
installed Codex CLI's account reset-credit operation and consumes exactly one
credit after an explicit browser confirmation. The button is hidden when no
credit is available. This is the dashboard's sole account-side effect: it
cannot access the Inbox, alter repository files, start a runner, create a pull
request, merge, release or deploy.

Above the quota values, the category identifies the active AI provider and the
locally detected CLI version. This identity is cached briefly, excludes paths
and account data, and remains visible even when quota retrieval is unavailable.

The active and last execution cards use provider-neutral wording. The last
execution card reads the exact report-bound runtime provenance for that run:
**Runtime Provider**, **AI Model**, any reported **Reasoning Profile** and
**Configuration Profile**, plus **Codex CLI Version**. Values that the CLI did
not report remain explicitly unavailable; the dashboard never substitutes the
current provider configuration or guesses a model. This preserves a future
provider abstraction without concealing which adapter executed the displayed
transaction.

The always-visible **Inbox-wachtrij** shows the current queue even when it is
empty. Entries are numbered in their real execution order: oldest file
modification timestamp first, with filename, prompt title and modification time.
The status projection is intentionally bounded to the first 25 queued prompts;
when more are waiting, the dashboard says so explicitly.

## Accessibility

The dashboard targets WCAG 2.2 AA for its private status interface. It exposes
a Dutch document language, a keyboard-visible skip link, semantic headings and
native controls, persistent focus indication, and touch targets of at least
44 by 44 CSS pixels for actions. Status changes and chat results use polite
live regions; log tables have accessible names and keyboard-operable sortable
headers. Motion is reduced when the operating system requests it, and forced
colours retain visible focus.

Automated checks cover the accessibility contract embedded in the page. Before
claiming complete conformance, also perform a manual keyboard and screen-reader
review in the supported browser and a contrast review of the rendered page.

## Private read-only AI advice

**AI-gesprek** is available only through this same private listener, from the
AI-chat glyph in a selected **Promptgeschiedenis** row. Its bounded context is
the repository identity, that exact terminal prompt and its matching local
Engineering Report. The visible interface is
provider-neutral; the current configured adapter is Codex CLI and is shown as
such. It starts an ephemeral, read-only process and cannot inspect or submit
Inbox files, modify a repository,
create or merge pull requests, or trigger release, deployment or publication.
Any requested implementation must be submitted as a new Engineering prompt.

Conversation history is browser-session-local and scoped by Run ID, so one
terminal prompt can never supply conversation context to another. It is not
Engineering Memory and is never an Inbox, runner or repository-control channel.

During an active prompt, the execution card can present a bounded, safe
**Huidige Codex-activiteit** label such as planning, reading files, editing or
testing. It is progress metadata, not a reasoning trace: raw prompts,
chain-of-thought, tool inputs and tool output are never rendered.

The Execution Host derives this label only from an allowlist of Codex JSONL
item types. It may describe a phase such as planning, running a command,
editing files, researching or preparing the result, but it never persists or
forwards event text, command text, paths, arguments or output.
