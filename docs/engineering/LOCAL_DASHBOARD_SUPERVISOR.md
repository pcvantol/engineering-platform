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
port-forwarding, pull requests, releases or deployments. The Inbox watcher is
a separate repository-owned LaunchAgent and has no authority beyond the normal
Engineering Platform lifecycle.

## Dashboard module boundaries

`tools/engineering/dashboard.py` is the intentionally thin dashboard façade:
it owns the private HTTP endpoints, security headers, server lifecycle and
LaunchAgent commands. `tools/engineering/dashboard_state.py` owns the
read-only status and server-sent-event snapshot composition. The façade passes
its bounded repository readers into that state module, preserving the existing
endpoint contracts while keeping the dashboard state model independently
testable. Neither module has authority to start engineering work or change a
target repository.

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
- `.engineering/reports/` supplies the Engineering Report and its advisory
  analysis for the matching terminal run; and
- `engineering_component_logs` in `.engineering/engineering.db` supplies
  bounded, redacted component-log tails that are automatically refreshed when
  their server-pushed revision changes.

iCloud Drive is solely an Inbox transport source for the separate watcher.
The dashboard does not read iCloud reports, status or archived prompts.

The page receives status changes through server-sent events. A browser refresh
remains safe, but periodic polling is not the source of truth. The dashboard
shows the Engineering Platform version in its status bar. The
component-health category shows the dashboard and watcher versions beside their
live health state, so a maintainer can distinguish a stale local service from a
stale browser page. The dashboard, Inbox watcher and dashboard relay also
report their locally observed uptime. Storage and private external access have
no process-owned uptime metric, so the dashboard intentionally does not invent
one for them.

## Browser state and evidence views

The title bar provides three browser-local controls: theme, section expansion
and automatic refresh. Their values, open category state, table filters,
sorting and pagination remain in the browser during a server-pushed status
update. With automatic refresh disabled, the visible state remains static until
the maintainer refreshes or re-enables it. These are presentation preferences;
they never alter an Engineering run or its evidence.

The **Laatst uitgevoerde prompt** and **Promptgeschiedenis** views fetch report
content only when a maintainer opens it. A delivered report can be viewed in a
large read-only Markdown dialog, copied to the clipboard or downloaded as a
Markdown file. The advisory AI analysis uses the same bounded interaction.
When a report or analysis does not exist, the dashboard states that explicitly
and does not present a download or copy control. Copy confirmation is a local
toast only; it does not send report content to another service.

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
dashboard process, Inbox watcher LaunchAgent, private relay LaunchAgent, local
status storage and Tailscale connectivity are all available. It returns HTTP
`200` with `"health":"ok"` when all components are healthy, otherwise HTTP
`503` with `"health":"degraded"` and a per-component diagnostic. The endpoint
is read-only and does not repair a component.

The matching **Platformonderdelen** dashboard category exposes a per-component
information dialog. It obtains its bounded metadata from
`GET /api/components/<component>/details`: the local host, executable or
LaunchAgent configuration, current build commit, observed process memory and,
when a component owns a local process, its uptime.
For only `dashboard`, `inbox_watcher` and `dashboard_relay`, an explicitly
confirmed `POST /api/components/<component>/restart` schedules a local
`launchctl kickstart -k` for the fixed owned label. The request accepts only an
empty JSON object, is same-origin, and never accepts a browser-supplied command
or executable. Storage and Tailscale entries are diagnostic-only. The restart
control cannot run engineering, claim Inbox work, or affect repository,
release, deployment or publication state.

## Browser validation

The Engineering Platform validation workflow runs a Playwright Chromium smoke
test against a locally started dashboard. It uses an iPhone-sized viewport and
checks the private status surface, workspace category and collapsed completed
prompt category. Run the same validation locally with:

```sh
npm ci
npx playwright install chromium
npm run test:engineering-dashboard
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

**AI-gesprek** is available only through this same private listener. Its
bounded context is the repository identity, the latest terminal prompt and
the matching local Engineering Report. The visible interface is
provider-neutral; the current configured adapter is Codex CLI and is shown as
such. It starts an ephemeral, read-only process and cannot inspect or submit
Inbox files, modify a repository,
create or merge pull requests, or trigger release, deployment or publication.
Any requested implementation must be submitted as a new Engineering prompt.

Conversation history is browser-session-local and is reset when the displayed
last-executed run changes. It is not Engineering Memory and is never an Inbox,
runner or repository-control channel.
