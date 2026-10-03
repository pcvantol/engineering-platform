# EP per-repository consumer authority readback v1

**Owner:** Engineering Platform. **Assignment:**
`L2-EP-PER-REPOSITORY-AUTHORITY-READBACK-V1-20261003`. **Version policy:**
`NO_BUMP` unless the owning release policy later selects a release.

`GET /v1/projects/{project_id}/repositories/{repository_id}/consumer-authority`
is an authenticated, read-only Server endpoint. It returns
`ep-repository-consumer-authority/v1`; the installed response schema is
[`repository-consumer-authority-v1.schema.json`](../../src/engineering_platform/schemas/repository-consumer-authority-v1.schema.json).
The caller supplies a normal scoped consumer bearer plus required
`EP-Instance-ID`, `EP-Consumer-ID` and `EP-GitHub-Repository` headers. The
consumer identity is derived from the bearer; the headers express the
consumer's expected scope and cannot select a different principal.

The positive response binds one current EP instance, active project,
registered repository and role, active consumer, explicit active parallel
Action repository grant, validated local repository attachment and current
GitHub fetch origin and single effective push target. It returns
`local_repository_binding=BOUND`, the concrete `github_repository`,
`binding_revision` and `authority_digest`. It discloses
neither the local path nor credential material. The attachment digest and
schema version identify the repository declaration used. Two repositories
under one project have distinct local bindings, grants and revisions; no
singleton selection is rotated to produce a second readback.
The project contract must name an existing authority repository in that
project with role `authority`; inconsistent or missing authority topology is
rejected.

Both hashes use canonical JSON with sorted keys and compact separators.
`binding_revision` hashes the repository registration and update timestamps,
the registered attachment, the local binding root and update timestamps,
the per-consumer/per-repository grant timestamps, and the current GitHub
origin and effective push target. Product bind/rebind/unbind and grant/revoke
operations advance their update timestamps beyond the previous value even
within one clock tick.
`authority_digest` additionally binds the instance, active project status,
consumer status revision and all public authority fields. It excludes the
project registration timestamp, which also changes when a sibling repository
is registered. A client may supply
`EP-Authority-Revision` and/or `EP-Authority-Digest`; a stale or wrong pin is
rejected with HTTP 409. The response is stable across Server restarts while
the authority facts are unchanged. Consumers must retain their accepted
revision/digest with the exact Action and check current authority before
making their own dispatch decision; a later binding cannot rewrite an old
Action's pinned evidence.

Missing headers or malformed pins return HTTP 400. Missing or inactive bearer
returns 401. Wrong instance, consumer, project, repository, origin, inactive
project/grant, missing/unbound local binding and attachment drift return 403
without a positive authority body. CENTRAL unavailability returns 503. The
response has `Cache-Control: no-store`. A positive readback reports
`submission_authorization=PARALLEL_ACTION_INTAKE` and
`dispatch_authorized=false`: it certifies the current EP side of a scoped
intake only. Forge retains its own Mission/Action approval, current per-target
baseline Truth, policy and final dispatch authority. No provider or
repository write occurs on this GET route.

The HTTP qualification uses real CENTRAL registration, bearer validation,
owner-set repository grants and two independently bound Git checkouts with
distinct origins. It verifies restart stability, pin drift, independent
rebinding and revocation, wrong identities and source drift. Installed-wheel
qualification repeats the HTTP boundary against a non-editable package from
the exact delivery commit. This contract does not qualify live providers,
shared non-Git resources or Forge PA-F1/F3 acceptance by itself.
