# EP governed continuation evidence v1

**Contract:** `ep-governed-continuation-evidence/v1`
**Scope:** bounded GP-E producer conformance for Forge governed continuation

Engineering Platform projects this contract inside every represented Action in
the authenticated `ep-parallel-action-collection/v1` response. It is a
read-only join over existing immutable intake, submission, run, terminal
receipt, authority and lease records. Reading it does not stage, admit,
dispatch, retry or derive an Action.

## Public versioned join

Forge consumes three authenticated, project-scoped EP routes:

1. `GET /v1/projects/{project}/parallel-action-intakes/{intake}/collection`
   supplies the Action identity, operation, policy, terminal receipt and EP
   effect-lease projection. The response `EP-Server-Instance` header must equal the
   nested continuation identity.
2. `GET /v1/projects/{project}/artifacts/{terminal_artifact_id}` supplies the
   immutable terminal-evidence 1.4 document with accepted request digest,
   Mission/Action correlation, requested and execution baselines, candidate,
   final revision, assurance and timing.
3. `GET /v1/projects/{project}/repositories/{repository}/consumer-authority`
   supplies `ep-repository-consumer-authority/v1` with the current instance,
   consumer, grant and binding pins. A retained terminal receipt does not
   replace this current-authority check.

The continuation projection contains:

- exact instance, project, Mission and the represented intake's originating
  Mission revision; the collection's top-level Mission revision identifies the
  selected graph and can be newer for a compatible accepted predecessor;
- Action, Action revision, intake and correlation identities;
- canonical submission, run and idempotency identities;
- the bound EP policy digest and concurrency profile;
- the effective immutable terminal artifact identity, digest and repository
  revision, including a validated reconciliation replacement when present;
- current repository-grant availability;
- redacted repository, provider-capacity, dispatch and uncertain-effect lease
  state.

`terminal_release_confirmed=true` requires verified terminal evidence for the
exact run and no held EP effect lease. A completed run can therefore release
its repository mutation and provider-capacity leases while Forge holds an
after-Action review fence. Active or uncertain EP effects remain `HELD`, so a
Forge pause never acts as implicit cancellation.

## Authority boundary

The projection always states `successor.selection_authority=FORGE`,
`successor.release_authorized=false` and `dispatch_authorized=false`. EP never
interprets terminal evidence as permission to plan or start a successor.
Forge must combine the receipt with current EP authority and its own exact
DecisionRequirement before continuation.

Repository-grant revocation changes the projection to `UNAVAILABLE` without
rewriting retained terminal evidence. Missing or invalid consumer credentials
remain HTTP 401; a foreign consumer cannot discover another producer's intake.
Duplicate reads and process restarts return the same immutable identity and
receipt. An uncertain provider effect remains held until the existing EP
recovery contract resolves it.

This bounded contract does not implement GP-F, a Mission planner, review
policy, an approval store, external delivery, publication or Mission
acceptance. Full GP-E/GP-Q/GP-X completion remains governed by their owning
roadmaps and qualification evidence.
