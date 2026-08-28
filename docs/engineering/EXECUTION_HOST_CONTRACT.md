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
the supplied transaction scope, branch and pull-request rules still apply. The
runner may mark a pull request ready for review, but its merge remains
operator-owned. A green, open pull request is persisted as
`WAIT_FOR_OPERATOR_MERGE`; it is not a failed execution and it must keep its
Inbox position until the operator merges it or explicitly aborts the hand-off.
The watcher reconciles that same durable state after a later restart or browser
session and continues Finalization only after GitHub reports the merge. Review
invocations remain `read-only`.

When GitHub confirms a merge, the watcher replaces its
`WAITING_FOR_OPERATOR_MERGE` projection with the resumed execution phase in
the same reconciliation cycle. It must not publish the earlier merge wait
again after the run has moved to Finalization; otherwise a dashboard refresh
could present an obsolete pull-request action modal.

An open pull request with failed required checks is not merge evidence and must
not be presented as a completed merge. The host enters bounded validation
repair with `repair_bounded_validation_failure` as its current action. The
Operations Console presents that action as “Fix pull request checks” in the
selected UI language, keeps the Merge lifecycle node blocked without a
checkmark, and returns it to active only when the PR is again awaiting an
operator merge. A Merge completion is projected only from subsequent
finalization evidence or a successful terminal outcome.

The host permits at most three automatic required-check repair attempts for
the same owner-authorized pull request. If required checks still fail after
the third repair, it records the failed check names and stops the execution as
`BLOCKED` with `repair_attempt_limit_reached`; it does not invoke another
agent repair. A single provider invocation can still end earlier under its own
runtime limits, and transient GitHub evidence reads remain separately bounded.

Before an implementation pull request exists, local repository validation has
its own independent, three-attempt repair budget. If the implementation agent
returns `FAILED` only after the host has verified a clean transaction branch
and exact commit, and its bounded validation evidence records a failed local
suite, the host routes that result into the local validation gate instead of
classifying it as an external dependency. Each attempt records checkpoint and
audit evidence. Explicit external blocks, provider/auth failures, missing or
unverified branch/commit evidence, and non-validation agent failures remain
fail-closed and never invoke this repair route. A still-failing local suite
after the third attempt stops as `BLOCKED` with
`local_validation_attempt_limit_reached`. This local budget is separate from
the later pull-request required-check repair budget.
When finalization creates its own pull request, that is a second, distinct
operator merge handoff: the implementation Merge node remains completed,
Finalization remains completed, and the console shows a separate active
Finalization merge node with the finalization PR's Open pull request and Abort
execution controls.

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
