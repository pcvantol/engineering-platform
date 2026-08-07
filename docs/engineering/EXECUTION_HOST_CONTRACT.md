# Execution Host and Producer Contract

The Execution Host Contract is the producer-neutral integration boundary
between a Producer and Engineering Platform. Forge owns the canonical Producer
Contract and Producer semantics. Engineering Platform consumes the declared
metadata while remaining independent from Forge implementation.

Engineering Platform executes compliant Engineering Actions from any Producer,
including a Human Architect, Forge and future Producers. The execution
lifecycle, qualification, preflight, evidence, reports and receipts are
identical regardless of the Producer that supplied the action.

The contract may supply Producer ID, Producer Type, Producer Version, Producer
Correlation ID, optional Mission ID, optional Engineering Action ID and
Execution Constraint Version. Engineering Platform stores these values as
immutable execution provenance alongside Execution Evidence, Engineering
Reports, Execution Receipts, dashboard projections and operational telemetry.

Supported Producer Types include `HUMAN`, `FORGE`, `EXTERNAL` and `UNKNOWN`.
Additional valid type tokens remain observable for future producers. A prompt
without Producer metadata remains valid and is represented as `HUMAN` producer
type with `legacy` producer ID.

Producer data is read-only audit metadata. It exists only for traceability,
audit and Execution Evidence. It never alters admission, preflight, scheduling,
lifecycle, execution, reviewer selection, execution evidence semantics or
terminal outcomes.

## Versioned Producer Submission Envelope

The Inbox accepts one atomic JSON Producer Submission Envelope in addition to
the canonical plain-text prompt compatibility path. Version `1.0` uses this
shape:

```json
{
  "contract": {"name": "djconnect.producer_submission", "version": "1.0"},
  "submission": {"id": "producer-submission-id"},
  "producer": {"id": "producer-id", "type": "FORGE"},
  "prompt": {"text": "Engineering prompt"},
  "execution_context": {"context_version": "1.0"}
}
```

`execution_context` is optional. Its object schema is validated independently
from `prompt.text`; unknown object fields are preserved for forward-compatible
producers. Contract name/version, required submission identity, known field
types and the context version fail closed before the Inbox claim. A JSON-like
but invalid submission is never treated as a legacy prompt or partially
processed.

The exact submitted JSON envelope, submission metadata, Producer provenance,
validated optional Execution Context snapshot and run linkage are written once
to the canonical datastore before the Inbox file is consumed. Snapshot rows
are insert-only: historical executions retain the exact snapshot supplied for
their run. Plain-text prompts remain valid legacy Human Producer submissions
with prompt-only transport.

Forge owns Execution Context generation. Engineering Platform owns only this
transport, immutable persistence and read-only presentation layer. It never
parses prompts for context, reads Forge Runtime or repositories, or derives
Mission semantics.

## Managed transaction authority

The Execution Host acquires the exclusive run lease, performs admission and
synchronizes the repository before it invokes the implementation agent. A
managed, owner-authorized transaction then runs in the Codex CLI's
`danger-full-access` sandbox profile so that the already-authorized bounded
transaction can create its branch, stage its own scoped changes, commit and
open its draft pull request. This is not an unrestricted lifecycle authority:
the supplied transaction scope, branch and pull-request rules still apply, and
the runner alone marks a pull request ready or merges it. Review invocations
remain `read-only`.

`workspace-write` is intentionally not used for managed transactions because
it denies Git index writes. It is suitable for edit-only work, but would leave
a completed implementation without the commit and repository evidence required
by the Execution Host Contract.

## Architectural boundary

Forge defines Mission, planning, Runtime Prompts, Decision Evidence and Runtime
Instance concepts. Engineering Platform executes Engineering Actions and owns
their execution lifecycle, Execution Evidence, Engineering Reports and
Execution Receipts.

Engineering Platform performs Engineering Actions only. It never performs
Mission Planning, Business Governance, Architecture Governance, Mission
Recommendation, Decision Evidence, Runtime Planning, Portfolio management or
Forge implementation. No future Engineering Platform capability may duplicate
these Forge responsibilities.
