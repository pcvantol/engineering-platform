# EP producer readback v1.2 and terminal-evidence v1.4 contract

`v1.2` is the consumer-visible, authenticated readback contract for a
canonical EP submission.  It belongs to the existing EP Server HTTP JSON API;
it is not a second API, consumer database, queue, or execution authority.

## Resource and authorization

Before a consumer submits a request it can make this unauthenticated,
read-only installation declaration request:

```
GET /v1/producer-compatibility
```

The response identifies the EP instance and product version and lists the
producer-readback and terminal-evidence contract versions. It has no project,
submission, queue, provider, repair or execution side effect.

Forge's normal peer preflight uses the authenticated form of the same route:

```
GET /v1/producer-compatibility
Authorization: Bearer <EP-issued scoped credential>
EP-Project-ID: <project_id>
EP-Repository-ID: <repository_id>
```

Both scope headers are required together. EP resolves the bearer verifier to
its own active consumer registration and project; it never accepts a caller
supplied consumer identity. A successful `v1.1` declaration reports that
EP-derived consumer, active project, exact authority repository, `BOUND` local
repository attachment and `AUTHORIZED` submission scope. A credential for a
different active consumer in the same project therefore reports that different
consumer. Forge must compare it with its own durable expectation before every
new submission. Missing or invalid credentials return `401`; project or
repository scope mismatch returns `403`. The public request without either
scope header remains the side-effect-free `v1.0` declaration.

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
predecessor. Provenance v1.2 additionally carries the separately versioned
Forge Action Context Envelope: a bounded credential-redacted Action summary,
its source/summary/envelope digests, and fixed generator ID/model/version.
EP validates all of those fields before admission and persists that document as
immutable, submission-scoped CENTRAL evidence. Other producers do not need
this object. The entire canonical constraints object is already part of EP's
idempotency comparison, so a replay with a changed runtime prompt or other
relevant provenance is rejected.

Only that safe, persisted document may be projected in the Operations Console.
EP never derives a summary from the Runtime Prompt or a current Forge record.
Runs submitted under v1.0/v1.1 have no Action Context Envelope and remain
explicitly unavailable in the Console; no upgrade or migration backfills them.

At the existing finalization writer, EP serializes an `EP_TERMINAL_EVIDENCE`
JSON artifact using UTF-8 canonical JSON (`sort_keys`, compact separators,
then exactly one newline) and registers its SHA-256. It binds submission, run,
accepted provenance, outcome, report identity, validation/quality/finalization
references, and a run-bound delivery revision. It never uses current checkout
HEAD or a mutable HTTP response as evidence.

`GET /v1/projects/{project_id}/artifacts/{terminal-evidence:<run_id>}` returns
the exact stored canonical JSON bytes only after the same project-scoped
authentication and digest verification. It never parses and reserializes the
artifact response. A missing, corrupt, or mismatched artifact is represented as
`MISSING`, `CORRUPT`, or `INCOMPLETE`; the reader never fills it in.

`COMPLETE` mutating delivery is qualified only when the checkpoint contains
verified, run-bound delivery evidence: normally a recorded merge revision, or
an explicit host-verified Managed no-op revision after the unchanged,
synchronized `main` checkout was rechecked in that transaction. EP never uses
the ambient checkout `HEAD` as a substitute, and an advisory producer-reported
SHA cannot override the host's before/after observation. A provider's `WAITING`
may be terminalized only when it has this exact host-verified no-op shape.
`VALIDATION_ONLY`, `BLOCKED`, and
`FAILED` may have valid terminal evidence with a null revision; they are not
fabricated into successful delivery.

For new Managed submissions, an optional accepted
`repository_revision_binding` constraint records a full
`requested_revision` and either an exact pin (`allowed_baseline_revision:
null`) or one explicitly permitted baseline transition. The terminal artifact
keeps that request reference, the selected execution baseline, the candidate,
and any delivery revision as separate fields. A pin mismatch is rejected before
repository synchronization or provider work; a transition is valid only when
the synchronized baseline equals the named permitted revision. This does not
alter historical artifacts or infer a missing candidate or delivery. Existing
v1.3 terminal artifacts remain their original bytes during reconciliation;
they are registered and read as historical evidence rather than rewritten as
v1.4.

