# EP policy-governance roadmap

Scoped lane under [Engineering Platform Roadmap](ENGINEERING_PLATFORM_ROADMAP.md).
Owning design: [EP policy governance and effective assurance profiles](../engineering/POLICY_GOVERNANCE_AND_ASSURANCE_PROFILES.md).
Increment: `POLICY_GOVERNANCE_AND_EFFECTIVE_PROFILES_V1`.

The current deliverable is architecture documentation. The implementation nodes
below are PLANNED; no active profile, grant, run or installed artifact changes.
The cross-product documentary graph is maintained in
`pcvantol/forge:docs/roadmap/policy-governance-v1.json` (proposal [#50](https://github.com/pcvantol/forge/pull/50)).
This EP document owns the EP milestones, not peer readiness.

| Node | Owned delivery / acceptance | Dependencies | Position |
| --- | --- | --- | --- |
| POL-0 | EP policy catalogue, classifications, authority and shared logical contracts documented | none | This documentation increment |
| POL-E | EP effective validation/assurance profile services; single runwide accounting; versioned snapshots and evaluation evidence | POL-0 | PLANNED; required subset joins existing assurance work |
| POL-B | EP half of exact materialized-request -> accepted-policy binding; fail-closed unsupported/conflicting requirements | POL-E and Forge POL-F contract | PLANNED integration |
| POL-Q | EP proof for restart, revocation, current-candidate review, bounded repair and receipt/projection integrity | POL-B | PLANNED cross-product qualification |
| VR-X | Execute explicit version changes/builds/publication with immutable identity; ordinary build does not allocate versions | POL-E | PLANNED execution; Forge owns version planning |
| VR-Q | Version/release evidence roundtrip qualification | VR-X, Forge VR-F, POL-Q | PLANNED; production installer dependency |

```text
POL-0 -> POL-E ---------> POL-B -> POL-Q --+
            |                ^            |
            +-> VR-X --+     |            v
                       +----> VR-Q -> consumer composition
Forge POL-F ------------+    ^
Forge VR-F ------------------+
```

The table specifies exact dependencies; peer labels are producer requirements,
not EP authority over their implementation. EP can run standalone under a valid
local profile; no new Forge/Workspace startup dependency is introduced.

## Integration rules

The assurance fixes in #100 should align with these policy seams without adding
a generic workflow designer or every catalogue migration to that PR. Existing
#102 dependency admission work remains separate. Versioning proposals must
satisfy the native Forge/release execution boundary before adoption; this docs
increment does not implement or approve their helper/workflow changes.

The first Mission canary requires real applicable profile, review, repair and
receipt semantics, not completion of all future policy-management capabilities.
Full Workspace policy administration and installer UI are post-autonomy work.
Production universal-installer composition that depends on released artifacts
requires the release/compatibility qualification, not a matching version string.

## Documentary acceptance

Owner boundaries agree with the three companion designs; every catalogue entry
has a pinned source/form; roadmap IDs match the cross-product graph; no cycles
or implicit peer readiness; source/CI/runtime/package/permission files unchanged.
Implementation acceptance additionally requires the concrete tests in the owning
design and applicable installed evidence. Documentary completion alone cannot
unlock a run or close a live assurance gate.

## Governed progression and existing CD authority

The [GP roadmap](GOVERNED_PROGRESSION_V1_ROADMAP.md) and
[owning delivery-authority design](../engineering/GOVERNED_PROGRESSION_AND_DELIVERY_AUTHORITY.md)
refine how EP enforces protected operation requirements without owning Forge's
review cadence or a project's existing CD approval/deployment authority.
GP-E/GP-Q/GP-X are PLANNED; a Workspace decision is not a substitute for the
external target's gate. No duplicate workflow, live policy or executable DAG
is introduced. The current Action assurance and bounded repair requirements
remain intact and distinct from post-Action human review.
