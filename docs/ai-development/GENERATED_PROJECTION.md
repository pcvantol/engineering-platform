# Generated AI-development projection

Do not edit; update the local extension or canonical contracts.

- schema_version: `1`
- source_repo: `pcvantol/ai-development-contracts`
- source_commit: `6ec3b443c3ab3bdf76c626c2046d3778db570eb0`
- profile: `engineering-platform`
- extension_identity: `ENGINEERING_PLATFORM_DEVELOPMENT_EXTENSION`
- projection_digest: `6f77e0a7cf5d641c753dbcfde867f530bf3aaeb7a9da77be3380943e43f4f0c1`
- materializer_version: `1`

# AI_BOOTSTRAP_CONTRACT

Start from the target repository only. Verify branch, HEAD, base/remote state,
and worktree cleanliness before reading local bootstrap, architecture, roadmap,
extension, handoff, and validation entrypoints. Repository evidence overrides
conversation history.

# HANDOFF_CONTRACT

Expose a local handoff/status entrypoint that names repository identity, bounded
work, evidence, risks and next decision. It does not replace product
architecture or immutable history.

# PROMPT_INITIALIZATION_CONTRACT

One prompt has one bounded objective, explicit scope, repository evidence,
validation requirements and handoff/finalization needs. It cannot infer
approval, rewrite history, or expand scope.

# BRANCH_WORKTREE_CONTRACT

Use isolated branches/worktrees, preserve unrelated work, verify base and state
before mutation, and fail closed on ambiguity. Automation never writes
protected main directly.

# VALIDATION_EVIDENCE_CONTRACT

Run applicable repository validation before review and record exact results.
Absent checks are reported as absent, never fabricated.

For a governed release lifecycle, durable evidence binds one immutable operation
identity to the owning component, selected version, policy revision, protected
source revision and exact artifact digests. Qualification, publication,
cleanup-pending recovery and release completion are distinct states. A resumed
operation reuses that identity and exact bytes; a conflicting concurrent
operation or an existing identity with different bytes fails closed.

Record qualification, publication receipt and registry readback separately.
Do not represent a normal build as a version change, a successful upload as
release completion, or a local installation as publication. Product
repositories retain their own release policy, artifact registry, installation
authority and runtime semantics.

# TDE_INTEGRATION_CONTRACT

Consume published standalone TDE through an explicit local profile/evidence
mapping. Invalid supplied evidence fails closed; observe-only maturity is
explicit. TDE product architecture, implementation, evidence semantics and
release/security remain TDE-owned.

# REPOSITORY_GOVERNANCE_CONTRACT

Changes are bounded, reviewable and traceable. Local rules retain product
approvals, tests, release conditions and security controls.

# PROJECTION_CONTRACT

Commit an offline generated projection and manifest bound to contract source
commit, profile, contract list, digest, materializer version and separate local
extension. Drift checking rejects missing contracts, profile/source mismatch,
digest mismatch and manual modification.

