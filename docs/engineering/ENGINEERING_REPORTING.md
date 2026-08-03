# Engineering Report Evidence Contract

Engineering Reports are local, derived evidence for a terminal Engineering
Platform transaction. They do not alter lifecycle, reviewer selection,
qualification or repository authority.

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

## Execution Host and Workspace Preflight evidence

Before an Inbox item is claimed, Execution Host Preflight Level 1 records local
host evidence: Execution Host identity, version, Bootstrap Contract, timestamp,
duration, outcome and compact per-check results. Level 2 then records Workspace
Preflight evidence: workspace, target repository, branch, execution mode,
timestamp, duration, outcome and compact per-check results. Both stages fail
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
Architect. Its output is advisory and redacted before persistence.

The dashboard displays that analysis only within **Laatst uitgevoerd** and only
when its run identifier matches the displayed terminal run. A failed or absent
analysis never changes the terminal checkpoint, report, repository state,
validation result or lifecycle outcome.

For the last completed execution, the dashboard also presents a compact
read-only summary of the Execution Host, Target Repository, Target Commit and
Evidence Bundle changed-file count. The complete Evidence Bundle remains in
the Engineering Report, so the dashboard does not duplicate its detail.

## Private dashboard evidence access

The dashboard renders a report and an advisory analysis as read-only Markdown
only after the maintainer opens the relevant evidence view. It provides local
copy and download actions only when the matching artifact exists. Downloaded
files contain the original local Markdown; rendering and copying do not alter
the report, checkpoint or target repository.

**Promptgeschiedenis** is a private SQLite-backed index of terminal runs. Its
report action opens the selected report in the same read-only Markdown dialog,
not in an editor. It is deliberately an evidence-navigation feature rather
than an execution or repository-control surface.

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
- **Codex CLI Version**, when detected.

A value is explicitly shown as `not reported` when the provider does not emit
it. The runner never infers or fabricates model, reasoning or configuration
metadata. These fields describe the process that produced this specific report;
they are not a claim about a currently configured provider or a later run.

The matching **Laatst uitgevoerde prompt** dashboard card reads the provenance
only from that terminal report. It therefore cannot display a model or profile
from an unrelated current run.

## Retry executions

A retry report contains a **Retry Relationship** section with Retry Of,
Original Run, Retry Generation, Retry Timestamp, Current Run, Terminal State
and Repository Context. It documents that new execution only; original reports,
checkpoints, telemetry and evidence remain immutable.
