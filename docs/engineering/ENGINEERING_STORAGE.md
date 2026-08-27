# Engineering Platform Storage Contract

## Purpose

Engineering Platform persistent evidence is stored in one private,
git-ignored workspace **per Git repository**, shared by every local worktree.
Every worktree exposes that workspace at its familiar `.engineering/` path;
the physical store lives beneath the repository's Git common directory. This
prevents dashboard history, terminal evidence and active status from splitting
when the Execution Host uses a dedicated runtime worktree or when a developer
switches branches.

Its only database path is:

```text
.engineering/engineering.db
```

iCloud Drive remains transport only. It is not an Engineering evidence store.

### Worktree-safe storage migration

On first provisioning after this contract, Engineering Platform discovers all
accessible worktrees belonging to the same Git common directory. It selects
the store with the largest immutable prompt-history index as the initial
shared store, safely merges independent SQLite evidence and reports, and then
replaces each worktree-local `.engineering/` directory with a private link to
the shared store. SQLite row identifiers are regenerated only for local log
and lifecycle-event rows; run IDs, receipts, submissions, prompt history and
artifacts retain their durable identities. Mutable status projections use the
most recent timestamp.

The migration is idempotent and fail-closed for incompatible schemas, foreign
key violations, unexpected links and conflicting immutable files. No history
is silently discarded. Process locks are intentionally not migrated: a live
lock blocks migration until the normal component restart has stopped its
owner, while a verified stale lock is discarded and recreated by the component
in the shared store. It has no cloud, release, deployment or publication
effect.

## Versioned schema

The storage contract is independently versioned as **Engineering Storage
schema `32`**. The required version is declared as `storage_schema` in
`tools/engineering/ENGINEERING_PLATFORM_VERSION.json` and is validated by the
runner compatibility contract.

The database records every applied change in
`engineering_schema_migrations`. Opening it is fail-closed when:

- a database contains unrecognized tables without an Engineering schema
  history;
- the recorded schema version is newer than the installed Engineering
  Platform supports; or
- a required migration is unavailable or cannot complete safely.

Schema upgrades use a controlled SQLite transaction and rollback-journal mode.
The latter avoids persistent WAL sidecar files in `.engineering/`. Opening an
existing shared store never upgrades it implicitly. A post-merge activation
must first prove there is no active Execution Host lease and that the Inbox
watcher and dashboard locks are not held; a new empty private store may be
initialized normally.

### Execution admission guard

An Inbox-admitted execution inherits the storage schema understood by the
watcher that admitted it. If that execution changes Engineering source files
to introduce a newer schema, its child processes may validate the new schema
against temporary workspaces, but they cannot migrate the canonical
`.engineering/engineering.db` yet. The migration is deferred until the
updated Engineering Platform has been merged and its components are restarted.
This prevents a running prompt from upgrading the live datastore beyond the
code that is currently publishing dashboard and watcher state.

### Controlled post-merge activation

Storage code delivery and shared-runtime activation are separate operations:

1. a Managed execution may create and test a migration against an isolated
   database, then delivers it through PR and CI;
2. after operator merge, verify every persistent component supports the new
   schema and that `main` is clean and synchronized;
3. stop the watcher, dashboard relay and dashboard in that order;
4. invoke `python -m tools.engineering.storage activate --repo <repository>` exactly once;
5. restart watcher, relay and dashboard on the merged revision, then run host
   verification.

The activation command refuses active execution leases and held watcher or
dashboard locks. Normal component startup refuses an existing lower-version
store rather than migrating it. Thus a managed prompt cannot bypass this
boundary merely by importing newer source without its admission environment.

## Execution Host telemetry

Schema `2` adds the generic, local-only Execution Host telemetry model. Schema
`3` adds total elapsed time to that model. Schema `4` makes SQLite the
canonical component-log store and imports the previous redacted JSONL logs on
first upgrade:

- `execution_runs` stores one operational record per terminal run, including
  Inbox arrival, runner start, completion, measured Codex CLI duration and
  total elapsed duration from Inbox arrival through terminal status publication,
  explicitly reported token values, terminal state, execution mode, workspace,
  repository and Execution Host version;
