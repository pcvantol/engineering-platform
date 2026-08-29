# Engineering Report Evidence Contract

Engineering Reports are local, derived evidence for a terminal Engineering
Platform transaction. They do not alter lifecycle, reviewer selection,
qualification or repository authority.

## Engineering Evidence 2.0

Engineering Evidence 2.0 makes each Engineering Report a self-contained audit
artefact while keeping **Repository Truth** authoritative. The evidence flow is:

```text
Repository Truth → Execution Evidence → Engineering Evidence → Engineering Report
```

The report explains the underlying repository and checkpoint evidence; it never
becomes an independent source of truth. Before publication, the generator
performs report consistency validation. A report with a missing required
Evidence 2.0 section, absent explicit deliverable answer, missing repository
commit, missing complete-run Evidence Bundle, or the former unqualified
`Implemented Components: none recorded` output is rejected.

Every terminal report includes these derived sections:

- **Component Inventory** — architectural components inferred automatically
  from changed implementation files, with their source files and added,
  modified or removed classifications.
- **Deliverable Projection** — every extracted requested deliverable has an
  explicit requested, delivered or undelivered outcome, with runtime,
  documentation and validation artefacts separately identified.
- **Qualification Projection** — execution, qualification, runtime,
  validation and governance status are separate facts. `COMPLETE` is never
  rendered as a synonym for a qualification pass.
- **Runtime Projection** — report-bound runtime instance and identity,
  Producer mission reference, dispatcher and queue availability, plus only
  persisted receipt and decision-evidence references.
- **Execution Receipt Projection** and **Decision Evidence Projection** —
  immutable run and Producer provenance references only; the report never
  reproduces receipt or Decision Evidence content.
- **Statistics Projection** — Mission, execution, Engineering Action and
  runtime counts are separately scoped. An Engineering execution never implies
  Mission completion.
- **Deliverable Answer** — an explicit `YES`/`NO`, `PASS`/`FAIL` and `GO`/
  `NO-GO` answer when the prompt requests one, derived from the persisted
  terminal checkpoint.
- **Commit Strategy** — Genesis Local Commit, Managed Pull Request, Managed
  Merge, Finalization or managed execution evidence, including the applicable
  commit and PR evidence.
- **Branch Traceability** — preflight branch, execution branch, final branch,
  final commit and the recorded transition.
- **Requirement Traceability** — prompt requirement → implemented component →
  repository files → regression tests → validation evidence.
- **Validation Traceability** — each recorded validation and documentation
  validation with its purpose, result and repository evidence.
- **Execution Phase Timing** — Total Wall Time, active processing, provider,
  validation, queue/external wait, overhead, report-generation and
  evidence-persistence measures. `Top Phase Categories` is aggregate-only;
  `Longest Individual Spans` is separately ranked. The legacy `Execution
  Duration` label, where retained for compatibility, explicitly means Provider
  Execution Time and never Total Wall Time.
- **Validation Control Results** — one explicit PASS, FAIL, NOT_EXECUTED,
  NOT_APPLICABLE or UNAVAILABLE result for each required control, with its
  LOCAL, GITHUB_CI or EXTERNAL source. Qualification remains separate from
  individual controls; `git diff --check` remains independent of transaction
  baseline availability.
  The canonical dashboard control derives inclusion and terminal result only
  from its persisted command invocation/terminal lineage. Its run-scoped
  artifact records the fixed four-shard, one-worker topology and every shard
  result when available; cleanup evidence remains separate from that result.
- **Execution Statistics** — execution count, evidence-backed engineering
  actions, Forge mission count, repair iterations, provider execution time and
  validation time status.
- **Repair History** — append-only, run-scoped evidence for each bounded
  required-check repair: failed checks, proposed action, bounded AI repair
  summary, resulting commit when reported, timestamp and outcome. It is audit
  evidence, not a raw AI chat transcript, and never replaces GitHub or
  repository truth.
