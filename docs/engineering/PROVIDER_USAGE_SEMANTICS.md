# Provider Usage Semantics

## Scope

This document defines the Engineering Platform read model for usage emitted by
Codex CLI JSONL. It applies to newly persisted provider invocations; immutable
historical reports retain their report-version terminology.

## Invocation boundary

One Engineering Platform provider invocation is one foreground `codex exec`
process launched by `CodexCliClient`. A run may contain several invocations:
specialist reviews, a primary execution and, when required, a repair or
Finalization execution. They are persisted independently and run totals are the
sum of those invocation totals.

An EP invocation is not evidence of one underlying model request. Codex CLI
0.147.0 emits one final `turn.completed.usage` snapshot per observed `codex
exec`. It does not expose intermediate usage snapshots, an underlying
model-request count, request boundaries or an active-context size.

## Usage counters

`turn.completed.usage.input_tokens` is stored as **provider invocation
cumulative input tokens**. It includes cached input where Codex reports a
cached-input counter. It must not be described as a context window, actual
context size or a single-request size.

The read model distinguishes:

- **Run cumulative input**: the sum of final cumulative input counters across
  persisted EP provider invocations.
- **Provider invocation cumulative input**: the final counter for one
  `codex exec` invocation.
- **Actual active context / single-request size**: `UNAVAILABLE` unless Codex
  explicitly emits that measurement.

## Snapshots and deltas

Only `turn.completed` events with a structured `usage` object are usage
snapshots. The parser retains no prompts, model replies, command text, paths or
tool output. Current Codex CLI behavior produces one final snapshot, yielding a
final cumulative total and no intermediate delta. If a future provider actually
emits multiple authoritative snapshots, their monotonic counter deltas may be
stored; the platform does not sample or fabricate snapshots.

## Attribution limits

The platform separately records bounded command/output indicators per
invocation: repository/source reads, tests, Git/GitHub output, tool output and
repeat-read counts. These are correlations, not token allocations. When Codex
does not publish intermediate usage snapshots, exact input growth by activity
is `UNAVAILABLE`.

## Session reuse

The execution command does not pass `resume` or a prior Codex thread ID. Each
EP provider invocation starts a fresh Codex CLI process. Whether Codex performs
hidden internal model interactions, or carries any server-side session state,
is not observable from the current JSONL contract and remains `UNAVAILABLE`.