- `daily_execution_statistics` stores daily aggregates for prompt counts,
  average Codex CLI, total elapsed and queue waiting times, explicitly reported token totals,
  and COMPLETE/BLOCKED/FAILED distribution.

Telemetry is a rebuildable operational projection and is never lifecycle
authority. Schema `30` writes one immutable, run-keyed terminal telemetry
intent to `terminal_telemetry_outbox` before materializing `execution_runs`.
The watcher drains pending intents at startup and before new Inbox work. A
process loss can therefore delay telemetry but cannot silently lose a terminal
run or double-count a daily aggregate: materialization is idempotent by Run ID
and each aggregate is recomputed from unique execution rows. Failures remain
`FAILED_RETRYABLE`; no terminal checkpoint, report, Prompt History entry or
repository evidence is modified by telemetry recovery.

When older terminal evidence has no telemetry intent, the bounded recovery
path accepts only matching structured checkpoint, Prompt History and terminal
timing evidence. It uses the recorded terminal timestamp for the historical
day, leaves unavailable optional fields unknown, and fails closed rather than
parsing report prose or inventing duration/token data. Outbox provenance is
`LIVE_TERMINAL`, `RECOVERY` or `BACKFILL`.
An unavailable database is logged by the watcher but never changes the
authoritative engineering checkpoint or its outcome. Token values remain null
when the provider did not report them; the platform never estimates them.

## Historical pull-request evidence recovery

Schema `31` adds an append-only audit for operator-invoked recovery of a
missing Managed implementation or Finalization pull request. The recovery is
dry-run by default:

```bash
python -m tools.engineering.pr_evidence_backfill --run-id <run-id>
```

`--apply` is required to write anything. For each missing role, the tool reads
the canonical SQLite checkpoint and current GitHub data again. It links a pull
request only when all of these facts match exactly: the run is terminal and
Managed, the checkpoint has the role's branch and merge commit, GitHub reports
one PR for that branch, that PR targets `main`, is merged, and has precisely
that merge commit. It also refreshes `origin/main` without changing the local
checkout and proves that same merge commit is an ancestor. The checkpoint
update, lifecycle event and `APPLIED` audit entry commit in one SQLite
transaction; the JSON checkpoint remains a post-commit compatibility
projection.

Every non-match—including unavailable GitHub evidence, incomplete legacy
checkpoint data, an already recorded PR, a non-terminal run, or a changed
checkpoint—is skipped. In apply mode it receives an immutable `SKIPPED` audit
record with a bounded reason. The tool never creates, edits, merges or closes
a PR, and cannot infer evidence from PR titles, timestamps or numbers alone.

Schema `9` records producer-neutral provenance alongside each run and creates
an immutable `execution_receipts` record. A receipt contains Producer ID,
Producer Type, optional Mission/Engineering Action/Correlation IDs, Execution
Host identity and version, Run ID, receipt timestamp and terminal outcome.
Forge owns Producer Contract semantics; Engineering Platform owns these local
execution receipts. This metadata supports operations and analytics only and
never affects scheduling or execution. It does not become Forge Decision
Evidence, Mission planning state or Runtime planning state.

Schema `29` adds immutable `dependabot_admission_events` before a generated
Dependabot envelope can be claimed. Each record identifies only the configured
repository, pull-request number, observed head SHA and branch, submission ID
and observation time. It prevents duplicate automatic admission while leaving
the normal submission, run, repair and terminal evidence contracts unchanged.

Schema `14` adds the canonical active-run lease. A lease binds one Run ID to a
stable Execution Host identity and a unique host-instance ID; a process ID is
only optional diagnostic evidence. The Execution Host obtains the lease before
publishing operational activity, renews it at a bounded interval shorter than
its expiry, and releases it at a terminal checkpoint. Heartbeats do not create
unbounded event rows. Only acquisition, expiry, stale detection/reconciliation
and release are recorded. Startup reconciliation treats expired or pre-lease
active transactions as recoverable/operator-visible datastore facts; it never
invents terminal evidence and never automatically reruns work.

Schema `15` stores one typed readiness evaluation for each admitted Run ID:
profile identity and version, execution mode, observed bounded facts, PASS or
BLOCKED result, failed requirements and a redacted diagnostic. It is local
datastore evidence; status files and the dashboard only project it.

