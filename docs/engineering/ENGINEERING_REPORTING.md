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

When a transaction reaches `COMPLETE`, an initial finding about a missing
capability is labelled as resolved during implementation unless the terminal
checkpoint records a remaining limitation.

## Repository truth

The report resolves evidence in this order:

1. persisted repository state and terminal checkpoint;
2. resulting commits;
3. validation evidence; and
4. advisory reviewer observations.

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
