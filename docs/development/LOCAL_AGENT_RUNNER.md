# Local Agent Runner

`engineering-execution-host` starts one foreground, bounded engineering transaction from this
repository. It is local-only developer tooling, not a product capability, CI
system, release engine, merge authority, daemon or remote control plane.

## Runner module boundaries

`src/engineering_platform/execution_host.py` is the foreground lifecycle orchestrator.
It owns mode selection, repository and GitHub reconciliation, agent invocation,
terminal checkpoints and command-line integration. Small, independently tested
local responsibilities are kept outside that orchestrator:

- `codex_observability.py` extracts explicitly emitted usage and runtime
  provenance and persists bounded per-run usage;
- `engineering_memory.py` owns advisory, local Engineering Memory persistence;
  and
- `live_status.py` atomically projects the current local status consumed by
  Engineering Status.

The Execution Host has one canonical Python module and command:
`engineering_platform.execution_host` and `engineering-execution-host`.
They are the only supported integration points and own the lifecycle authority,
repository truth and runner command contract.

## Engineering Platform versioning

`src/engineering_platform/ENGINEERING_PLATFORM_VERSION.json` is the canonical,
deterministic Engineering Platform manifest. It versions the engineering
environment independently from the repository and declares the platform,
runner, watcher and dashboard versions; Bootstrap Contract; checkpoint,
memory, report, status-model and Inbox Protocol formats; and the minimum Codex
CLI version. Engineering Platform `2.0.0` is the current minimum supported
platform for future engineering prompts.

The local Inbox worker and private Dashboard are separately versioned
components of Engineering Platform 2.0. Their current versions are the
canonical `watcher_version` and `dashboard_version` manifest fields; neither
is a separate Engineering Platform release. The private dashboard displays
them with the corresponding live components, while its status bar displays the
Engineering Platform version and Git commit.

At runner startup, `engineering-execution-host` reads the manifest and rejects an unsupported
platform major version, older runner, older Bootstrap Contract, unsupported
checkpoint/memory/report format or unsupported Codex CLI. Diagnostics state the
repository requirement, detected runner or CLI value, and required action.
Newer runners remain compatible with older repositories only when they
explicitly advertise support for every declared contract. Compatibility is
therefore auditable and never inferred from individual implementation details.

## Capability-aware specialist reviewers

Engineering Platform 1.1 has four deterministic, read-only reviewer types:
Repository Governance, Validation, Documentation and Finalization. Before the
primary agent begins work, the runner classifies the objective, lifecycle state
and safe Engineering Memory and selects only relevant reviewers. Independent
reviewers may run in parallel; their recommendations are deduplicated and
advisory.

Reviewers may inspect, analyse and recommend only. They cannot edit, commit,
push, merge, create pull requests, finalize or alter lifecycle state. Reviewer
failure is recorded as advisory and the primary engineering agent continues
from repository evidence. Reports show selection reasons, contributions and
reconciled recommendation counts; Engineering Memory retains bounded reviewer
confidence, usage, outcome and duration metadata for future selection.

### Provider timeout policy

Every Codex provider action has a fixed, host-owned deadline. It is a safety
boundary, not an operator preference: the Dashboard presents it read-only in
**Configuration → Provider timeout policy** and no API or dashboard control can
raise, disable or override it. The policy is versioned in
`execution_timeout_policy.py` so the Console and lifecycle worker share the
same contract.

| Workflow action | Maximum provider time |
| --- | ---: |
| Specialist review | 5 minutes |
| Implementation | 15 minutes |
| Local repository validation | 15 minutes |
| Autonomous quality control | 10 minutes |
| Repair | 15 minutes |
| Finalization | 15 minutes |
| End reconciliation | 10 minutes |

When the deadline expires, the host terminates the complete provider process
group rather than only the CLI parent. This prevents a descendant that still
owns stdout from holding the worker and the project's FIFO queue indefinitely.
The host then records a durable `provider_invocation_timeout` failure. Repair
and finalization retain their existing bounded recovery paths; no automatic
unbounded retry is created. Operators should inspect the retained run evidence
and submit a normal replacement only after the underlying provider issue is
understood.

