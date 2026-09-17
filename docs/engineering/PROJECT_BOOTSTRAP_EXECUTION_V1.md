# Project bootstrap execution V1 — Genesis, Managed and promotion

**Increment:** `PROJECT_BOOTSTRAP_AND_ARTIFACT_MANIFEST_V1`. **Owner:** Engineering Platform.
**Status:** canonical target design after protected merge; implementation and qualification of the added operation remain **PLANNED**. **NO_BUMP.**

This is the EP-owned execution/registration side of the Forge L1/L1-R bootstrap design, consuming the existing B8R identity contract, Genesis/Managed primitives, P-TRANSPORT and applicable L0 validation/proof capability. See [owning roadmap](../development/PROJECT_BOOTSTRAP_V1_ROADMAP.md) and [documentary DAG](../development/PROJECT_BOOTSTRAP_V1_DAG.json). Companion authorities: `pcvantol/forge:docs/architecture/PROJECT_BOOTSTRAP_AND_ARTIFACT_MANIFEST_V1.md` and `pcvantol/workspace:docs/PROJECT_BOOTSTRAP_V1.md`. The shared PB-01..PB-40 qualification catalogue is in Forge; EP selects its applicable cases and owns their execution evidence.

## 1. Observed capability versus target

Source basis on 2026-09-17: EP `d7fe0af363fb36f1f819fedc7a63919e01b88e66`, Forge `eee7f8dadd5b2df9e836627b43de4928a46eeb35`, Workspace `92da171c97dc3f67cc844125b08cf8a2a0e56a1e`. These are source observations, not installation claims.

[Genesis Mode](GENESIS_MODE.md) already describes local-only execution, explicit mode selection, bounded workspace placement and local commit/reconciliation. [B8R](PROJECT_IDENTITY_AND_ATTACHMENT_RUNTIME.md) already separates committed logical identity from authenticated physical attachment. Neither establishes a complete manifest-driven provisioning API, Managed birth workflow or Genesis-to-Managed promotion. Those new compositions require the nodes below; do not reopen proven primitives without a regression.

This documentary change does not alter the current exact legacy `Execution Mode: Genesis` line, Managed default, CLI behavior, CENTRAL schema, existing registered projects, #271/#272 telemetry/report work or any Mission. New API operation/field names here are contract concepts to map to versioned published schemas, not claims of implemented endpoints.

## 2. Authority and security

Forge owns the product bootstrap plan, approved artifact selection and project readiness interpretation. EP owns admission of effects, local/remote repository mutation, execution-resource leases, host/tool checks, assurance, registration, result and recovery. Workspace sends governed intents and renders owning results; neither it nor Forge shells to EP or reads EP CENTRAL. Inter-product traffic is authenticated HTTP even on the same machine. Project Agent/qualified local host facilities perform physical work; CENTRAL does not gain arbitrary filesystem access.

A bootstrap operation is not a Mission, arbitrary user shell script or second general execution engine. It is a bounded typed infrastructure operation over EP's existing lifecycle/storage/provisioning facilities. The effect inventory allows only approved directory/Git creation, selected artifact writes, explicit remote/ref/settings changes, verification and attachment. Ordinary feature engineering uses normal Mission/Action submission. No fake Mission, fake Action count or zero SHA is created to work around the absence of a first commit.

Creation authorization must work before a project-scoped execution credential exists. Use EP-owned provisioning authority bound to caller, target reservation, host/path-placement reference or remote namespace/name, explicit visibility, approved manifest/tree, allowed settings, expiry and operation. A consumer scoped to another/existing project cannot grant itself access to a new project. Reuse existing valid pairing and identity where appropriate; separately authorized credential issuance uses an established secure transfer path. No bearer in Git, CLI arguments, logs, portable manifests, Workspace clients or report exports. Missing authority/compatible service is a typed refusal, not broad bootstrap administrator fallback.