Required host validation controls receive a distinct, per-child scratch
directory below the EP-managed artifact root. EP probes write/read, rename and
SQLite before launching the child and reports an unavailable or lost directory
as an unexecuted validation-environment failure, never as a passing control.

For every terminal run, the authenticated `run` object additionally contains
`execution_started_at`, `execution_completed_at`, and
`execution_duration_ms`.  These are derived only from the durable
`ep_execution_runs` claim and terminal-transition timestamps, normalized to
UTC; the duration is therefore EP's governed lifecycle duration, rather than
an unverified provider wall-clock claim.  The same fields are bound into every
new immutable terminal-evidence artifact.  If either durable timestamp is
missing, unordered, or invalid, EP exposes `evidence.status: INCOMPLETE` and
does not issue a terminal artifact.  This is intentionally fail-closed:
consumers must not infer a completed host execution from an HTTP response or
from telemetry alone.

The machine-readable response shape is
[`producer-readback-v1.2.schema.json`](../../src/engineering_platform/schemas/producer-readback-v1.2.schema.json).

## Forge admission receipt and bidirectional audit

Producer readback stays at `v1.2`; its exact root shape is not changed by this
addition.  When an authenticated HTTP submission declares Forge execution
provenance `v1.1` or `v1.2`, the successful POST response additionally carries a
versioned `receipt` object (`v1.0`).  It binds the accepted submission ID,
receipt ID and time, EP installation and application versions, Forge
application version, Forge Producer Contract version, Forge provenance
version, producer-readback version and the canonical accepted-request digest.

This receipt acknowledges admission only.  It is not a run receipt, a
delivery result or terminal execution evidence.  The same fact is stored in
the immutable `ep_forge_exchange_audit` table.  The redacted central
`http_ingress` log records distinct `forge_submission_accepted` (`FORGE_TO_EP`)
and `forge_submission_receipt_issued` (`EP_TO_FORGE`) events.  Those records
never contain the prompt, bearer credential, local checkout path or unbounded
request body.  The matching Forge client records its outbound submission and
the validated EP receipt as separate immutable exchange facts.

Forge provenance `v1.3` additionally carries a separately versioned immutable
Planning Context Envelope. It contains only the bounded Mission title, redacted
business/engineering summaries, lifecycle snapshot and opaque decision-evidence
reference/digest. EP validates its identity and canonical digest, stores it as
submission-scoped immutable evidence, and returns its exact value in readback
and terminal evidence. Earlier submissions remain unchanged and never receive
a reconstructed planning snapshot.

Terminal evidence is version `1.3`. In addition to the v1.2 identities and
timing, it binds `host_execution` v1.0: a path-free Execution Host baseline and
terminal snapshot. Where the Host observed Git safely, this records branch,
commit, inventory digests, tracked-file counts, diff counters and semantically
defined provider/validation counters. An unavailable observation is explicit;
EP never substitutes current checkout state. This evidence is captured at run
start and terminalization in CENTRAL before the immutable artifact is written.

## Historical report-analysis reconciliation

`ADVISORY_REPORT_ANALYSIS` is a redacted, derived artifact and never changes a
terminal run, producer receipt, terminal-evidence artifact or Forge context.
When a Server starts, its Lifecycle Worker asynchronously finds CENTRAL-indexed
terminal reports that predate the analysis flow and have no verified advisory
artifact.  It generates and registers one report analysis against that exact
immutable report.  A provider-unavailable result is still a bounded advisory
artifact with a safe status and may be retried through the existing Console
action.  The reconciliation never synthesizes an analysis from a prompt and
never fills historical Forge planning/context evidence that was not submitted.

Historical Forge provenance `v1.0` remains readable and admissible for
continuity, but lacks the two explicit version facts required to issue this
audit receipt. Provenance `v1.1` retains that receipt but deliberately lacks
an Action Context Envelope. Deploy EP before a Forge client that requires the
receipt: older Forge clients safely ignore the added POST field, while the new
client fails closed for an absent or mismatched receipt.

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

The v1.1 fixture pair is historical and is not a v1.2 compatibility promise.
Consumers must source any v1.2 shared fixture from this versioned producer
contract and verify the terminal artifact's exact bytes and SHA-256.