Engineering Platform 1.2 complements those generic reviewers with deterministic
product specialists for Apple Platform, Windows Platform, Home Assistant
Integration, ESPHome Firmware, Pi Renderer, Universal Receiver, Website and
API. Path and objective evidence select only the relevant specialist; each
reviewer receives an explicit capability scope and may not redesign another
product area without cross-capability repository evidence. Product and generic
reviewers can inspect independently in parallel, while recommendations remain
advisory and require primary-agent reconciliation.

## Engineering Platform Qualification

Run `./src/engineering_platform/engineering-execution-host qualify` to execute every deterministic
scenario in `src/engineering_platform/ENGINEERING_QUALIFICATION.md`. The local
qualification dashboard reports pass/fail, scenario coverage, failure and
blocked counts. Its JSON and Markdown evidence remains under the git-ignored
`.engineering/qualification/` directory. Terminal Engineering Reports include the
latest available qualification version, result, execution time and coverage.

## Generation 1 status

Engineering Platform Generation 1 is feature complete. Its stable capability
set and future evidence-driven governance are recorded in
`src/engineering_platform/ENGINEERING_PLATFORM_STATUS.md`. New capabilities require
demonstrated insufficient coverage, explicit architectural approval,
implementation, qualification and evidence; routine work remains limited to
maintenance, bug fixes, compatibility and qualification improvement.

## iCloud Engineering Inbox

The repository-owned local watcher accepts iPhone-submitted `.txt`, `.md` and
filename-neutral files whose bounded UTF-8 content is Markdown. It validates a
stable input file, selects the oldest eligible file by File Date Modified,
serializes jobs and invokes only the repository-owned runner. Its v1 protocol
is `src/engineering_platform/ENGINEERING_INBOX_PROTOCOL.md`; iCloud is transport
only.
After a prompt is claimed, its lifecycle archive, reports and status are stored
only in `.engineering/`. The iCloud workspace contains only `Inbox/`.
The default queue is strict and fail-closed: after a `BLOCKED` or `FAILED`
Inbox run, later prompts remain in Inbox as `WAITING_FOR_PREDECESSOR`.
Repair and explicitly resubmit the blocking prompt with
`Retry-Of: <blocking-run-id>` as its own line; only that corrected retry can
release the sequence.

Before the explicit per-user install, verify readiness:

```sh
python3 -m engineering_platform.inbox_watcher doctor
```

Install the watcher only for the current local user:

```sh
python3 -m engineering_platform.inbox_watcher install
```

The installed LaunchAgent starts at login and remains limited to the configured
local repository. `once`, `run`, `status`, `uninstall` and `doctor` remain the
supported commands. Tests never install the LaunchAgent.

### Component logging

The watcher and dashboard write private, structured application events to
`engineering_component_logs` in `.engineering/engineering.db`. Each record has
a UTC timestamp, severity, component and, where applicable, run ID. Event and
diagnostic text are redacted and bounded before persistence.

Both owned long-lived components publish lifecycle `INFO` events: startup,
received shutdown signal, and completed orderly shutdown. The dashboard also
records an explicit restart request before it kickstarts one of the three fixed
owned LaunchAgents. Lifecycle records carry the component version, short build
commit, fixed LaunchAgent label and plist location. They never carry prompt
content, credentials, browser input, arbitrary commands or executable paths.
These records are audit diagnostics only: a lifecycle record neither changes a
run checkpoint nor authorizes runner, Inbox, repository or release work.