A grant to initialize a new empty resource is not permission to mutate an existing protected branch. No documentation, README, AGENTS file, generated plan, provider output or template can expand the admitted effect scope. Privilege, mode, visibility and target are revalidated at the actual effect boundary. Baseline scripts/validators are executed only under the accepted sandbox/tool/network profile, never merely because inventory read a command string.

## 3. Identity and admission inputs

The fixed `.engineering-platform/repository.json` schema remains EP-owned. Unknown fields/versions fail closed. Keep project.id, project.authority_repository_id, repository.id and role independent of paths, remote names, installation IDs and credentials. Portable declaration must not contain endpoints, host/Agent IDs, absolute paths or credentials/references. A child declaration refers to the same authority identity; Workspace display state does not become logical topology authority.

Before first commit, a project/repository name or ID can be **reserved in the bootstrap operation ledger**, but is not yet a B8R canonical registered declaration. Registration/adoption becomes canonical only after EP validates the resulting committed declaration and its authority consistency. This preserves the existing committed-declaration boundary without requiring a circular credential/project creation step. Inventory must distinguish proposed identity, reserved identity, committed declaration and registered attachment.

The versioned request includes stable operation/idempotency/correlation IDs; true actor and service identity; target journey/mode; exact manifest/approved-plan/baseline versions and hashes; expected existing HEAD/tree/settings revision or explicit ABSENT/UNBORN observation; logical identity reservation; allowed effect set; placement capability reference; remote provider immutable ID or guarded create coordinates; applicable validation/assurance/protection profiles; proof requirements; limits and recovery disposition. Preserve source actor separately from the forwarding peer.

Forge-owned portable contract paths and content are validated through the published manifest contract; EP doesn't reinterpret product goals or choose roadmap priorities. Every actual artifact path is normalized and finite before execution. Template globs/tokens are resolved to concrete approved entries, with before/after hashes, byte limits, file mode and validation mechanisms. Unknown operation/profile capabilities return UNSUPPORTED. Runtime command spelling and transport schema are qualified later; no guessed endpoint is shipped by this document.

## 4. Inventory, locks and durable execution boundary

Inventory is a bounded read operation. Resolve target, actual Git/common-worktree root and owning attachment under EP policy. Distinguish absent/empty directory, unborn Git, clean existing history, dirty staged/unstaged/untracked content, bare repository, nested repo/submodule and non-directory target. Existing Genesis direct-child workspace-root restriction remains the supported minimum; broader adoption needs explicit separately qualified placement, not a permissive path parser.

Reject host/server source targets and active/conflicting attachments. Do not crawl siblings/parents, follow untrusted symlinks or execute local hooks. Protect against path traversal, case/Unicode alias collisions, file-to-symlink replacement, nested metadata and writes into .git internals. Preserve unrelated files and all unknown-owned work. Local ignored databases and secrets are not disposable bootstrap clutter.

The mutating operation claims the existing repository/resource lease and its operation-specific ledger before effects. Recheck the target identity, expected hashes/HEAD/remote settings and current authority under the owning lock. Changed input requires refreshed inventory/plan/approval where material; do not create a new operation silently. The ledger records intent, accepted scope, step attempts, actual effects, readback, audit references and uncertain boundaries durably.

A lost acknowledgement resumes/readbacks the same accepted operation. Replaying identical bytes cannot create a second repo/ref/attachment; same idempotency key with changed content conflicts. HTTP timeout after create/push is EFFECT_UNCERTAIN until provider readback, not proof of failure or permission to issue a fresh create. Use existing bounded recovery capabilities; no direct ad hoc SQL repairs or alternate peer ingress.

## 5. Genesis complete execution contract

Genesis has no required upstream, PR or remote CI and performs no remote/GitHub API operations. Existing remote configuration, if inventoried and explicitly tolerated for local adoption, is not used, rewritten or interpreted as publication authority. Unavailable Managed infrastructure never switches to Genesis.