- **Engineering Evidence Summary** — compact JSON for read-only consumers.

Component and traceability records are generated from repository files,
terminal checkpoint data and bounded recorded validation evidence. They require
no manual report authoring. When an item was not recorded, the report says so
explicitly instead of inferring it.

## Report interpretation

Each report separates three viewpoints:

1. **Initial Repository Assessment** describes the repository before any
   attempted implementation.
2. **Reviewer Findings** preserves read-only reviewer observations as initial,
   advisory input. These findings are never a statement of final repository
   state.
3. **Engineering Outcome** and **Management Summary** describe the terminal
   repository outcome.

When a transaction reaches `COMPLETE`, a reviewer finding remains advisory.
If implementation addressed it, the report states **Resolved by** and points
to implementation, changed-component and repository evidence in the Evidence
Bundle. It never treats a reviewer observation as final truth by itself.

## Multi-repository reporting

Every terminal report has an **Execution Target Identity**. It distinguishes
the Engineering Platform **Execution Host** from the engineering **Target
Repository** and records the Execution Host Repository, Execution Mode, Target
Workspace, Target Repository, Target Branch, Target Commit, Execution Host
Version, Runner Version, Bootstrap Contract and Checkpoint Format.

Managed execution normally uses the host repository as its target. Genesis
execution normally uses a separate local target repository. The host is never
described as the target merely because it generated the report.

## Producer Contract projection

Forge owns the canonical Producer Contract and all Producer semantics.
Engineering Platform consumes only its declared, immutable audit metadata:
Producer ID, Type, Version, Correlation ID, optional Mission ID and Engineering
Action ID, plus Execution Constraint Version. Reports expose those values in a
**Producer** section without exposing Forge implementation details. When a
legacy prompt has no Producer metadata, the report records `HUMAN` and
`legacy`. Producer metadata never changes execution behaviour.

The report is execution evidence, not Forge Decision Evidence. It does not
interpret, recreate or recommend Missions, Runtime Prompts, Runtime Instances
or planning decisions.

## Producer Submission and Execution Context contracts

Reports keep four independent facts: **Producer Submission Contract**,
**Execution Context Contract**, **Execution Status** and **Execution Context
Status**. The first two identify the persisted envelope/submission and optional
immutable snapshot; the latter two describe only the Engineering Platform run.
An absent Execution Context is reported as `Not supplied by Producer` and does
not change execution status. Reports never parse prompt text, inspect Forge
Runtime or derive Mission semantics to fill these fields.

## Forge Mission Recommendation Handoff

When a Forge Producer explicitly supplies a structured recommendation handoff,
Engineering Platform projects it as an immutable, read-only advisory
deliverable. The accepted sources are structured Producer metadata or a
declared repository-relative Forge artefact; prompt wording, branches, commits
and summaries are never used to infer a recommendation.

The terminal report adds **Forge Mission Recommendation Handoff** and the
Deliverable Projection identifies the requested recommendation, supplied
artefact, recommended Mission and Decision Evidence reference. It keeps
Execution Status, Recommendation Status, Business Decision and Mission Created
as separate facts. `COMPLETE` execution never means approved, allocated or
executable. Missing title or Decision Evidence is reported as
`Recommendation Projection: INCOMPLETE`; values are never fabricated.

The projection supports the Forge-supplied statuses `PROPOSED`, `RECOMMENDED`,
`NOT_RECOMMENDED`, `SUPERSEDED` and `UNAVAILABLE`. Alternative candidates keep
the supplied order, rank and ordering reason. A reported supersession remains
on the historical run; it does not overwrite an earlier handoff. Forge remains
the sole owner of ranking, Decision Evidence, the Business Workspace, approval
and Mission lifecycle.

## Execution Host and Workspace Preflight evidence

