# Generated AI-development projection

Do not edit; update the local extension or canonical contracts.

- schema_version: `1`
- source_repo: `pcvantol/ai-development-contracts`
- source_commit: `f5c40e05b987721f91da86db4981cb9ad1837f41`
- profile: `engineering-platform`
- extension_identity: `ENGINEERING_PLATFORM_DEVELOPMENT_EXTENSION`
- projection_digest: `34d04daa1668d5ee1288a22d77aa143fecf4e167cb7fdc443d4082cb3ed45d77`
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

