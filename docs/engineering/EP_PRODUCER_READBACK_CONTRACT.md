# EP producer readback contract v1.0

`v1.0` is the consumer-visible, authenticated readback contract for a
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

The JSON response is schema version `1.0` and contains the immutable canonical
submission ID, project/repository IDs, producer provenance, and the submitted
correlation/mission/engineering-action IDs.  `run` is `null` until CENTRAL has
claimed the accepted submission; once present, its run ID and lifecycle state
are canonical.  `result` is a projection of that same lifecycle state.

`evidence` exposes only stable CENTRAL references (`central-submission`,
`central-run`, terminal transaction and terminal report references).  It never
contains prompt text, bearer material, provider credentials, filesystem paths,
or reconstructed Forge semantics.  A terminal result is not asserted until
the canonical transaction checkpoint is terminal.

The machine-readable response shape is
[`producer-readback-v1.schema.json`](../../src/engineering_platform/schemas/producer-readback-v1.schema.json).
