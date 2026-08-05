# Execution Receipts

Execution Receipts are immutable, local Engineering Platform evidence for a
terminal Execution Host run. Engineering Platform owns their storage and
exposure. Forge may reference a receipt but does not own it.

Each receipt records Producer ID, Producer Type, Producer Version when
supplied, Mission ID and Engineering Action ID when supplied, Correlation ID,
Execution Constraint Version, Execution Host identity and version, Execution
Run ID, receipt timestamp and terminal execution outcome.

Receipts are producer-neutral: Forge owns Producer semantics; Engineering
Platform owns execution semantics. Producer information supports auditability,
traceability, usage statistics, execution-distribution analysis and future
operational analytics only. It cannot influence execution behaviour or
scheduling. A receipt is not Mission planning, Decision Evidence or a Forge
runtime record.
