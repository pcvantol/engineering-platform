# Integral assurance and repair contract v2

## Purpose

The bounded repair budget is a safety boundary. It must not become a sequence
of partial repository reviews in which every repaired candidate reveals the
next predictable lifecycle defect. Mandatory assurance therefore reviews one
complete candidate impact boundary before any repair is dispatched.

## Candidate review boundary

Quality and Security remain separate read-only provider invocations. Each is
bound to the same clean candidate, base, delivery role, validation profile and
original approved Action. A repair wave always reassesses the complete branch
against its base; it is never scoped to only the latest repair commit.

For an implementation candidate, Quality must account for:

- the approved criteria;
- the complete candidate diff;
- callers and consumers;
- service startup and shutdown;
- maintenance, recovery and cleanup;
- persistence and concurrency;
- transport and schema compatibility;
- regression validation.

Security must account for:

- the approved criteria;
- the complete candidate diff;
- trust and authentication;
- data integrity and concurrency;
- maintenance, recovery and cleanup;
- failure redaction;
- availability and resource bounds;
- security validation.

Finalization and reconciliation use smaller role-specific matrices for their
governance-only diffs. EP owns these matrices. A repository cannot omit an
impact surface by changing its Action text.

## Mandatory review output

Mandatory review output contract `2.0` contains three independent parts:

1. `coverage`: exactly one `REVIEWED` or evidenced `NOT_APPLICABLE` record for
   every host-required impact surface;
2. `finding_dispositions`: exactly one `RESOLVED` or `OPEN` reassessment for
   every still-open finding previously raised by that same reviewer;
3. `findings`: all concrete new findings discovered in the complete wave.

Missing, duplicate, malformed or incomplete coverage and disposition evidence
is `UNRESOLVED`. It cannot be interpreted as an empty PASS.

## Monotone finding ledger

Findings remain immutable. A later broad PASS does not close earlier blockers.
Only the reviewer that raised a finding can explicitly mark its exact finding
ID `RESOLVED`, with evidence, on a repaired candidate. EP then appends one
resolution record bound to the repair identity, review invocation and candidate
SHA. An `OPEN` disposition keeps the original blocker active.

Quality and Security do not consume each other's reasoning. Each receives only
its own earlier open findings. The repair provider receives the complete
cross-review unresolved ledger because it owns the one bounded repository
mutation.

## Integral repair handoff

Before a repair invocation, EP persists its normal immutable reservation and
supplies every unresolved blocker, not merely findings from the latest wave.
The repair instruction requires one coherent change, a complete branch-to-base
impact reassessment and regression evidence across both host-owned impact
matrices. It may not weaken an Action criterion or an existing workflow.

Every repaired candidate still passes deterministic local validation and a new
independent Quality and Security wave. Candidate, base or diff drift fails
closed.

## Budget semantics

The run-wide maximum remains three correction rounds across local validation,
assurance, hosted checks and finalization. The number is a containment limit,
not a convergence strategy. Contract v2 improves the completeness of each
wave and makes progress finding-specific. Exhaustion still blocks the run and
preserves all review, coverage, disposition, resolution and repair evidence.

Increasing the limit requires separate evidence that complete review waves
make monotone progress and that the additional runtime is operationally
acceptable. A failed Mission is not itself permission to increase the limit.

## Compatibility and activation

EP continues to read historical assurance records using contract `1.0`.
New mandatory provider output and autonomous exact-head qualification use
contract `2.0`. Activation requires repository validation, installed-product
qualification, release publication and installation before another production
Mission acceptance attempt starts.
