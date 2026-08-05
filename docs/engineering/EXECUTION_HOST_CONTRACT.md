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
