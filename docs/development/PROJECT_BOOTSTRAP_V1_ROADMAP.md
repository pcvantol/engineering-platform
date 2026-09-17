# Project bootstrap V1 — EP owning roadmap

**Owner:** Engineering Platform. **All added implementation/qualification nodes: PLANNED. NO_BUMP.**
This scoped delivery roadmap extends the [canonical EP roadmap](ENGINEERING_PLATFORM_ROADMAP.md) at the existing B8R attachment, Genesis/Managed execution, P-TRANSPORT and L0 contract/proof seams. It is not a replacement execution engine or a claim those foundations are absent. Forge L1/L1-R is the consumer lane; Workspace onboarding is a later client, not an EP prerequisite.

Read the [EP execution design](../engineering/PROJECT_BOOTSTRAP_EXECUTION_V1.md) and [documentary DAG](PROJECT_BOOTSTRAP_V1_DAG.json). Cross-product semantic contract and PB-01..PB-40 catalogue: `pcvantol/forge:docs/architecture/PROJECT_BOOTSTRAP_AND_ARTIFACT_MANIFEST_V1.md` and `PROJECT_BOOTSTRAP_QUALIFICATION_V1.md`.

| Node | Depends on | Deliverable and closure |
| --- | --- | --- |
| PB-E0 | none | Versioned typed operation/receipt/capability contract, B8R-compatible identity reservations and authority mapping |
| PB-E1 | PB-E0 | Bounded inventory, authorization, lease/ledger and actual HTTP admission/readback over owning services; no new side channel |
| PB-E2 | PB-E1 | Genesis absent/unborn/existing local bootstrap with trusted validation, local commit and committed attachment evidence |
| PB-E3 | PB-E1 | Managed absent-resource birth plus existing-repo adoption, protected delivery, governance/CI readback |
| PB-E4 | PB-E2, PB-E3 | Quiesced, publication-authorized Genesis-to-Managed promotion preserving IDs and Git history |
| PB-E5 | PB-E1 | Scoped reporting/export/telemetry, cancellation and restart-safe effect reconciliation, privacy and no implicit Mission |
| PB-EQ | PB-E2, PB-E3, PB-E4, PB-E5 | Installed execution qualification across both modes/promotion and all applicable PB cases; real remote-provider proof distinct from fixtures |

PB-E0/PB-F0 align published immutable contract fixtures. PB-E1 consumes the qualified P-TRANSPORT/authority subset, not the whole future installer/Agent-fleet programme. PB-EQ may use Forge-produced contract fixtures without requiring final Forge PB-FQ. Forge PB-F3 consumes PB-E1, PB-F4/PB-F5 consume mode results, and PB-FQ consumes PB-EQ. There is no cross-product cycle and no dependency on Workspace UI.

Genesis and Managed implementation can proceed independently after E1. Promotion joins them. Qualification of the new operation must not alter the current Mission-3 acceptance or install new runtimes during a running test. Ordinary feature delivery and active #271/#272 changes are outside this documentation increment.