Before an Inbox item is claimed, Execution Host Preflight Level 1 records local
host evidence: Execution Host identity, version, Bootstrap Contract, timestamp,
duration, outcome and compact per-check results. Level 2 then records Workspace
Preflight evidence: workspace, requested and canonical target repository,
matched authorization identity and policy, branch, execution mode, timestamp,
duration, outcome and compact per-check results. Both stages fail
closed before an Inbox claim or active transaction. The Workspace stage checks
only target resolution and repository readiness; it does not validate missions,
actions or capabilities. Every terminal Engineering Report includes both
evidence sections without exposing internal implementation details.

## Evidence Bundle

Every `COMPLETE` report includes a structured **Evidence Bundle**. It exposes:

- repository evidence: target repository and commit, worktree state, and
  changed, added, modified and removed files;
- validation evidence: recorded tests/results when available, qualification,
  checkpoint-schema and example-validation status, plus `git diff --check`;
- implementation evidence: changed components, models, documentation, tests,
  contracts and schemas; and
- reconciliation evidence when the objective requests reconciliation:
  initial and final classification, required assessment coverage, changes and
  remaining limitations.

New runs persist up to twelve redacted `{command, result}` validation summaries
from the terminal agent result. Older runs and runs that provide none remain
explicitly marked `not recorded by the runner`. The runner still does not
persist an initial assessment taxonomy.

## Documentation validation

The Validation Traceability section always records documentation validation as
a distinct validation item. Its scope is the canonical Engineering Report
Evidence Contract and the rendered report sections; its result is evidence that
the report is generated from that contract. This does not replace repository
documentation tests when those are recorded by the runner.

## Repository truth

The report resolves evidence in this order:

1. Execution Host, Target Repository and Target Commit;
2. persisted repository state and terminal checkpoint;
3. Repository Evidence and Evidence Bundle;
4. resulting commits and validation evidence; and
5. advisory reviewer observations.

Reviewer findings cannot override repository evidence. `BLOCKED` and `FAILED`
reports never claim successful implementation or delivery.

## Advisory Codex analysis

After a terminal report is written, the runner may request one separate Codex
CLI analysis of that exact local report. The analysis is read-only, bounded and
stored locally per run under `.engineering/report-analysis/<run-id>.md`. It
distils findings, issues, risks, next steps and advice for the Product
Architect. Its output is advisory and redacted before persistence. Every
analysis also records a bounded **Analyseverwerking** status. If the provider
is unavailable, fails, or returns an invalid structured response, the matching
safe reason is shown there; raw provider output and diagnostics are never
persisted. This makes an empty advisory analysis distinguishable from a
successful analysis with no findings. Large reports are passed to the managed
Codex CLI over standard input rather than as a command-line argument. If a
report exceeds the advisory input budget, its beginning and terminal end are
provided with an explicit omission marker; the full report remains immutable
and authoritative.

The private Engineering Status dashboard exposes an **AI analysis** column in
Prompt History next to the engineering report. View and download actions are
available only when the analysis file belongs to that exact Run ID; analyses
from another execution are never selected as a fallback.

For a controlled temporary processing failure (`provider_failed`,
`provider_unavailable` or `invalid_structured_response`), the analysis dialog
also offers **Generate analysis again**. It is bound to that same indexed
terminal report and regenerates only the advisory analysis; it never resumes,
retries or changes the Engineering execution, checkpoint, branch or pull
request. A successfully processed analysis is deliberately not retryable.

If a terminal report temporarily cannot be read in its dialog, the dashboard
offers **Reload report**. That action repeats only the read-only retrieval of
the indexed immutable report. It never regenerates a report from current state
or changes any run evidence.

The dashboard exposes that analysis only from the matching Promptgeschiedenis
row and only when its Run ID matches the selected terminal execution. A failed
or absent analysis never changes the terminal checkpoint, report, repository
state, validation result or lifecycle outcome.