For a missing or empty allowed path, create only the approved directory/repository and render the common manifest-selected product/engineering foundation. For unborn Git, preserve allowed existing metadata and explicitly represent absent predecessor. For an existing local repository, require an approved adoption mapping and clean expected checkpoint; never commit pre-existing staged/untracked work as part of the operation. Use existing isolated local transaction/worktree machinery where supported.

Validate qualified baseline bytes and execute actual applicable local checks. Formal Quality/Security or Human Gates remain governed by the selected effective contract; local mode does not automatically remove them. Where no production code exists, record honest coverage scope, not a green fake denominator. Missing required tool/test proof blocks readiness. Product-specific technology templates are finite reviewed scaffolds, not unrestricted model generation.

Create and independently inspect real local commit/checkpoint(s), tree and materialized declarations/contract. Reconcile through the local Genesis outcome contract. Each result identifies execution mode GENESIS, expected/input/output identities, validation/review records, local branch/commit/tree and actual changed paths. Remote PR/merge/check fields are NOT_APPLICABLE with reason, not synthetic success. Preserve historical receipts as local evidence.

Register the committed identity through B8R's supported owning route where the selected topology requires registration. Qualification must prove declaration validity, host attachment and local operation independently; merely initializing .git isn't bootstrap completion. EP reports local execution and attachment evidence; Forge decides scoped GENESIS_READY. No paid planner invocation, new Mission or automatic successor follows from bootstrap completion.

## 6. Managed complete execution contract

Managed create requires explicit owner/account/namespace, visibility, default-branch and governing profile. Inspect immutable host resource identity before allocating effects. Existing remote content, including a single README, is adoption and uses ordinary protected delivery. A matching name alone doesn't prove ownership or an idempotent prior create.

For adoption, inventory actual host settings, repo declaration and baseline. Produce a drift/effect diff. Preserve intended existing policy unless the exact reconciliation is approved. Required checks are derived from the selected profile; an unsupported required host feature becomes incompatible/blocked, never omitted. Requested settings are not actual governance proof.

### Empty/unborn repository birth

A normal PR cannot target a nonexistent base commit. Define a narrowly qualified **repository-birth** capability rather than fabricate a PR or temporarily disable protection. The one-time authorization binds the absent resource/ref, actor, namespace/visibility, reviewed seed tree, validator evidence and intended governance. EP first validates the immutable seed locally. The host adapter creates the missing repo/ref conditionally and reads back exact immutable resource/commit/tree identities. Unexpected existing state stops execution; it never overwrites a new concurrent owner commit.

This is a distinct reviewed absent-resource creation boundary, not an exception allowing direct automated writes to existing protected main. If organization/provider policy prohibits this path, return OWNER_BASELINE_REQUIRED and require a separately authorized compliant seed. Do not weaken rulesets to make the plan succeed. Do not use Genesis as a hidden fallback to bypass Managed policy.

While the birth operation installs approved CI/security assets and establishes required checks/rules, ordinary engineering remains fenced. Run/observe real checks on the seed through the supported host path, then compare actual governance with desired state, including stale-review rules, merge strategies, required contexts, workflow permissions and ownership requirements. A missing check is not PASS; a newly configured check name without any corresponding workflow evidence is not readiness. No automatic feature execution in the temporary birth interval.

For an existing protected base, all manifest additions/changes use normal PR/review/CI/merge/finalization. Read back the actual merged tree and host governance, not only an API acknowledgement. Record protected delivery separately from first-resource birth. Register/verify committed attachment declarations and expose actual mode-specific result evidence to Forge.

## 7. Promotion: Genesis to Managed without loss of history

Promotion requires a new explicit plan and publication authority; setting origin or changing a profile flag is insufficient. Quiesce/fence affected project writers via owning operations, inventory local refs/history and resolve the approved destination. Preserve project/repository IDs, founding commits and previous local receipts. Existing compatible destination requires exact refs/ancestry; divergent or unrelated history is a conflict, never automatic merge/force-push.

