# Action execution envelope and fit V1 — EP

**Increment:** `ADAPTIVE_ACTION_SIZING_V1`. **Owner:** Engineering Platform.
**Status:** PLANNED implementation, NO_BUMP documentation only.
[Scoped DAG](../development/action-execution-envelope-v1.json) joins the existing
SA-ROLE/RMP and effective-profile/admission contracts. Forge owns decomposition;
this is not an EP planner. Workspace consumes owner APIs, not this runtime's SQL.

Source pin: `fc250a55ad2433b0e596189fa4c8d260dac6bc77` (2026-09-12).
[Role/task policy](ROLE_TASK_MODEL_POLICY_V1.md) remains PLANNED. Existing model
configuration or validation success alone does not prove a qualified sizing API.
Companions: Forge `docs/architecture/ADAPTIVE_ACTION_SIZING_V1.md` and Workspace
`docs/ADAPTIVE_ACTION_SIZING_V1.md`. No live provider or config change is selected.

## Owning contract

EP exposes scoped, redacted, versioned observations of effective execution and
required review profiles: expected instance/project/repository, adapter/artifact,
policy/profile set and revision/digest, applicable task/effect/risk classes,
model/default selection and supported effort values, context/output accounting,
tools/sandbox, complete required rubric/context, deadlines and finite limits,
source/time/expiry plus uncertainty/enforcement status. Account details are opaque;
no secrets, raw prompts, authfile scraping, model guessing or forced API purchase.
Codex-first uses the existing supported subscription/session boundary. Availability
or speed is not model quality. Default/alias resolution and observed identity
remain separate; no immutable-model claim from a mutable vendor alias.

Logical `ExecutionEnvelopeObservation` and `ActionFitAssessment` are target
records, not new live endpoints, SQL tables or v1.2 fields. Reuse existing provider
catalogue, profile resolution, admission and evidence records. New transport fields
require versioned capability negotiation/OpenAPI/Postman and consumer qualification;
unsupported peers remain unsupported, not permissive. Forge uses HTTP-only.

A non-generating fit operation receives authorized exact proposed Action semantics,
Mission/policy references, effects, relevant scope/source context and bounded
per-phase demand. Resolve current EP profiles, not a client-selected unchecked
model name. Bind response to request digest, observation/profile revision,
instance/scope, expiry and all assumptions. Return FIT / TOO_LARGE / UNSUPPORTED /
STALE / UNDETERMINED with machine-readable limiting phase and safe reasons.
These outcomes are proposed vocabulary, not current statuses. Deterministic
inspection may read permitted repository/context metadata but must not invoke
an LLM, create a run, reserve a lease, mutate a repository or issue credentials.
Validate sizes/auth before expensive analysis; bound processing and data disclosure.

Fit is preflight, NOT a scheduling reservation, grant, accepted submission or
promise of quality. EP re-resolves and checks candidate/profile/authority at actual
admission and later phase boundaries. Preserve planned versus accepted versus
observed bindings. A known mismatch before execution returns an explicit non-
execution outcome; after acceptance use canonical run/recovery evidence. Never
pretend a timeout/lost acknowledgement proves no work. Repeat fit calls may be
idempotent but never authorize repeated execution. EP reports constraints; only
Forge proposes a new Action or dependency graph.

## Whole-cycle feasibility, not only implementer context

Check every REQUIRED implementer/Quality/Security/other mandatory role independently.
Required source, diff, artifact, rubric and safety context must fit with system/tool
instructions, possible history/tool-result growth, reserved output/reasoning and
headroom. The minimum usable phase capability can limit the Action. Never drop
criteria, truncate a diff or substitute an unqualified cheaper reviewer to get FIT.
Task context limits and accumulated Action usage are distinct; no sum of all
phase contexts treated as a single model window. Use adapter-specific accounting
so reasoning inside output quotas is not counted twice. Predicted demand is an
estimate; monitor actual context and output/timeout enforcement at each invocation.
Qualified compaction retains complete obligations and provenance, not silent loss.

Profile fallback is allowed only within already-authorized and proven feasibility
constraints, or after explicit re-evaluation. No automatic API billing fallback,
raised immutable phase timeout, enabled parallel review or relaxed sandbox.
If a strict profile identity changes, old fit cannot prove the replacement fits.
No absolute spend guarantee from unavailable CLI metering. UNKNOWN stays UNKNOWN.

Telemetry reuses SA-OBS/RMP evidence: requested/applied/observed profile, phase,
source/candidate/request digests, usage with coverage, elapsed time, repair ordinal,
context-limit/quality/infrastructure failure category and origin invocation IDs.
Missing old data is NOT_RECORDED, not backfilled. Late/replayed evidence is deduped.
Classify credential/temp-storage/transport failures separately from oversize work.
One Action may contain multiple model turns; fit must not imply one prompt = one run.

Corrective subdivision is not a budget reset. Retain failed Action/run ancestry
and enforce the existing run/continuation three-round ceiling plus stricter current
limits. A new model/run/Action identity does not create extra attempts. If a split
correction cannot carry enforceable lineage through the supported contract, deny
it pending owning contract qualification. No reviewer shopping or replacement
Mission to clear MAY_HAVE_HAPPENED. Independent new work remains distinguishable.

## Roadmap and qualification

| Node | Local predecessors | Delivery |
| --- | --- | --- |
| AS-E-OFFER | none | Qualified scoped execution/review envelope from RMP/profile services |
| AS-E-FIT | AS-E-OFFER | Non-generating fit, admission binding and actual phase recheck |
| AS-E-Q | AS-E-FIT | Installed negative/admission/evidence and profile-drift proof |

AS-E-OFFER consumes published AS-F-CONTRACT semantics; it does not require the
Forge planner to be implemented. RMP-CATALOG/RESOLVE, SA-CTX/ISO/OBS and admission
supply exact qualified subsets, not a full-SA-Q/UI dependency. Existing RMP and
SA nodes keep their status and edges. The complementary graph is acyclic and no
universal installer or live-canary prerequisite is added.

Apply AS-T02/04/05/06/08/09/10/11/12/13/14/15/16/18/19 from the Forge-owned shared
scenario registry. Test real EP profile/host/admission and adapter argument paths;
external processes can be fixtures, internal authorization or receipts cannot.
Also qualify provider-owned actual metering and complete-review behavior under
explicit live authority before claiming real binding efficiency. Preserve existing
backend/frontend coverage, security, installed/API and browser gates where relevant.
Documentation guards and mock EP acceptance are not real EP execution proof.
No product code, schema, workflow, installed runtime, PR175 or active policy changes.
