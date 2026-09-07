# EP producer readback contract v1.1

`v1.1` is the consumer-visible, authenticated readback contract for a
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

The JSON response is schema version `1.1` and contains the immutable canonical
submission ID, project/repository IDs, producer provenance, submitted
correlation/mission/engineering-action IDs, and a server-computed
`accepted_request_digest`. `run` is `null` until CENTRAL has claimed the
accepted submission; once present, its run ID and lifecycle state are canonical.

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
[`producer-readback-v1.schema.json`](../../src/engineering_platform/schemas/producer-readback-v1.schema.json).

## Forge consumer mapping fixture

The pinned pair [`forge-producer-readback-v1.1.json`](../../tests/fixtures/forge-producer-readback-v1.1.json)
and [`forge-terminal-evidence-v1.1.json`](../../tests/fixtures/forge-terminal-evidence-v1.1.json)
is the shared consumer fixture. Its response digest is the SHA-256 of the
terminal-artifact bytes. Forge maps `submission` and `correlation` directly to
its issued request, `run.id` to `host_run_id`, `report.id` to `report_id`, and
the verified `repository.revision` plus artifact digest to
`ExecutionRepositoryEvidence`. A mismatch is consumer rejection, never a
synthetic successful result.