In Engineering Status, open **Logs** to inspect a bounded, live view of these
redacted records. Each table keeps its own sort order and shows 50 matching
records per page. The dashboard sends the selected search term, level,
event, time window and sort order to the local API; SQLite applies all of
those filters before it counts and paginates the retained records. A record
from an earlier retained day can therefore be found even when more than 100
newer records exist. The **Specific day** control is shown only for that
time-window choice, and **From**/**To** only for a custom range. Each entered
date can be cleared independently; an entered **To** cannot precede **From**.
They are never loaded or streamed outside the private dashboard.

### Dashboard configuration

The final **Configuration** disclosure in the private dashboard makes the
effective local Inbox location, Dependabot admission scan and fixed monitoring
intervals observable. It also presents the local machine diagnostics — free
disk space, Engineering Platform database path, database size and schema
version — rather than presenting them
as project-specific **Workspace** metadata. The database remains in its current
workspace-owned location for the 2.0 installation; this is a presentation
boundary that already matches the planned central-installation migration.
The language picker and automatic-refresh toggle remain direct controls in the
title bar and are therefore not duplicated here. The current entries cover Inbox scanning,
operator-merge verification, required GitHub checks, open-pull-request status,
dashboard status streaming, platform health, open component details, execution
lease heartbeat/timeout, bounded GitHub-evidence retry backoff and the
read-only provider timeout policy.

Every entry includes a keyboard-accessible information glyph with localized
explanation in English, Dutch, German, French and Spanish. Workflow safety
limits remain read-only. Two bounded local preferences are directly editable
and save immediately: component-log retention (30, 60, 90, 120, 180 or 360
days) and dashboard log level (`INFO` or `DEBUG`). Reducing retention first
requires a confirmation because it prunes only expired local component-log
rows. Each change is persisted only in local Engineering storage and is added
to the append-only audit log. Audit logging itself is always enabled. The
Inbox location has a separate confirmation flow: it accepts only an
existing absolute Engineering root that already contains a writable `Inbox`
folder, refuses a change while an execution is active, writes the local
host-owned override and restarts the Inbox watcher. The current Inbox must be
empty before a route change is admitted. The dashboard reports the
change as successful only after a fresh watcher process records the resolved
new Inbox path; a restart or route-verification failure restores the previous
configuration and restarts that previous route. Browser file pickers do not
receive arbitrary filesystem access; the modal therefore accepts the local
folder path and validates it server-side before it is applied.

The existing `inbox.out.log`, `inbox.err.log`, `dashboard.out.log` and
`dashboard.err.log` remain the LaunchAgent process streams. They complement,
rather than replace, the application logs. Rotating `.engineering/logs/*.log`
files are created only as a private fallback if SQLite is unavailable during
early startup or a crash.

### Prompt history

The dashboard also maintains **Promptgeschiedenis** from the private SQLite
table `prompt_execution_history`. It lists terminal Inbox runs with their
status, title, execution time, available commit and a report download when a
local Engineering Report exists. This projection is convenience metadata; the
terminal checkpoint and target repository remain authoritative.

Selecting a history row is the sole dashboard route to an execution's
operational details. It opens the selected Run ID in a read-only detail dialog;
the engineering report and AI analysis remain separate evidence actions on the
same row. There is no separate **Laatst uitgevoerde prompt** card, so a terminal
execution is never represented twice in the dashboard.

The dashboard's saved log-level preference is authoritative for the dashboard
and Inbox watcher, including after their LaunchAgents are regenerated. The
LaunchAgent environment is retained only as a bootstrap fallback when the local
preference store is unavailable. The default is `INFO`; invalid fallback values
fail closed to `INFO`.

When a component does not start or terminate cleanly, inspect **Logs** in the
private dashboard first. If that is unavailable, inspect the owned LaunchAgent
`*.out.log` and `*.err.log` streams, then run the appropriate `doctor` command.
Do not manually edit the SQLite database or invoke `launchctl` with an arbitrary
label; use the repository-owned install, doctor and explicitly confirmed
dashboard restart paths.

## Remote Engineering Experience

Engineering Platform 2.0 projects canonical watcher status as bounded, atomic
`status.json` and an iPhone-readable private dashboard. The dashboard is
status- and evidence-first. Its only local operational control is an explicit,
confirmed restart of one of its own per-user LaunchAgents (dashboard,
Inbox-watcher or dashboard relay). It binds only to loopback and, when
Tailscale reports one, the workstation's explicit Tailscale IPv4 address; it
never binds a wildcard, LAN or public address. It uses server-sent events for
status changes and has no engineering execution, repository, release,
deployment or publication authority.

In **Platformonderdelen**, the information glyph opens bounded component
details: current host, executable/LaunchAgent settings, build commit and
observed process memory. Restart is shown only for the three owned
LaunchAgents and calls `launchctl kickstart -k` for that fixed label after a
browser confirmation. Status storage and private remote access remain
diagnostic-only. No arbitrary executable, label or command is accepted from
the browser.

Before the explicit per-user dashboard install, verify readiness:

```sh
./src/engineering_platform/dj-engineering-dashboard doctor
```

Then install it for the current local user:

```sh
./src/engineering_platform/dj-engineering-dashboard install
```

The dashboard LaunchAgent starts from the neutral filesystem root and receives
the selected repository only through its explicit module path and `--repo`
argument. It also uses Python safe-path mode (`-P`), so Python does not derive
imports from the LaunchAgent working directory. This avoids relying on an
interactive shell or a protected working directory while preserving the
repository-owned execution boundary.

Tailscale may provide private reachability, but this repository never enables
Funnel, public binding, ACL changes, port forwarding or remote command
execution. `docs/engineering/runs/latest.md` and `index.json` are the durable,
sanitized discovery records for successfully finalized transactions; local
reports and prompt contents remain local.

The dashboard's **Codex gesprek** is a separate, private advice surface. It is
available only through the same loopback/Tailnet listener and uses the repository
identity, last executed prompt and matching Engineering Report as bounded context.
Each reply uses an ephemeral `codex exec` process in a separate read-only
workspace. It has no Inbox, runner, repository write, pull-request, merge,
release, deployment or publication authority. A requested change must still be
submitted as a new explicit Engineering prompt. The displayed chat model is
explicitly selected as `gpt-5.6-terra` by default; a local
`DJCONNECT_ENGINEERING_CHAT_MODEL` override may select another valid Codex
model before the dashboard is started.

## Prerequisite and usage

Codex CLI must already be installed and authenticated in the developer's local
environment. From a clean DJConnect checkout, run:

```sh
./src/engineering_platform/engineering-execution-host path/to/engineering-prompt.md
```

For a bounded transaction with explicit owner authorization for the complete
PR and Finalization lifecycle, use:

```sh
./src/engineering_platform/engineering-execution-host path/to/engineering-prompt.md \
  --owner-authorized --run-id bounded-run
```

The authorization is checkpointed locally, applies only to that transaction,
and permits branch/PR readiness, bounded repair, merge and Finalization. It
does not permit releases, deployments, tags, packages, infrastructure changes,
repository-settings changes or branch-protection bypass.

The runner verifies the repository, builds a repository-first Codex prompt from
the supplied file and canonical repository instructions, then records an
advisory checkpoint in `.engineering/engineering-runs/`. That directory is local
and Git-ignored. It stores identity and execution evidence only; it never
stores prompt content, credentials, tokens or agent output.

The runner retains at most the ten newest completed checkpoints for local
diagnosis and removes older completed checkpoints only from that owned local
directory. Blocked and malformed checkpoints are preserved for inspection.

To continue an interrupted non-terminal run, restart the foreground command:

```sh
./src/engineering_platform/engineering-execution-host path/to/engineering-prompt.md --run-id <run-id> --resume
```

There is no background continuation. A resume synchronizes and re-inspects
repository and GitHub evidence; that evidence overrides checkpoint phase and
next-action fields. Malformed, incompatible or conflicting state fails closed.
An abandoned checkpoint can be removed only after inspecting it locally, with
`rm .engineering/engineering-runs/<run-id>.json`.

## Diagnostics

Codex may return an optional short `diagnostic` field with a `BLOCKED` or
`FAILED` structured result. The runner stores only a bounded, redacted,
human-readable reason in the local checkpoint and prints the reason and the
next action. Diagnostics are advisory: resume always recomputes phase from
repository and GitHub evidence.

If Codex CLI itself exits unexpectedly, the current console additionally shows
its exit code plus bounded, redacted stderr and stdout. Those command-output
details are never checkpointed; the checkpoint contains only a safe summary.

## Autonomous lifecycle and Finalization

With `--owner-authorized`, the runner treats implementation and its mandatory
governance-only Finalization as one resumable transaction. It checkpoints the
implementation and Finalization branch, PR, observed head, merge commit, safe
repository/GitHub evidence and repair count. Repository and GitHub evidence
always override those advisory fields on resume.

After objective evidence proves the implementation merge is in main, the
runner synchronizes local main and derives Finalization from the merged change
and current repository governance. Finalization may reconcile rolling status
records, management/repository summaries, prompt navigation/history and
lifecycle evidence. It cannot add capabilities, change runtime behavior,
select new roadmap work, release, deploy or publish.

The runner marks both PRs ready for review, polls until checks are terminal,
and merges only green PRs under the recorded authorization. A failed required
check starts a bounded repair cycle on the same PR; its check name and repair
count are safe diagnostic evidence. Missing permission, unsatisfied review,
out-of-scope merge conflict or another external dependency remains blocked
with a bounded reason and resume guidance. Waiting, queued CI and transient
API failures remain non-terminal.

On full authorized completion the console emits one management summary with
the implementation/Finalization PRs and merge commits, repair count, authority
boundary and confirmation that no release, deployment or publication occurred.
It does not expose prompt content.

## Repository cleanup

After merged Finalization evidence is contained in `main`, the runner enters
`REPOSITORY_CLEANUP` before it can report `COMPLETE`. It fetches with
`git fetch --prune`, checks out and fast-forwards `main`, and evaluates only
the implementation and Finalization branches recorded for that transaction.
It first uses ordinary deletion. If Git refuses solely because a squash merge
made the transaction branch non-ancestral, reconciled PR/main evidence and
checkpoint ownership authorize a safe local force deletion for that exact
branch. Missing branches are already-cleaned success; uncertain ownership or
failed reconciliation remains blocked. Resume repeats the same idempotent
evidence-based cleanup and never removes unrelated branches.

## Terminal reports and advisory sub-agents

Each terminal transaction writes an immutable local Markdown report beneath
`.engineering/reports/`. Reports are never opened automatically in an editor;
they remain available through Engineering Status and the local report path.
Reports are git-ignored. When the Inbox watcher owns the
transaction, it validates the report against the terminal checkpoint and keeps
the safe terminal report locally under `.engineering/reports/`. If correction is
needed, the watcher writes a corrected checkpoint-consistent local copy; it
never publishes a report to iCloud. Reports summarize checkpoint evidence,
PRs, repair and cleanup evidence, diagnostics and the management summary.
Optional sub-agents
are read-only, bounded advisory helpers for inspection or validation; they
cannot write, create/ready/merge PRs, create Finalization, alter governance or
perform cleanup. The primary runner validates and integrates every result.

Every report records the Engineering Platform Version, Runner Version,
Bootstrap Contract, Checkpoint Format, Memory Format and Report Format
alongside the transaction evidence. It also records runtime provenance for the
specific invocation: Runtime Provider, AI Model, Reasoning Profile,
Configuration Profile and detected Codex CLI Version. The runner writes `not
reported` rather than inventing provider metadata that the CLI did not emit.

## Terminal evidence and boundaries

Queued or running CI, pending checks, a polling interval, a temporary GitHub
failure and a Codex process exit are never successful completion. The runner
records waiting work and exits non-zero so the developer can resume it. It
returns success only after its structured agent result agrees with terminal Git
and GitHub evidence: either an authorized merged/reconciled result on `main`,
or an explicitly bounded open-PR objective with all required checks terminal.

The runner uses repository-scoped `workspace-write` Codex access. It does not
reset, stash, overwrite or discard unrelated work. A dirty workspace,
repository mismatch, missing Codex CLI, failed required checks, missing
approval or other external authority boundary is reported rather than bypassed.
Without `--owner-authorized` it does not merge. In every mode it does not
release or deploy.

This foreground process has no background continuation. It can be resumed from
repository evidence after an interruption.

## Live progress

The runner emits concise terminal and cleanup phase updates and atomically
maintains `.engineering/status/current.json`. The Inbox watcher projects bounded
dashboard status to `.engineering/status/status.json`. Both are git-ignored local
advisory records; resume recomputes from repository and GitHub evidence. iCloud
carries only a submitted Inbox file and never receives status, reports or prompt
archives. Run
`./src/engineering_platform/engineering-execution-host status` to display the current phase, PRs,
repair count and action.

During Codex execution, the live status also includes aggregate worktree
progress: changed, new and deleted file counts. It never includes filenames,
paths, command output or prompt content, and is advisory only rather than
repository evidence.

## Engineering Memory

Successful transactions store bounded metadata under `.engineering/memory/`,
which is already covered by the local `.engineering/` ignore rule. Memory never
stores prompts, source snapshots, credentials or personal data. Retrieved
patterns are advisory context only: repository and GitHub evidence override
them, and they cannot change scope, validation or authority.
