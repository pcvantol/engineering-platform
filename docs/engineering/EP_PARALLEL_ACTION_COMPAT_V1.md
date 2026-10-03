# EP parallel Action compatibility V1 (PA-E0)

EP's transport-neutral `engineering_platform.parallel_action_compat` module
consumes Forge's protected `parallel-action-graph/v1` peer graph as UTF-8 JSON
bytes. The exact A/B/Q fixture used for qualification is copied to
`tests/fixtures/forge-parallel-action-peer-graph-v1.json` from Forge main
`bf5153fc5cb46b221e7e8bd5a869b9b857c732a7`, Git blob
`018d68ff3e719eb897363882e17da21a27fa6200`, SHA-256
`b938388fb7a031c574407b62f07cb3ed7b12d692170dd4acf81cba5a14a3ab9c`.
The producer contract is described in Forge's
`docs/architecture/PARALLEL_ACTION_FRONTIER_PRODUCER_V1.md`.

## Call boundary

```python
from engineering_platform.parallel_action_compat import (
    CompatibilityScope,
    assess_parallel_action_graph,
)

scope = CompatibilityScope(
    ep_instance_id="ep-fixture-1",
    project_id="project-fixture-1",
    repository_ids=("repository-a", "repository-b"),
)
readback = assess_parallel_action_graph(forge_graph_bytes, scope=scope)
```

`CompatibilityScope` is an EP caller's expected identity set for comparison.
PA-E0's first profile compares one EP instance and project with up to 256
repository IDs; graphs spanning instances or projects fail closed in this
profile. The scope can be widened only in a later explicitly qualified profile.
The synthetic fixture scope is not a registration, binding, repository grant or
execution admission. A future PA-E1 caller must obtain its scope from current
authoritative EP state and separately enforce grant, baseline, resource,
capacity, predecessor evidence and idempotency predicates.

The input must have exactly `contract_version`, `mission_id`,
`mission_revision` and `actions`. Each Action has exactly `action_id`, `target`
and `dependencies`; its target has `ep_instance_id`, `project_id`,
`repository_id`, `baseline_revision`. Each dependency has one
`predecessor_action_id` and `required_evidence` with `kind`, `repository_id`,
`content_digest`. Evidence kinds are `QUALIFIED_ARTIFACT` and
`REPOSITORY_REVISION`; digests are lower-case SHA-256 tokens. Unknown fields,
including producer-claimed dispatch or concurrency grants, are rejected.

The parser accepts up to 32 MiB of UTF-8 JSON so even a dense 256-Action Forge
graph in ordinary serialization fits. It rejects larger wire documents with
`INPUT_TOO_LARGE`. A pre-decode punctuation budget derived from the maximum
Action and edge counts rejects container/element floods with
`STRUCTURE_LIMIT_EXCEEDED`, before JSON can materialize an excessive invalid
tree. The parser then bounds Action count, rejects duplicate JSON keys and
identities, malformed identifiers/evidence, missing or repeated predecessors,
self edges, cycles, foreign repositories, instance/project mismatches and
evidence repositories that do not match their predecessor target. It copies
the validated producer edge set without adding Engineering dependencies. Its
`producer_snapshot_digest` fingerprints the canonical versioned graph for a
later immutable submission check. This module does not persist that snapshot.

## Stable readback

Successful results use `ep-parallel-action-compat/v1`,
`status=COMPATIBLE`, `compatible=true`, and include sorted Actions with copied
targets and producer dependencies. Every Action reports
`target_baseline_state=UNVERIFIED`. Dependent Actions report
`predecessor_evidence_state=UNVERIFIED`; roots report `NOT_REQUIRED`. The
fixture contains *required* evidence declarations, not observed predecessor
receipts, so PA-E0 cannot verify that an artifact actually exists or matches
its declared digest.

`concurrency.classification=TOPOLOGY_ONLY_UNQUALIFIED` reports pairs that
have no ancestor relation in either direction and target distinct repositories.
It reports A/B for the Forge fixture. This is static graph metadata, not a
worker, resource, lease or capacity decision. Forge's peer envelope contains
no concurrency grant field. `resource_and_capacity_verified=false`,
`execution_authority=NOT_EVALUATED`, `admission=COMPATIBILITY_ONLY` and
`dispatch_authorized=false` are always explicit on success.

Rejection returns `status=REJECTED`, `compatible=false`,
`dispatch_authorized=false`, `admission=DENIED` and a safe `error.code` plus
structural `error.path`; it returns no Action snapshot. Codes are
`INVALID_SCOPE`, `MALFORMED_INPUT`, `INPUT_TOO_LARGE`,
`STRUCTURE_LIMIT_EXCEEDED`, `UNSUPPORTED_VERSION`,
`INVALID_ENVELOPE`, `INVALID_ACTION`, `INVALID_TARGET`,
`TARGET_SCOPE_MISMATCH`, `FOREIGN_REPOSITORY`, `INVALID_DEPENDENCY`,
`MISSING_PREDECESSOR`, `INVALID_EVIDENCE`, `EVIDENCE_TARGET_MISMATCH` and
`CYCLE`. No submitted text is echoed in a rejection.

This path imports no EP execution, provider, CENTRAL or repository writer.
It performs no provider call, repository write, worker launch or admission.
PA-E1/E2/E3/EQ and real parallel execution remain separate qualifications.