Schema `16` persists an immutable Producer Envelope execution-context
snapshot and its version with the submission, and links each submission to its
Run ID. Schema `17` retains declared Engineering Action provenance with that
submission. Schema `18` adds immutable operator dismissal evidence separately
from the terminal outcome, so the dashboard can distinguish a failed or
blocked execution that an operator has deliberately closed from one still
requiring action. Schema `19` persists the supplied Forge governance handoff
snapshot and version with the submission. These additions remain local
Engineering evidence; they do not grant the Execution Host ownership of Forge
planning or decision state.

Schema `20` adds `execution_phase_spans`, the canonical, immutable per-run
phase-timing evidence. Each observed span records an ID, canonical phase name
and category, optional parent, attempt and ordinal, UTC start/completion
timestamps, a duration in milliseconds, bounded metadata and outcome. The
canonical names are `QUEUE_WAIT`, `SUBMISSION_CLAIM`, `INITIALIZATION`,
`HOST_PREFLIGHT`, `WORKSPACE_PREFLIGHT`, `CAPABILITY_PREFLIGHT`,
`EXECUTION_PREPARATION`, `PROVIDER_EXECUTION`, `VALIDATION`, `REPAIR`,
`REPOSITORY_FINALIZATION`, `PR_OR_MERGE`, `FINALIZATION`,
`REPORT_GENERATION`, `EVIDENCE_PERSISTENCE`, `REPOSITORY_CLEANUP`,
`RECONCILIATION`, `EXTERNAL_CI_WAIT` and `TOTAL_EXECUTION`.

Durations are captured from a monotonic clock whenever a phase runs in one
process; persisted timestamps remain stable UTC wall-clock timestamps.
Cross-process queue timing uses an explicit persisted submission-eligibility
timestamp and closes at the observed execution claim boundary. The
`TOTAL_EXECUTION` envelope starts at that claim boundary, so queue wait and
active execution are disjoint.
Repeated phases remain individual records and nested spans retain their parent.

Schema `23` adds explicit model authority and an optional raw provider model
to provider invocations. Only structured provider runtime events may populate
those fields. Historical records retain `UNAVAILABLE` model authority and are
never backfilled from configuration, token counts or later observations.

## Execution timing read-model semantics

An **Individual Span** is one persisted concrete occurrence. It retains its
phase ID, UTC boundaries, duration, outcome, attempt, parent and bounded typed
metadata. Repeated provider attempts are therefore always independently
auditable.

A **Phase Aggregate** is the non-double-counted total for one canonical phase
name. Derived category totals suppress only a same-category ancestor, so a
nested validation wrapper and its individual checks cannot be double-counted
while a provider span inside repair remains measurable. `Top Phase Categories`
ranks each category once by duration descending and phase name ascending.
`Longest Individual Spans` is a separate ranking, ordered by duration
descending, phase name and ordinal; it may contain repeated categories.

**Total Wall Time** is the `TOTAL_EXECUTION` envelope between execution claim
and terminal reconciliation. The queue boundary ends at claim, so Queue Wait
is disjoint and is not subtracted from Total Wall Time. The total envelope is
excluded from category and individual-span rankings. `Overhead Time` is
`max(0, active EP processing - processing
coverage)`, where processing coverage includes only outermost provider or
validation spans; nested validation belongs to its provider coverage and is
not subtracted twice. Active processing equals total wall time less explicit
external wait. Report generation and evidence persistence are individual,
bounded terminal spans; the immutable terminal projection is rendered after
those spans close and is deliberately not recursively timed. Validation
commands emitted by the runtime provider are timed
at their direct JSONL command start/complete boundaries; only a bounded
validation category is persisted, never command text or output. Historical
runs retain their prior total duration from `execution_runs` but have no
fabricated phase spans and must be labelled phase telemetry incomplete. Lease
reconciliation closes an active span only at the actual reconciliation boundary
with `STALE`/`INTERRUPTED` outcome.

Phase duration and phase share are deliberately separate projections. Raw
category duration retains the complete persisted span for audit, including a
`STALE` tail observed after terminal reconciliation. A displayed share uses
only the phase interval that overlaps the `TOTAL_EXECUTION` envelope; where
the UTC boundaries are not comparable, it is conservatively capped at that
envelope. Thus an individual phase share cannot exceed 100%, without rewriting
or discarding the underlying timing evidence. Nested categories may still
overlap each other, so their shares are not a partition and must not be summed.

