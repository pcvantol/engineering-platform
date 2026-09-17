# Provider usage and telemetry semantics

## Owning contract

Engineering Platform owns the read model defined by
`telemetry-contract@2.2`. `provider_usage.py`, `execution_timing.py` and
`telemetry_contract.py` are the calculation authority. The API, dashboard,
Engineering Report, Markdown download and JSON export project that contract;
they must not independently calculate totals or bottlenecks.

Every metric states its meaning, unit, aggregation level, provenance,
calculation version, source snapshot, expected observations, observed
observations and coverage. Coverage is `COMPLETE`, `PARTIAL`, `UNAVAILABLE` or
`CONFLICT`. `AUTHORITATIVE` describes provenance, not completeness. A measured
zero, an unknown value and an unavailable observation are distinct.

Coverage aggregation retains expected, present, valid, missing and conflicting
observations. A source `CONFLICT` remains `CONFLICT` at run, UTC-day and chain
scope even when its numeric present count equals the expected count. Unknown
expected populations remain unknown. Independent metrics propagate only their
own dependencies; one invalid cached-input observation does not invalidate an
otherwise valid output observation.

A numeric aggregate marked `VALID_OBSERVATIONS_SUBTOTAL` contains only the
observations that remain valid for that metric. Higher scopes retain that
subtotal while independently retaining `PARTIAL` or `CONFLICT` coverage and
its observation counts. An unmarked numeric value from a conflicted legacy
source is not admitted. This makes sums and maxima independent of whether the
same population is grouped first by run, day, chain or overview. A compatible
cache-ratio population follows the same rule and preserves a measured zero.

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

Opaque PR identities are retained up to a fixed privacy/storage bound. Every
query-bearing invocation records whether its retained identity set is complete
and whether it was truncated. Scope-wide uniqueness is exact only when every
contributing set is explicitly complete. Otherwise the exact value is `null`,
coverage is `PARTIAL`/`UNAVAILABLE`/`CONFLICT` as applicable, and a safe lower
bound is the greater of the retained union and each source's own lower bound.
Legacy rows without an explicit set-completeness marker are never promoted to
an exact complete union. A modern record that claims completeness while its
declared unique count, retained count, opaque identities, truncation marker or
coverage disagree fails closed as `CONFLICT`; its safe lower bound may remain
visible, but it is not projected as an exact unique total. The same invariant
is checked again during read projection, so a pre-existing corrupt or
inconsistent stored row cannot bypass the owning writer's validation.

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

The exclusive distribution uses the wall-clock interval envelope for both its
positions and durations. The independently measured monotonic process duration
remains `total_monotonic_duration_ms`. Their signed difference is reported as
`clock_difference_ms`; it is never inserted into `UNASSIGNED`. Category
durations are accumulated at timestamp precision and rounded together only for
the integer-millisecond presentation, so no category can become negative.

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

## Export contract

`telemetry-export@1.1` is the read-only export envelope for the overview and
detail Markdown/JSON downloads. Each envelope carries a snapshot digest,
source-as-of timestamp, separate download timestamp, project, UTC selection, scope, source references, displayed
and full population, export completeness and metric coverage. Markdown and
JSON serialize that one model; neither recalculates totals.

All CENTRAL reads used to build one model share one short read-only SQLite
transaction. The transaction is released before serialization or download.
The already projected, privacy-safe model is retained in a bounded in-memory
cache for ten minutes so Markdown and JSON can read back the same snapshot ID.
A missing, expired, project-foreign or selection-foreign ID fails explicitly;
it is never replaced silently by a newer read. The cache has independent
per-model, item-count and total encoded-byte limits. It evicts oldest snapshots
within that process-wide budget; a single model that cannot fit is rejected as
an explicit `TELEMETRY_EXPORT_SNAPSHOT_TOO_LARGE` product response rather than
being truncated or retained without a bound.

The overview export reads the entire active project/filter population through
bounded 500-record database pages inside the same read transaction; the
1,000-run Console preview limit and 360-day UI limit are not export limits.
Usage and timing reducers page that identical run population in 500-identifier
batches as well. It therefore contains every retained row and its available
measurements rather than only the visible page.
Detail export can select the UTC day,
one attempt, or its verified execution chain. Full exports disable the UI
preview limits for runs, invocations and spans. A chain detail includes a safe
full attempt record, invocations, spans, timing distribution and coverage for
every retained verified member, including members outside the selected UTC
day. Export completeness is separate from measurement coverage. JSON retains numeric machine
values and `null`; Markdown localizes human headings and explicitly renders
unavailable values. Neither export includes prompts, replies, commands,
secrets, raw tool output, or span metadata outside the telemetry allow-list.

Synthetic, secret-free examples are retained with this contract:

- [overview Markdown](examples/telemetry-followup/telemetry-overview-example.md)
- [overview JSON](examples/telemetry-followup/telemetry-overview-example.json)
- [detail Markdown](examples/telemetry-followup/telemetry-detail-example.md)
- [detail JSON](examples/telemetry-followup/telemetry-detail-example.json)