Promptgeschiedenis also opens a near-fullscreen operational-detail dialog for
the selected Run ID. It contains the bounded execution status, timing, runtime
provenance, provider usage, commits, Evidence Bundle summary and reviewer
results. The complete report and advisory analysis remain separate evidence
actions, so their Markdown is never duplicated in that detail dialog.

## Private dashboard evidence access

The dashboard renders a report and an advisory analysis as read-only Markdown
only after the maintainer opens the relevant evidence view. It provides local
copy and download actions only when the matching artifact exists. Downloaded
files contain the original local Markdown; rendering and copying do not alter
the report, checkpoint or target repository.

**Promptgeschiedenis** is a private SQLite-backed index of terminal runs.
Selecting a row opens the run's near-fullscreen operational-detail dialog;
that dialog is bound to the selected Run ID and contains no report or analysis
body. Its separate report and AI-analysis actions open the matching read-only
Markdown dialog, not in an editor. It is deliberately an evidence-navigation
feature rather than an execution or repository-control surface.

The dashboard detail projection is deliberately separate from its storage
lookup. It reads the selected immutable history row and its bounded companion
data first, then a small projector creates the response shape. That projector
may derive only the displayed Evidence Bundle summary, target-repository
provenance and report-bound Forge recommendation handoff from the report for
the same Run ID. It never writes SQLite,
rewrites the report, or substitutes information from another run. An absent or
non-readable report produces an empty evidence summary while retaining the
stored history data.

When no report or analysis was persisted for the selected terminal run, the
dashboard must say so explicitly. It must not show an unavailable artifact as
pending, or expose copy/download controls for empty content.

## Runtime provenance

Every terminal report records its runtime provenance alongside the terminal
evidence:

- **Runtime Provider**;
- **AI Model**, as actually reported by the provider;
- **Reasoning Profile**, when reported;
- **Configuration Profile**, when reported; and
- **Codex CLI Version**, when detected; and
- **Codex CLI Installation Path**, captured at invocation time only when the
  runtime is the Engineering Platform-managed Codex CLI.

A value is explicitly shown as `not reported` when the provider does not emit
it. The runner never infers or fabricates model, reasoning or configuration
metadata. These fields describe the process that produced this specific report;
they are not a claim about a currently configured provider or a later run.

The matching **Promptgeschiedenis** detail dialog reads provenance only from
the selected terminal report. It therefore cannot display a model, profile or
CLI installation path from an unrelated current run.

## Retry executions

A retry report contains a **Retry Relationship** section with Retry Of,
Original Run, Retry Generation, Retry Timestamp, Current Run, Terminal State
and Repository Context. It documents that new execution only; original reports,
checkpoints, telemetry and evidence remain immutable.

## Provider usage and context efficiency

The local datastore appends one immutable provider-invocation row for every
observed primary, repair or reviewer invocation. It retains only timing,
provider/model provenance, role, token counters, the observed speed state and
bounded churn counters; prompts, replies, command arguments, file paths and
tool output are never stored for this purpose.

`AUTHORITATIVE` means the provider/runtime reported a counter directly.
`DERIVED` means it was calculated from direct counters (for example, uncached
input is input minus cached input). `UNAVAILABLE` means the runtime did not
report it and is never converted to zero. Legacy reports remain unchanged;
their invocation detail is `UNAVAILABLE`.

The versioned `2026-08-18` rate table supplies only an estimated
purchased-credit equivalent for GPT-5.6 Sol, Terra and Luna. EUR is derived at
EUR 0.04 per credit. These values are observability estimates, not account
billing, included allowance, remaining quota or account-balance authority.

The report and private run detail distinguish cumulative input from the maximum
input in any one invocation. A runtime may prove `FAST`, `NORMAL_DEFAULT` or
`OTHER` only from its actual CLI/runtime metadata; otherwise it reports
`UNKNOWN`. Context counters are deterministic observations where possible and
remain bounded; they do not preserve raw traces.
