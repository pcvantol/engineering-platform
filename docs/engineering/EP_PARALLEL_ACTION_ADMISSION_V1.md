# EP parallel Action admission V1 (PA-E1)

PA-E1 persists one immutable Forge Action intake against the complete
`parallel-action-graph/v1` snapshot and resolves its exact predecessor
requirements before canonical queue admission. It extends the PA-E0
[compatibility contract](EP_PARALLEL_ACTION_COMPAT_V1.md). The initial profile
is one active EP project, distinct registered repository targets and
repository-only writes. It does not grant parallel worker slots or provider
dispatch; PA-E2 and PA-EQ own those qualifications.

## Producer boundary

An authenticated Forge consumer stages each Action with
`POST /v1/projects/{project_id}/parallel-action-intakes`. The JSON body is the
entire Forge graph, unchanged. Headers bind `EP-Action-ID`,
`EP-Action-Revision`, `EP-Intent-ID`, `EP-Intent-Revision`,
`EP-Correlation-ID`, `Idempotency-Key`, `EP-Write-Scope`,
`EP-Policy-Digest` and `EP-Concurrency-Profile`. The bearer credential defines
the producer identity; it cannot be overridden by the request. The first
profile accepts `repository-only`, `DIFFERENT_REPOSITORIES_V1` and the
`SUPPORTED_POLICY_DIGEST` published by
`engineering_platform.parallel_action_admission`. Staging returns an immutable
`intake_id`, the graph snapshot digest and `dispatch_authorized=false`. An
identical retry returns HTTP 200 with `replayed=true`; a new intake returns 201.
The opt-in PA-E2 digest and its bounded delivery behavior are specified in
[EP parallel Action delivery V1](EP_PARALLEL_ACTION_DELIVERY_V1.md); the
original PA-E1 digest retains project-serial behavior.

The installation owner must grant each consumer access to each target using
`grant-parallel-action-repository --consumer-id ... --project-id ...
--repository-id ... --reason ...`. Revocation uses
`revoke-parallel-action-repository` with the same scope and an audit reason.
Staging, queue admission and dispatch read the current grant. A revoked grant
produces `WAITING_SCOPE` and blocks new execution. The grant ledger survives
operational reset.
The first staging of a graph requires a current grant for every repository it
names, including Actions not yet staged individually.
The owner can revoke an existing grant while its consumer is disabled; the
grant stays revoked if that consumer is later reactivated.

The canonical submission retains its normal Forge execution provenance,
mission and Action IDs, exact idempotency key, correlation, target repository
and requested baseline revision. Its constraints also carry:

```json
{
  "parallel_action_intake": {
    "contract_version": "ep-parallel-action-intake/v1",
    "intake_id": "<staged intake ID>",
    "snapshot_digest": "sha256:<staged graph digest>",
    "action_revision": "<staged Action revision>",
    "write_scope": "repository-only",
    "policy_digest": "<supported EP policy digest>",
    "concurrency_profile": "DIFFERENT_REPOSITORIES_V1"
  }
}
```

The existing authenticated submission route rejects an absent, stripped or
mismatched intake. A graph cannot be activated after any of its Actions has
already entered the legacy queue without the exact link. Graph revisions,
Action revisions, idempotency keys and accepted submission links are append
only; operational reset records retirement tombstones before purging history.
An enrolled producer's bearer identity must match Forge `producer.id` even
before graph staging, regardless of a client supplied producer type. Once a
graph is staged, any consumer's unlinked submission for one of its Actions is
rejected at queue admission and at claim. Graph activation checks already
queued work for the same project, Mission and Action IDs across producer
aliases, so older unlinked submissions cannot enter a newly staged graph.
Operational reset retires those conflict identities for legacy submissions.
Graph staging and Forge queue admission use a serialized SQLite write order.
An accepted Action remains bound to its original graph revision. A later
revision may reuse it only when its target and predecessor contract are
identical. An unaccepted intake from an older revision becomes
`GRAPH_SUPERSEDED`; it cannot enter the queue. Failed accepted Actions may
create an operator-authorized retry against the same intake. Each retry has
an immutable parent submission link and exact accepted request digest;
it cannot impersonate a fresh Forge delivery.
An exact staging replay still returns its original intake ID after a newer
revision appears. One producer/Mission/Action has at most one accepted root
submission across revisions.
Operational reset retires accepted producer/Mission/Action root identities,
so a later revision cannot silently create another root after history is
purged.

