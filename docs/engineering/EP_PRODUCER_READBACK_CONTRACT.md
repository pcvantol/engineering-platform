# EP producer readback contract v1.2

`v1.2` is the consumer-visible, authenticated readback contract for a
canonical EP submission.  It belongs to the existing EP Server HTTP JSON API;
it is not a second API, consumer database, queue, or execution authority.

## Resource and authorization

```
GET /v1/projects/{project_id}/submissions/{submission_id}
Authorization: Bearer <EP-issued scoped credential>
```

The caller must hold an active EP credential for exactly `{project_id}`.  EP
returns `401 UNAUTHENTICATED` for an absent or invalid credential and `404
SUBMISSION_NOT_FOUND` when the submission is absent from that exact project.
The latter intentionally includes cross-project identities.  The endpoint
never reads a Forge checkout, browser session, Console HTML, logs, or consumer
storage.

## Response identity and evidence

The JSON response is schema version `1.2` and contains the immutable canonical
submission ID, project/repository IDs, producer provenance, submitted
correlation/mission/engineering-action IDs, and a server-computed
`accepted_request_digest`. `run` is `null` until CENTRAL has claimed the
accepted submission; once present, its run ID and lifecycle state are canonical.

`disposition` is separate from the execution result. It records the current
submission state, monotone revision, worker eligibility and, where an operator
command exists, its operation/event reference, verified actor reference,
reason and timestamp. `DECLINED` is terminal for the submission while keeping
`run: null` and `result.outcome: NOT_STARTED`; EP never fabricates an execution
receipt, commit or terminal artifact for a declined-but-unclaimed submission.

Forge requests place the versioned, exact execution identity under
`constraints.forge_execution`: host, repository, correlation, mission and
revision, intent and revision, action, runtime-prompt ID/digest, and retry
predecessor. EP validates that the duplicated boundary identities agree before
admission. Other producers do not need this object. The entire canonical
constraints object is already part of EP's idempotency comparison, so a replay
with a changed runtime prompt or other relevant provenance is rejected.

At the existing finalization writer, EP serializes an `EP_TERMINAL_EVIDENCE`
JSON artifact using UTF-8 canonical JSON (`sort_keys`, compact separators,
then exactly one newline) and registers its SHA-256. It binds submission, run,
accepted provenance, outcome, report identity, validation/quality/finalization
references, and a run-bound delivery revision. It never uses current checkout
HEAD or a mutable HTTP response as evidence.

`GET /v1/projects/{project_id}/artifacts/{terminal-evidence:<run_id>}` returns
that stored JSON only after the same project-scoped authentication and digest
verification. A missing, corrupt, or mismatched artifact is represented as
`MISSING`, `CORRUPT`, or `INCOMPLETE`; the reader never fills it in.

`COMPLETE` mutating delivery is qualified only when a recorded merge revision
is present in the checkpoint's verified commit evidence. `VALIDATION_ONLY`,
`BLOCKED`, and `FAILED` may have valid terminal evidence with a null revision;
they are not fabricated into successful delivery.

The machine-readable response shape is
[`producer-readback-v1.2.schema.json`](../../src/engineering_platform/schemas/producer-readback-v1.2.schema.json).

## Operations Console queue disposition

The Console-only mutation boundary is separate from the producer API:

```
POST /api/queue-disposition?project={project_id}
Authorization: Bearer <EP-issued, project-scoped operator credential>
```

The exact JSON v1.0 request has `contract_version`, a unique `operation_id`,
`submission_id`, `expected_state`, integer `expected_revision`, `disposition`
and a bounded operator `reason`.  It rejects duplicate JSON names, unknown or
missing fields, stale state/revision, unknown submissions and invalid state
transitions. Replaying the same operation ID and exact command is idempotent;
using that ID for a different command is a conflict.

Authentication and queue authority are deliberately distinct. A scoped
producer credential receives `403 OPERATOR_CAPABILITY_REQUIRED`; no credential
receives `401 UNAUTHENTICATED`. `QUEUE_HOLD_RESUME` permits defer, quarantine
and resume. `QUEUE_DECLINE` permits terminal decline, including the direct
`QUARANTINED -> DECLINED` path. The worker claim and this command arbitrate in
one CENTRAL transaction, so a claimed submission returns `409` and is never
cancelled by a queue action.

## Forge consumer mapping fixture

The pinned pair [`forge-producer-readback-v1.1.json`](../../tests/fixtures/forge-producer-readback-v1.1.json)
and [`forge-terminal-evidence-v1.1.json`](../../tests/fixtures/forge-terminal-evidence-v1.1.json)
is the shared consumer fixture. Its response digest is the SHA-256 of the
terminal-artifact bytes. Forge maps `submission` and `correlation` directly to
its issued request, `run.id` to `host_run_id`, `report.id` to `report_id`, and
the verified `repository.revision` plus artifact digest to
`ExecutionRepositoryEvidence`. A mismatch is consumer rejection, never a
synthetic successful result.
