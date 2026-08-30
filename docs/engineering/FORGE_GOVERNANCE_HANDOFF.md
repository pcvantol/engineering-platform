# Forge Governance Handoff Projection

Forge owns governance truth: recommendation sets, rankings, selected recommendations, Decision Evidence, Business Workspace state, approvals, Mission allocation and runtime remain Forge decisions.

With respect to Forge governance payload, Engineering Platform is only a
read-only transport, immutable-evidence and reporting consumer. A Producer
Submission Envelope may include the optional, versioned
`forge_governance_handoff` payload (`version: "1.0"`). The supplied payload is
validated on ingress, persisted unchanged with the submission in
`.engineering/engineering.db`, and linked to exactly one Run ID. It is never
refreshed when Forge later changes and is not derived from prompt text,
repository files or Forge storage.

The Engineering Report, Prompt History/read model and live execution projection consume that same stored snapshot. Legacy and HUMAN submissions remain valid and report `NOT SUPPLIED BY PRODUCER`.

`Governance State` is Forge-owned truth. A `Governance Handoff Snapshot` is the immutable Producer-supplied copy for a single execution. `Governance Handoff Completeness` is a structural reporting classification only: it reports whether the supplied snapshot contains the selected recommendation identity/title/rank/status/confidence, Decision Evidence ID, alternatives and Business Approval state. `Business Review Readiness` is shown only when Forge explicitly supplies it. Neither completeness nor reporting gives Engineering Platform approval, ranking, selection or lifecycle authority.

## Mission and execution authority

Forge Mission state and EP Execution state are separate state machines. Forge
may submit an Engineering Action and later observe immutable EP evidence, but
EP never advances, repairs or infers a Mission transition. Conversely, Forge
planning/governance changes never rewrite EP admission, lease, execution,
qualification or terminal evidence for an already submitted run.

For a Forge-originated submission, `project_id` is Forge's canonical project
identity carried into EP as a foreign identity. EP validates its registration
and scopes its operational records to it, but does not create a competing
project registry. Repository/GitHub remain authoritative for source-delivery
facts; the handoff snapshot remains a run-bound copy of Forge truth, not a
replacement for either authority.
