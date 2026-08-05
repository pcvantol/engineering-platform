# Execution Host Contract

The Execution Host Contract is the producer-neutral integration boundary
between a Producer and Engineering Platform. Forge owns the canonical Producer
Contract and Producer semantics. Engineering Platform consumes the declared
metadata while remaining independent from Forge implementation.

The contract may supply Producer ID, Producer Type, Producer Version, Producer
Correlation ID, optional Mission ID, optional Engineering Action ID and
Execution Constraint Version. Engineering Platform stores these values as
immutable execution provenance alongside Execution Evidence, Engineering
Reports, Execution Receipts, dashboard projections and operational telemetry.

Supported Producer Types include `HUMAN`, `FORGE`, `EXTERNAL` and `UNKNOWN`.
Additional valid type tokens remain observable for future producers. A prompt
without Producer metadata remains valid and is represented as `HUMAN` producer
type with `legacy` producer ID.

Producer data is read-only audit metadata. It never alters admission,
preflight, scheduling, lifecycle, execution, reviewer selection, execution
evidence semantics or terminal outcomes. Engineering Platform performs
Engineering Actions only; it never performs Mission planning, Business
governance or Architecture governance.