Before disclosure, the owner reviews selected publishable refs, visibility, license and sensitive-data/history scan. .gitignore cannot protect secrets already committed. Necessary history rewrite or destructive cleanup is a separately authorized migration, not automatic compensation. An unknown privacy or authorization result blocks push.

EP executes guarded remote creation or compatible fast-forward transfer, then host governance/CI/attachment readback. Report exact refs/tree/commit and resulting resource ID. Forge changes the project's effective mode only after complete bound evidence and a CAS against the old Genesis policy. An incomplete promotion leaves its actual effects visible and ordinary conflicting writes fenced; no two independent active project identities and no retroactive Managed qualification.

Lost push acknowledgement is readback of the same operation. Cancellation cannot undisclose pushed content. No delete-remote rollback without separate authority and current exact ownership. Recovery may conclude that local Genesis remains the active project with a recorded incomplete promotion only after the effect state is safely reconciled. Old Genesis evidence remains available and mode-labelled.

## 8. Receipts, readiness, telemetry and failure semantics

Expose safe operation queries/events with scoped cursors, source freshness and immutable result references. A receipt includes caller/instance/project/repository/operation IDs, plan/manifest/baseline hashes, authorization reference, mode, before/after commit/tree/resource/settings identities, per-step effects, validator/assurance outcomes, attachment/readback references, outstanding work, failure owner and evidence completeness. No raw prompts, credentials or whole user history in logs/exports.

Keep technical operation outcome, host-governance qualification, physical attachment availability and Forge project readiness separate. Partial mutation with missing governance may truthfully have delivered a tree but is not MANAGED_READY. EP does not issue Forge Mission/autonomy acceptance. Report unknown versus zero/not-applicable accurately, with explicit scope and as-of time; a historical pending status is not the terminal state.

Bootstrap/provider/validation time and usage use the canonical telemetry layer but have operation-kind BOOTSTRAP or PROMOTION rather than fabricated Mission/Action/run counts. Existing projection completeness, conflict preservation and JSON/Markdown snapshot guarantees apply. A preview/read/export never triggers provisioning or an automatic follow-up Action.

Cancellation/failure preserves evidence and resources. Automatic cleanup is allowed only for explicitly admitted owned temporary artifacts with verified unchanged identity; unknown ownership is retained. New permissions, destructive compensation, visibility changes, force pushes and deleting user data require genuine authority. Logical registration rows are not casually rolled back after consumers may have observed them. Per-target results make multi-repo partial completion visible without pretending distributed atomicity.

## 9. Qualification and current non-effects

Required layers: deterministic state-machine/security/manifest fixtures; real local Git Genesis; installed own-service plus actual HTTP inter-product boundary; Managed test-host provisioning/readback under explicit test-resource authorization; promotion and crash/replay cases; mode-correct report/export evidence. Shared cases PB-01..PB-40 specify negative boundaries as well as happy paths. EP PB-EQ consumes Forge's contract and fixtures, not completed Forge PB-FQ, avoiding a qualification cycle. Workspace is not a headless predecessor.

At minimum cover all absent/unborn/existing variants, dirty/symlink/nested targets, ID conflicts, lost acknowledgements at create/ref/write/register boundaries, repeated requests, revoked/expired scope, current-project credential trying to create another project, unsupported governance, unsafe history publication, promotion after partial push, source-checkout absence and no credential/host-path leakage. Genesis tests fail on any remote call; Managed tests cannot substitute local-only success.

The new operation is not implemented by this documentation. No CLI/HTTP route, CENTRAL table, policy, grant, workflow, package version, running service, Project Agent or current Mission is changed. The existing reporting/telemetry PRs remain independent. Parent roadmap status and existing completed primitives are not reset; only the added PB nodes remain PLANNED.
