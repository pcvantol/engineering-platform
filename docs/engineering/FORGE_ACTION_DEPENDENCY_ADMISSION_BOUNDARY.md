# Forge Action dependency and EP admission boundary

**Status:** target Engineering Platform contract boundary; canonical when merged. Implementation and qualification remain separately governed.

## Purpose

Engineering Platform must support a producer-supplied hard Engineering Action dependency graph without becoming the planner that invents that graph.

For Forge-produced work:

- Forge owns which Actions exist;
- Forge owns each Action's target repository/write scope;
- Forge owns hard logical `depends_on` decisions and predecessor evidence requirements;
- EP durably persists the immutable submitted dependency snapshot;
- EP enforces dependency eligibility during admission;
- EP independently enforces repository/resource exclusion, Agent capability and execution capacity.

`FORGE_OWNS_ENGINEERING_DEPENDENCY_REASONING = TRUE`
`EP_OWNS_DEPENDENCY_ADMISSION_ENFORCEMENT = TRUE`
`EP_DOES_NOT_INVENT_FORGE_DEPENDENCIES = TRUE`

## Three independent gates

An Action can wait for three fundamentally different reasons:

1. **Hard logical dependency.** The producer-supplied predecessor evidence is not yet satisfied.
2. **Execution resource/lease.** The plan permits execution, but the required repository/resource is exclusively leased or otherwise unsafe.
3. **Qualified execution capacity.** The plan and resource state permit execution, but no authorized Agent/provider capacity can accept it yet.

These states must remain distinguishable in CENTRAL evidence and projections. Priority must never bypass any of them.

```text
producer Action is logically eligible?
        |
        +-- no --> WAITING_DEPENDENCY
        |
        v
required repository/resource available?
        |
        +-- no --> WAITING_RESOURCE / WAITING_LEASE
        |
        v
qualified Agent/provider capacity available?
        |
        +-- no --> WAITING_CAPACITY
        |
        v
admit / place / execute
```

## Immutable dependency snapshot

The submitted Action contract must be able to bind at least:

```text
Action identity/revision
Mission/Intent provenance supplied by producer
target project/repository
bounded write scope
depends_on predecessor Action identities
required predecessor outcome
dependency evidence requirements/references
producer correlation/idempotency identity
```

EP validates and stores the dependency snapshot with the canonical submission/run lineage. A producer retry or transport recovery must not silently change predecessor requirements under the same idempotency identity.

EP may reject malformed or contradictory dependency input. That validation is not planning.

## Evidence-gated predecessor satisfaction

A predecessor edge can require more than a generic COMPLETE flag. The Action contract may require concrete predecessor evidence such as:

- delivered repository revision;
- qualified artifact identity;
- artifact SHA-256;
- source revision;
- receipt/report identity;
- protocol/schema version evidence;
- qualification/provenance reference.

The successor becomes dependency-eligible only when the required canonical predecessor evidence is present and verifies against the submitted requirement.

A source merge alone is therefore not equivalent to publication of a qualified installable artifact when the dependency explicitly requires the artifact.

## Cross-repository dependencies

A hard dependency may cross repositories and products. Project or repository membership alone does not create a dependency.

Example:

```text
EP-A5: publish qualified EP Server + Project Agent artifacts
  evidence = artifact identities + SHA-256 + source revisions + qualification refs

FP-A3: finalize Forge Platform component manifest
  target_repository = forge-platform
  depends_on = [EP-A5, FP-A2-installer-support]
```

EP must keep `FP-A3` in `WAITING_DEPENDENCY` until the `EP-A5` evidence required by the submitted edge is satisfied, even if a Forge Platform Agent/lease is otherwise available.

## Parallel execution

When Forge supplies two Actions with no dependency between them, EP may admit them independently if resource and capacity rules allow. Different repositories are independently lockable and are the primary safe first case for mutating parallel execution.

Forge deciding that two Actions are independent does not force EP to run them concurrently. EP retains execution-safety and capacity authority.

Conversely, EP finding spare capacity must never override a Forge-supplied hard dependency.

## Relationship to Agent local scheduling

CENTRAL decides logical eligibility and placement before an Agent owns delivery. An Agent may schedule already accepted independent work within its local resource policy, but may not execute an Action that CENTRAL has not admitted or bypass a hard predecessor.

Agent-local retries, validation repair and branch reconciliation remain execution attempts for the same admitted Action; they do not create new Forge dependency nodes.

## Dynamic Forge replanning

EP does not need the whole future Living Mission Graph. It receives immutable materialized Action snapshots. Forge may reconcile completed EP evidence and then derive new successor Actions or changed future dependencies.

EP therefore sees a durable stream of immutable submitted Actions rather than a mutable planning database shared with Forge.

```text
Forge mutable future plan
  -> immutable Action A submission
  -> EP A terminal evidence
  -> Forge replan
  -> immutable Action B/B2/C submission(s)
```

No cross-product direct SQL or shared graph store is introduced.

## Qualification

The dependency-admission contract requires deterministic and end-to-end evidence for:

- hard predecessor blocks successor despite free capacity;
- dependency becomes eligible only after required terminal evidence exists;
- wrong predecessor/run/project/repository evidence is rejected;
- artifact requirement is not satisfied by source merge alone;
- changed dependency set under one idempotency identity is rejected;
- independent repository Actions can be simultaneously eligible;
- resource/lease wait remains distinct from dependency wait;
- capacity wait remains distinct from dependency/resource wait;
- restart/recovery preserves dependency identity and eligibility;
- priority cannot bypass hard dependencies;
- Agent local execution cannot self-admit blocked work.

The first Forge dynamic inner-Mission canary may remain serial and does not require this full multi-repository parallel qualification. The cross-repository Forge canary requires this boundary together with EP's qualified multi-execution, repository lease and Agent/provider capacity capabilities.
