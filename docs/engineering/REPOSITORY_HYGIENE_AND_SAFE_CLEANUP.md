# Repository observation and safe cleanup

## Decision and authority

Increment: `PROJECT_HYGIENE_AND_REPOSITORY_RECONCILIATION_V1`.
This is an EP-owned documentation/roadmap target, canonical only on owning main.
No command, schema, API, runtime policy, grant or scheduled scan is implemented
or activated by this change. The coordinating Forge
[design](https://github.com/pcvantol/forge/blob/main/docs/architecture/PROJECT_HYGIENE_AND_REPOSITORY_RECONCILIATION.md)
does not transfer EP execution authority.

Reuse [Execution Host](EXECUTION_HOST_ARCHITECTURE.md) application services,
canonical Git/GitHub providers, admission, resource exclusion, finalization and
[effective policy](POLICY_GOVERNANCE_AND_ASSURANCE_PROFILES.md). Existing own-run
cleanup remains a lifecycle concern, not a new Forge Mission or a dependency on
Forge/Workspace availability. Product capability targets are
`EP::REPOSITORY_OBSERVATION_V1` and `EP::SCOPED_REPOSITORY_CLEANUP_V1`.

Forge owns project-wide interpretation/cases/proposals; the repository provider
owns remote facts/protection; EP owns host facts and actual mutation. Workspace
is a human interface. Product/organization CD, registry retention and installer
state are not acquired through repository cleanup.

## Two bounded seams, not a second execution engine

**Observation** provides authenticated project/repository-scoped snapshots and
incremental events/readback through declared EP interfaces. It may expose ref
identities/object IDs, PR/delivery bindings, host/worktree state, leases and
ownership evidence with timestamps, freshness, collection bounds and coverage.
Missing host coverage, unknown local provenance, inaccessible provider pages,
shallow history and unavailable protection facts remain explicit unknowns.
No implicit filesystem walk across unregistered repositories or execution of
repository hooks/tests. A qualified read-only collector may use an isolated
mirror; it does not mutate the user's checkout to manufacture a clean snapshot.

**Maintenance cleanup** is a closed set of typed operations for a local ref,
remote ref or EP-owned worktree. It reuses current admission/lease/provider and
receipt infrastructure. It is not an arbitrary-shell endpoint, another queue,
a new engineering mode or a fabricated Action/run. A parent run, Mission or
Forge case may be a correlation reference; a real scoped operator command may
exist without a Mission. A necessary new contract must be versioned and qualified
before exposure; this document does not assert that a current endpoint accepts it.

EP's own finalizer uses the same safe primitives for its actual run resources.
A project-wide caller cannot claim finalizer privileges by sending a run ID.

## Logical command and receipt contract

Required command semantics (not an implemented schema): contract version,
operation ID, authenticated actor/delegation reference, project/repository and
provider identity, operation kind, exact target identities, expected ref object
IDs/worktree inventory revision, proposal and observation digests, decision and
effective-policy references, retention/recovery prerequisites and bounded scope.
Each target has its own outcome. Identifiers and paths are strictly validated;
no wildcard, recursive deletion root, caller-selected shell or caller-asserted actor.

Persist accepted command intent before external side effects. Same operation ID
and canonical payload recovers the same operation; different payload conflicts.
Recheck current authority before any outstanding side effect even on retry.
A receipt records actual actor, command/policy/proposal binding, observed before
and after state, expected and actual object IDs, provider result, retained recovery
reference, timestamps and partial/denied/unknown outcomes. No secrets or source
contents in operational logs. “Not found” does not by itself prove our deletion.
Consumer readback is project-scoped, durable and restart-safe. Maintenance
outcomes are not fabricated engineering terminal receipts or merge commits.

## Eligibility and mutation boundary

All applicable conditions must hold, not merely one:

1. The real actor has current permission for this operation, repository and
   target; read/producer/push access alone is insufficient. Remote-ref delete,
   local-ref delete and worktree removal are distinct privileges. Read-only
   reconciliation does not imply any of them.
2. Ownership is verified from EP lifecycle/provider evidence or explicit bounded
   adoption. A branch prefix, age, author display string or Forge model assertion
   is not ownership. Standalone EP operation still uses its own approved policy.
3. No conflicting active run, Action, PR, checkout user, lease or required
   recovery resource claims the target. Incomplete coverage denies unattended
   mutation, not reads. The finalizer retains its own valid run/cleanup lease
   while cleaning that run's proven-delivered resources; that coordinating lease
   is not a conflicting owner. Do not release it early to pass this check or
   treat unfinished implementation as completed cleanup work. A separate
   maintenance operation cannot borrow this exception from another run.
4. Default/main/release/protected refs and tags are excluded by this capability.
   Retention/legal holds and active release-source/installer references win over
   cleanup preferences. Changing these exclusions is separate governance.
5. Delivery/reconciliation is current for the exact branch head. A merged PR
   covers its delivered head, not later commits. Ancestry-only and semantic
   equivalence are distinguished; semantic acceptance never replaces authority.
6. Required recovery objects/bundles exist, are integrity-verified, accessible
   under controlled retention, and preserve unmerged/superseded history before
   destructive cleanup. Missing backup evidence blocks that target. Do not
   create release-triggering refs or expose private source as an archive.
7. Local worktree inspection includes tracked, untracked AND ignored content,
   attached checkouts, symlink/real-path containment and active processes. Never
   erase `.engineering`, `.git/forge-runtime`, product data-roots, credentials,
   artifacts or databases because Git's ordinary clean check ignored them.
8. The expected target snapshot is still current immediately at mutation.
   Use EP exclusion for cooperating writers and a provider's atomic expected-ref
   operation for remote races. An EP lease cannot prevent unrelated Git writers.
   If conditional deletion is unavailable, return UNSUPPORTED/RETAINED for
   unattended cleanup rather than perform check-then-unconditional-delete.

Branch recreation/name reuse, new commit, new PR/lease, permission/hold change
or changed cleanup set invalidates stale eligibility. Inspect symlinks and
containment at use time; the command cannot escape approved roots by replacing
a path between preview and execution. Unsupported filesystem/provider guarantees
are explicit capability limits, not a fallback to force-delete.

## Semantic cases, decisions and safety

Forge can supply a bounded intent-to-current-main reconciliation with code/test
references and uncertainty. EP treats it as evidence/proposal, not an instruction
to trust an LLM. Automatic deterministic own-run cleanup is allowed only under
current scoped delegation and qualification. Semantically superseded history
requires accepted disposition, recovery retention and all current safety checks;
this architecture enables no model-only deletion.

The safety decision and destructive operation are separate from routine analysis
budgets. No new provider repair allowance is created by a maintenance case;
corrective engineering work follows the existing run/lineage and shared limits.

## Recovery, partial effects and result integrity

There is no atomic transaction across GitHub refs, local Git, filesystem and
CENTRAL. Journal each admitted target operation and reconcile actual remote/local
state after lost acknowledgement, restart or crash. Finish only the still-
authorized outstanding stages. Do not repeat completed deletion, allocate a new
command to bypass denial, or silently recreate a now-reused ref as compensation.

Local cleanup can fail after remote deletion; retain a PARTIAL receipt. Repository
or provider busy is a bounded wait, not permission to remove locks. Garbage
collection, reflog expiry, archive deletion and arbitrary `git clean`/reset are
outside this capability. Archive retention is a separately governed lifecycle.

A delivered Action remains delivered if later cleanup fails. Store cleanup
warnings/evidence separately; only an actual unresolved safety/lease condition
blocks relevant later admission. An unrelated retained branch cannot block every
repository or overwrite a successful terminal delivery.

## Required qualification and rollout

First qualify observation freshness/completeness and command/readback contracts.
Then qualify own-run safe primitives, explicit project-maintenance admission,
replay/partial effects and Forge consumer interpretation. Reuse current product
services; do not require generalized Agent fleet management for a single-host proof.
Workspace UI is not required for approved CLI/API/producer operations.

Tests must cover exact delivery versus post-merge commits, squash-equivalent
history, active/unknown ownership, cross-project/forged actor, expired grant,
retained/release refs, dirty/untracked/ignored state, runtime-data preservation,
concurrent push/ref reuse, own-finalizer lease versus conflicting owner,
unsupported conditional delete, archive failure, symlink escape,
duplicate/conflicting operation IDs, interrupted effect, restart/lost
acknowledgement, partial cleanup and preserved delivery outcome.
Use isolated repositories/fixtures; no product branch deletion in ordinary CI.

The [EP scoped roadmap](../development/PROJECT_HYGIENE_V1_ROADMAP.md) sequences
these proofs. No live repo cleanup, database migration, protocol downgrade,
version bump or host mutation is authorized by this documentation increment.
