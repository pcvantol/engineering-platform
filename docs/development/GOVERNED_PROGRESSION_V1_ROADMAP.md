# EP governed progression roadmap

Increment: `GOVERNED_PROGRESSION_AND_DELIVERY_AUTHORITY_V1`.
Scoped under [EP policy-governance roadmap](POLICY_GOVERNANCE_V1_ROADMAP.md)
and its canonical parent roadmap. This is a documentary capability DAG, not
execution authority or live policy. Implementation remains PLANNED.

Shared documentary graph:
`pcvantol/forge:docs/roadmap/governed-progression-v1.json`.
EP owns the implementation/qualification of its execution/delivery-adapter boundary.

| Node | Owner | Depends on | Completion proof |
| --- | --- | --- | --- |
| GP-0 | Four owning repositories | none | Reconciled lifecycle/cadence/delivery/authority documentation |
| GP-E | EP | GP-0 | Explicit target/operation authority checks, external request/readback correlation, no bypass, durable evidence |
| GP-Q | Forge + EP | GP-F, GP-E | Cross-product boundary tests, revision-bound approvals and restart/no-duplicate progression |
| GP-X | Forge + EP integration, target owner retains authority | GP-Q, GP-DC | Real external gate/pipeline qualification under an approved project target contract |

GP-F is Forge's progression resolver and decision reconciliation. GP-DC is the
project-owned Delivery Control Contract consumed by Forge/EP/Platform. Neither
gives EP Mission planning authority. Workspace GP-WC/GP-W and Forge Platform
GP-P remain consumer productization lanes. Existing POL/VR nodes are retained;
only the effective-policy/release seams used by this profile are prerequisites.

```text
GP-0 -> GP-E ---------------------+
GP-0 -> GP-F (Forge) --------------+-> GP-Q --+
GP-0 -> GP-DC (project contract) ------------+-> GP-X
```

The first serial dynamic Mission canary needs only the progression/approval
seams actually used by its approved Mission. It does not require general CD
adapters, production deployment, App Store release, Workspace policy UI or
universal installer implementation. Existing EP assurance, queue and SemVer
findings remain independent work; this document fixes none by declaration.

Before delivery-aware execution is claimed, prove exact authority, environment,
artifact and operation binding, retained external gates, no duplicate approvals,
current authorization, fail-closed unknown evidence and qualified recovery.
See [the owning design](../engineering/GOVERNED_PROGRESSION_AND_DELIVERY_AUTHORITY.md).
