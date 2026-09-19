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

For runs created by the protected-reconciliation contract, qualification also
requires the exact reconciliation PR, terminal required-check evidence and a
satisfied `RECONCILIATION_MERGE_APPROVAL` gate. Historical qualification
snapshots that predate this contract are not backfilled with a third PR or
fabricated merge evidence.

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

The Execution Host, not the read-only provider, produces those authoritative
terminal receipts. Provider-reported commands remain optional observational
telemetry under a distinct identity and can never satisfy, replace, or
conflict with a required control. For a non-EP checkout with a conventional
`tests/` root, the documentation profile binds its documentation-contract
control to that checkout's `python3 -m unittest discover -s tests` suite;
EP's specialised documentation-contract test is used only for an EP checkout.

## Authenticated Forge control readback

The project-scoped consumer first reads its submission at
`GET /v1/projects/{project_id}/submissions/{submission_id}` and then reads the
referenced, digest-verified terminal artifact at
`GET /v1/projects/{project_id}/artifacts/{terminal_artifact_id}`. Both routes
require the existing project consumer credential. This read path executes no
control or provider command. The outer terminal artifact remains version
`1.4`; new artifacts add `validation_controls` contract `1.0`. An older
artifact without that member stays valid history but supplies no criterion
control proof.

`validation_controls` freezes the persisted profile tier/version/reference,
the candidate-bound profile digest and candidate SHA, currentness, required
control IDs and each required host control observation. Each control includes
its stable definition digest, computed from profile version/reference,
validation ID/category/control identity and every command argument. Only an
exact first argument matching EP's runtime Python executable is replaced by
the fixed `{python}` token before hashing; all other arguments stay exact.
The command argument vector is not separately published in the new snapshot.
The actual resolved argument vector remains part of the separate candidate-bound
profile digest. Each control also includes execution status, result,
command ID, start/end times, exit code and evidence authority. A profile
digest includes candidate and currentness, so it is a run binding, not a
preapproval identity. Forge preapproves a stable control definition and owns
the interpretation of that control for a Mission criterion.

For deterministic host controls, a second immutable local artifact retains
only the command's run/command/control identity, exit code, capture state,
SHA-256 output digest and a parsed unittest `Ran N tests` count. The terminal
artifact embeds the detail only after checking the local artifact digest and
exact run/command/exit binding. Raw output and arbitrary command text are not
published. `test_count=0` means zero discovered tests; `null` means no
recognized count. Neither establishes behavioral test execution. A passing
exit code alone therefore does not prove a functional criterion. The detail
does not claim coverage beyond the approved test/control scope.

The terminal artifact's existing submission, correlation, run, repository,
candidate, assurance and delivery fields bind the control snapshot to the
accepted Action and delivery. An unavailable, missing, conflicting, skipped,
nonterminal or corrupt control stays unproven. EP confirms observed execution;
Forge separately decides criterion fit and revision validity. The result
detail is not a signature or independent third-party attestation; its trust
boundary is the authenticated EP Server readback of an integrity-checked
owning record.

## Historical dashboard projection

For a terminal blocked or failed run, the detail projection exposes the safe,
persisted terminal diagnostic as `execution_diagnostic` and
`blocking_reason`. Duration and runtime fields are projected from the same
immutable execution snapshot used by run history, rather than returned as
empty placeholders. This is presentation only: it never creates a diagnostic
or timing fact for a historical run that did not record one.

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
