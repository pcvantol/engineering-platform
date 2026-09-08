# EP governed progression and external delivery authority

Increment: `GOVERNED_PROGRESSION_AND_DELIVERY_AUTHORITY_V1`.
Documentation and roadmap only. Canonical target on owning main; otherwise
PENDING_PR. No runtime implementation, deployment, policy activation or grant
is created by this document.

## Authority

Forge owns Mission progression and review cadence. EP owns Action admission,
execution, validation/assurance, operational repair, finalization and receipts.
A project's existing CD/release system retains its approval, deployment,
publication, credentials and rollback authority. Workspace presents decisions;
presentation does not transfer authority. Forge Platform composes qualified
artifacts and invokes supported product/target interfaces; it does not replace
an organization's delivery control plane.

Shared semantics:
`pcvantol/forge:docs/architecture/GOVERNED_PROGRESSION_AND_DELIVERY_AUTHORITY.md`.
This elaborates the existing [EP policy architecture](POLICY_GOVERNANCE_AND_ASSURANCE_PROFILES.md)
and narrows generic release/deployment wording to the actually assigned authority.

## Three distinct gates

1. Pre-Mission Business and Architecture approvals remain Forge lifecycle
   invariants. They are not EP's post-implementation quality step.
2. An after-Action human review is Forge progression policy. EP can complete
   its bounded Action while Forge waits before releasing successor work.
3. A before-publish/install/deploy gate protects a specific side effect and
   belongs to the declared target authority. A post-Action review cannot replace
   a required pre-production approval.

EP must not autonomously launch a new Forge Action while Forge progression is
paused. Conversely, a Forge pause does not cancel an already running Action or
external pipeline. In-flight handling uses the owning execution/cancellation
contract and preserves evidence. A completed EP run need not retain its repository
mutation lease merely because Forge's next Action is waiting for human review.

## Environment and Delivery Control Contract

Each project-owned target declaration distinguishes stable target identity,
environment class (TST/ACC/PROD/custom), account/resource/audience, product/component,
artifact input, pipeline/entrypoint identity and configuration revision, approval
authority, trigger authority, deployment/publication authority, permitted operations,
evidence/readback and cancellation/rollback boundaries. Labels and prompt text
cannot turn a production endpoint into TST. Actual target identity must agree
with the approved declaration and the authoritative external configuration.

TST and ACC may allow automatic progression; PROD or App Store submission may
require a human gate under the applicable target policy. These are selectable
profiles, not a universal requirement to add a second human approval to every
production pipeline. A project may impose stricter TST/ACC rules. Upload,
submission for review, approval and public release are distinct operations;
permission for one does not authorize the others.

## External delivery integration

Use `OBSERVE_ONLY` by default for existing external delivery. An approved
`REQUEST_AND_WAIT` mode permits a bounded request to an existing entrypoint.
Direct trigger permission is separately scoped within that mode, not another
mode conferring deployment or approval permission. Existing external gates
cannot be bypassed by EP's provider, repository credentials or Workspace approval.

One logical requirement has one authoritative satisfaction binding. Mapping to
an external gate is not a satisfied decision: pending external approval stays
pending. Verify semantic coverage, not just equal labels. Only validated evidence
for the exact candidate/target can satisfy that requirement. Two gates are
legitimate for explicitly different obligations, such as business release and
SRE deployment approval. Missing mappings fail closed; no local fallback gate.

EP may execute a release/deployment operation only where the project explicitly
assigns EP that operation and supplies scoped authority. This does not confer
control over a target that remains CD-owned. Never give general cloud, store or
production credentials to Forge/Workspace just to trigger a pipeline.

An authorized pipeline request may occur while its external approval is still
pending IF that pipeline demonstrably fences the protected side effect until
approval. Verify the authority/enforcement binding before requesting it. This
avoids requiring approval before the request that creates the external gate.
If the trigger itself causes the protected side effect, approval must precede
that trigger. A request acknowledgment never proves approval or deployment.

## Side-effect boundary and evidence

At each protected operation the owning executor verifies current actor/grant
scope, expiry/revocation, exact policy/decision references, artifact bytes/digest,
source-to-delivery provenance, target/environment and operation identity.
Candidate, target, pipeline or material policy changes invalidate old evidence
according to declared rules. Pinned policy does not override current revocation.
Avoid network calls inside long database write transactions; persist operation
intent and use idempotent dispatch plus authoritative readback.

Receipts correlate Mission/Action where supplied, release/operation ID,
product/component, artifact identity/digest, source revision, target/environment,
pipeline/run identity, applied gate/decision references, outcome and the actual
deployed/published identity. Acceptance, approval, execution, verification and
public release are separate facts. External evidence is mapped through qualified
adapters, not fabricated into an EP execution artifact. Redact secrets and
sensitive operator details.

Duplicate callbacks, lost acknowledgments and restarts reconcile the same
operation. Verify authenticated origin and exact identity and handle out-of-order
events. Successful but wrong-run/wrong-target evidence cannot unlock progression.
If a pipeline cannot supply sufficient verifiable evidence, report unsupported
or unverified integration. An outage is not a license to deploy directly.
Already proven delivery stays true when later cleanup or rollback fails; report
the subsequent event separately.

## Engineering governance compatibility

The existing quality/security step, read-only reviewers, actual candidate
qualification and shared maximum of three corrective rounds remain unchanged.
Human review cadence cannot remove those controls or replenish consumed budget.
A project/Mission policy may impose stricter limits but cannot grant execution,
merge or publication rights. Review after each Action is distinct from GitHub
PR review and from an external CD environment approval. EP-only operation stays
supported under its own approved local policy; optional Forge/Workspace downtime
does not require a second authority or bypassing a mandatory gate.

## Future qualification

Required scenarios: automatic TST/ACC and externally gated PROD; prototype
Mission-end review versus production after-Action review; no duplicate approval;
explicit independent business/SRE approvals; wrong target/environment/artifact;
stale approval after mutation; revoked trigger grant; untrusted callback; lost
trigger response/restart without duplicate deployment; rejected/deferred external
gate; unavailable authority; request-before-external-approval without protected
side effects; store submission distinct from public release; no direct credential
or SQL bypass; preserved repair consumption and history.

Implementation is PLANNED. The owning lane is GP-E in the
[governed progression roadmap](../development/GOVERNED_PROGRESSION_V1_ROADMAP.md).
This architecture does not close open #100 findings or insert full CD integration
ahead of the first serial Forge Mission canary.
