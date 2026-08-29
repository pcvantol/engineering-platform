# Run Qualification Evidence Contract

Run Qualification is derived only from run-scoped, persisted evidence. It is
separate from Platform Qualification and execution outcome. A platform-level
pass must never upgrade an individual run.

## Submission lineage

Before deterministic admission or provider-backed work, Inbox intake persists
one immutable `execution_run_qualification_context` record. It contains the
submission and producer identity through the linked submission record plus:

| Submission kind | `fresh_submission` | Retry parent | Resume parent |
| --- | --- | --- | --- |
| New | `true` | `null` | `null` |
| Retry | `false` | run ID | `null` |
| Resume | `false` | `null` | run ID |

Dual parentage is rejected. Existing historical runs receive no synthetic
record and therefore remain `UNAVAILABLE`/`EVIDENCE_INSUFFICIENT`.

## Provider-dispatch admission

Provider-backed work has one fail-closed boundary:

`SUBMISSION_PERSISTED → LINEAGE_PERSISTED → DETERMINISTIC_ADMISSION → provider dispatch`

For a watcher-spawned Managed run, the Execution Host must read the immutable
admission decision for its exact run ID and find `PASS` before it can select
or invoke reviewers, implementation, validation, quality, repair,
finalization, or reconciliation providers. Missing, incomplete, unavailable,
failed, blocked, or inconsistent admission evidence terminates the run before
provider dispatch. The checkpoint records the completed decision and source;
provider invocation telemetry records that dispatch followed admission.

## Required validation

The local validation gate persists the selected tier, profile version, and
exact required validation IDs before recording control results. Every result
has a stable ID, category, control identity, required marker, execution
status, result, observation time, and evidence reference.

The read-only Run Context API projects these same persisted lineage fields and
required-validation result. It exposes `null` parents for a fresh submission;
legacy runs with no v33 record remain `UNAVAILABLE` rather than falling back
to submission or prompt-derived lineage.

`PASS` requires authoritative `PASS` for every required control. A failed
required control is `FAIL`; missing, conflicting, or unexecuted mandatory
evidence is `UNRESOLVED`. Optional controls do not affect required-validation
pass status.

## Evidence audit matrix

| Evidence area | Current classification | Qualification role |
| --- | --- | --- |
| Submission lineage | Canonically persisted | Mandatory |
| Validation profile and controls | Canonically persisted | Mandatory |
| PR check observations | Canonically persisted | Mandatory where PR exists |
| Telemetry uniqueness and daily aggregation | Canonically persisted | Non-mandatory operational evidence |
| Commit timeline | Canonically persisted | Mandatory delivery evidence |
| Codex, GitHub, host readiness | Canonically persisted | Admission/readiness evidence |
| Capacity-reserve admission | Canonically persisted | Admission evidence |

This contract is prospective only. It neither changes Prompt History nor
upgrades the historical PR #973/#974-backed qualification.
