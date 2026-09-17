# Provider usage and telemetry semantics

## Owning contract

Engineering Platform owns the read model defined by
`telemetry-contract@2.0`. `provider_usage.py`, `execution_timing.py` and
`telemetry_contract.py` are the calculation authority. The API, dashboard,
Engineering Report, Markdown download and JSON export project that contract;
they must not independently calculate totals or bottlenecks.

Every metric states its meaning, unit, aggregation level, provenance,
calculation version, source snapshot, expected observations, observed
observations and coverage. Coverage is `COMPLETE`, `PARTIAL`, `UNAVAILABLE` or
`CONFLICT`. `AUTHORITATIVE` describes provenance, not completeness. A measured
zero, an unknown value and an unavailable observation are distinct.

The scopes are:

- `EP_RUN_ATTEMPT`: exactly one EP execution attempt;
- `EXECUTION_CHAIN`: only attempts joined by retained retry/resume parent IDs;
- `EP execution within this Mission`: Mission/Action identity copied through
  the existing accepted Forge submission interface. It is not total Forge
  planning time or usage.

## Supported Codex event contract

Qualification verified the installed and npm-supported `codex-cli 0.154.0`
and `codex exec --json`. Public OpenAI lifecycle documentation establishes
that streaming responses have lifecycle updates followed by terminal events
and that usage is optional until supplied by a terminal response. EP therefore
does not treat each lifecycle update as a separate operation and does not infer
missing usage. The regression fixtures cover the supported JSONL shapes rather
than depending on an old comment or one captured invocation.

One EP provider invocation is one foreground `codex exec` process launched by
`CodexCliClient`. It is not evidence of one underlying model request. A run can
contain primary, reviewer, repair and finalization invocations.

Within an invocation, item/tool-call identity is `(invocation_id, item_id)`.
`started`, `updated` and `completed` for that identity are one item. A replayed
terminal observation is counted once; conflicting terminal observations make
the affected metric conflicting. Equal item IDs in different invocations stay
separate. Missing item IDs make exact item-level counters partial or
unavailable. Cumulative output updates contribute only the largest compatible
observed representation, not every repeated prefix.

Only safe counters and opaque hashes are retained. Raw prompts, replies,
secrets, command text, full paths and output dumps are not telemetry fields.

## Usage

Input and output are cumulative provider-invocation observations. Cached input
is a component of input, never additional token volume. Uncached input is
derived only where input and cached input are both valid and compatible.
Booleans, negative values, cached input greater than input, counter resets and
non-monotone snapshots are rejected or marked conflicting. Replayed identical
snapshots do not add usage. Deltas are used only across demonstrably monotone
snapshots from the same invocation.

Totals sum the compatible final observation for each invocation. Missing
fields are not replaced with zero. Coverage is calculated separately for
input, cached input, uncached input, output and duration. A cache ratio uses
only invocations where input and cached input share compatible coverage.

The largest invocation input is **largest cumulative invocation input**. It is
not an active context size or context-window measurement. Actual request count,
single-request context size, active context and model inference time remain
`UNAVAILABLE` unless a provider boundary reports them directly.

An observed runtime model ID is retained safely even if it is not in a
normalization list. A configured model can be shown separately with provenance
`CONFIGURED`; it is never substituted for an unobserved runtime model. Cost is
an estimate only when model, rate and compatible usage are known. Estimate,
billing and subscription budget are separate concepts.

## Provider activity counters

Historical PR measurements distinguish search/list queries, structured result
occurrences, unique repository/PR identities and actually fetched detail/diff
observations. Output lines are never PRs inspected. When only old unstructured
output-line totals survive, the value is named `legacy_historical_pr_output_lines`
and exact PR fields are unavailable.

Read-command counters are derived command observations. Exact file-read
observations require reliable tool metadata for opaque file identity, revision
and (when relevant) range. Repeating a shell command is not proof that a file
was reread. No full command or sensitive path is persisted.

Tool output bytes mean deduplicated bytes observed at the provider-event
boundary, not bytes delivered to the model. Test output is split into known
passing, known failing and unknown-exit categories. Shell-text recognition is
`DERIVED`, not proof that a test ran or a file existed.

## Timing and lineage

Inclusive phase workload retains nested work and may exceed 100%; its shares
are explicitly non-additive. The exclusive elapsed-time distribution sweeps
the retained `TOTAL_EXECUTION` envelope. The deepest active child owns a
segment; simultaneous independent categories become `PARALLEL_OVERLAP`; gaps
become `UNASSIGNED`. No proportional rescaling is used. Complete compatible
intervals close exactly on elapsed time except presentation rounding.

Provider process duration is cumulative monotonic process lifetime. Provider
coverage is the wall-clock interval union inside the run envelope. Neither is
model inference time. Conflicting clocks or boundaries produce `CONFLICT`
instead of a clamped total.

Execution chains use only explicit submission, attempt, retry-parent and
resume-parent relations. Cycles, duplicate edges and missing parents degrade
coverage and never duplicate usage. Technical completion, qualification and
autonomy acceptance remain separate states.

Immutable historical reports and qualification evidence are never rewritten.
Read-time reprojection retains its calculation version and source reference;
missing historical event metadata is never fabricated.