Execution phase timing belongs solely to Engineering Platform execution
evidence. Forge remains the authority for Mission and planning intelligence.
Reports project the compact timing summary and largest measured consumers; the
dashboard intentionally has no new timing UI in this increment.

## Component logging

`engineering_component_logs` is the canonical store for redacted watcher and
dashboard events. The dashboard reads its bounded log views from this table,
and clearing a component log removes only that component's SQLite rows.

The table also records bounded, redacted dashboard user actions that have an
operational effect or access local evidence: component restart requests,
provider reset-credit requests, submitted AI-advice questions, and report or
log downloads. A user action records its event type and the applicable fixed
component or run identifier, never its free-form chat text, report body,
credentials or browser-supplied command.

The former `.engineering/logs/inbox.log` and `dashboard.log` files are no
longer normal application logs. They are created only as a private, rotating
fallback when SQLite cannot be opened during early startup or an application
failure. Existing redacted JSONL entries are imported once during the schema
`4` migration. LaunchAgent `*.out.log` and `*.err.log` streams remain separate
process-level crash diagnostics.

## Prompt history and retry lineage

Schema `5` adds `prompt_execution_history`, a canonical local index of every
terminal Engineering Platform run. It stores the immutable run identifier,
terminal status, prompt title, execution timestamp, available Git commit and
the relative location of a delivered Engineering Report. Existing reports and
telemetry runs are backfilled safely when the dashboard first requests the
history. The index is a convenience projection only: the checkpoint, report
and target repository remain authoritative evidence.

The private dashboard exposes this as **Promptgeschiedenis**. Its searchable,
sortable and paginated table can download an indexed report only when that
report was actually delivered locally.

Schema `6` adds immutable `retry_of`, `original_run_id`, `retry_generation`
and `retry_timestamp` fields to prompt history and execution telemetry. Each
retry has its own Run ID, report and telemetry row; lineage links evidence
without merging or overwriting original runs.

Schema `7` adds a bounded duration-learning profile to terminal telemetry:
prompt character count plus the explicitly reported runtime provider, model,
reasoning profile and configuration profile. The dashboard may use this only
for an advisory duration range when at least two **COMPLETE** runs have the
same fully reported profile. The historical duration is scaled to the active
prompt size and blended conservatively with the existing size-and-phase range.
Missing or unreported runtime fields never create a cross-profile estimate.
This data remains local operational telemetry; it does not schedule work,
change an execution outcome or retain prompt contents.

Schema `8` stores the resolved local checkout path of the target repository
and its Git tracked-file count when a run reaches a terminal state. The
Promptgeschiedenis detail dialog presents that immutable workspace snapshot;
it never substitutes a later live repository count for historical evidence.

Schema `21` adds an immutable `execution_metadata` snapshot to both terminal
prompt history and execution telemetry. It contains only non-negative,
bounded aggregates: modified, created and deleted file counts and the number
of Codex commands executed. The live dashboard may show the same counters for
an active run, and the prompt-history detail dialog shows the stored terminal
snapshot. Command text, output, arguments, file paths and file names are never
stored in this field.

## Canonical workspace migration

The shared `.engineering/` workspace is the sole canonical local location for
status projections, transaction checkpoints, immutable artifacts, reports,
redacted component logs and locks. When an existing workspace contains the
historical `.djconnect/` directory, provisioning performs a local, fail-closed
migration before any component starts:

- existing evidence is moved to `.engineering/` without rewriting it;
- byte-identical duplicates are discarded only after verification;
- a conflicting historic log or qualification category is retained under
  `.engineering/legacy/` without replacing its active counterpart;
- a conflicting file, symlink or incompatible path type aborts the migration;
- the legacy directory is removed only after every child has migrated.

The migration has no cloud, release, deployment or publication effect.

## Integrity and privacy

The database is private to the local user (`0600` where supported), git-ignored
and contains only redacted Engineering Platform evidence. It has no cloud sync,
network listener, release, deployment or publication authority.
