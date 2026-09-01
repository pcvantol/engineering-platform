# Engineering Platform public contracts

`tools.engineering.contracts` is the provider-neutral, read-only boundary for
future Workspace consumers. `get_run_context(root, run_id)` returns a versioned
JSON-compatible `run_context` projection; `get_allowed_actions(root, run_id)`
returns only descriptors currently produced by Engineering Platform policy;
`evaluate_action(...)` requires a fresh matching evidence version.

The projection is derived from canonical EP evidence and is never lifecycle or
merge authority. It performs no network calls, lifecycle transitions, storage
migrations, Git operations or action execution. Incompatible contract major
versions fail closed.

The boundary never projects raw prompts, credentials, command/provider output,
reviewer reasoning, absolute local paths or executable commands. Missing
evidence remains `UNAVAILABLE`; it is not converted to success or zero.
Unsafe objective metadata is omitted as `UNAVAILABLE`, rather than redacted
into a new representation of immutable prompt content.

Only read-only inspection descriptors are exposed in this increment. Future
mutating actions must be re-evaluated against fresh canonical evidence by an
Engineering Platform action gateway and cannot be created by a Workspace or AI.