`GET /v1/projects/{project_id}/parallel-action-intakes/{intake_id}` returns
the current producer-scoped decision. It can report `WAITING_SCOPE`,
`WAITING_BASELINE`, `WAITING_DEPENDENCY`, `GRAPH_SUPERSEDED`, `INVALID_SNAPSHOT` or
`DEPENDENCY_ELIGIBLE`, with typed blockers and the target baseline state.
Scope and baseline waits retain unsatisfied predecessor IDs in `blockers`.
The normal target baseline requires a clean local `main` at the exact declared
commit; a dirty worktree or another branch remains `WAITING_BASELINE`.
`DEPENDENCY_ELIGIBLE` is a predecessor predicate, not a claim. Under the
PA-E1 serial digest, resource and capacity fields remain `NOT_EVALUATED`;
PA-E2 overlays live resource and capacity state. `dispatch_authorized` stays
false. The queue worker and immediate claim boundary recheck the predicate so
a stale or corrupted predecessor cannot be used for a new run.
When a claimed run has moved its repository `HEAD`, continuation requires a
nonterminal checkpoint for that run naming the observed head. The accepted
execution baseline must remain an ancestor; scope and predecessor proof are
still rechecked.
An operator retry may pin a protected `origin/main` revision ahead of a clean
local `main`. Admission verifies that exact remote pin and ancestry, reports
`PENDING_HOST_SYNC`, and leaves checkout synchronization to the execution host
under its lease.

## Exact predecessor evidence

A completed predecessor is linked only to its own canonical EP submission,
successful terminal readback v1.3, run and terminal artifact. The finalizer
records the link, and intake/queue reads can reconcile a missing link after
restart. Each check compares the stored intake to the accepted request and
rechecks current canonical evidence. A `REPOSITORY_REVISION` edge requires the
declared predecessor repository and digest of its qualified terminal
revision. A `QUALIFIED_ARTIFACT` edge additionally requires the exact package
bytes, artifact record and a separate canonical qualification manifest bound
to that package, project, repository, submission, run and terminal receipt.
The manifest must record passing package integrity and installed readback
controls. The manifest alone is insufficient: the installation owner records
an immutable qualification receipt with
`qualify-parallel-action-artifact --intake-id ... --artifact-id ...
--qualification-artifact-id ... --installed-relative-path ... --reason ...`.
EP reads that file below the bound target repository, hashes its bytes and
requires them to equal the exact package digest. The receipt binds the owner,
package, manifest and observed location. Every dependency readback rechecks
the installed bytes, so removal or drift blocks the edge. Source merge or
terminal completion alone does not satisfy an artifact edge.
If EP's sanctioned terminal-evidence reconciliation replaces a projection,
the immutable outcome retains its original receipt while the verified
reconciliation operation binds the active replacement. Unsanctioned receipt
changes remain blocked.
An artifact qualification created after reconciliation must name the active
replacement receipt. A qualification bound to the old receipt stops satisfying
that edge until a new owner-observed qualification is recorded.

The package and manifest records must be published by a separately qualified
artifact producer. PA-E1 verifies them and requires a separate owner-observed
installed-byte receipt; it does not publish package bytes or a manifest. The
synthetic A/B/Q tests supply those records and an owner receipt to exercise
both blocked and eligible predicates. Live A/B/Q execution overlap and
artifact publication remain later qualification work.
