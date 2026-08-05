# Engineering Platform Architecture Handbook

**Status:** Stable execution architecture

**Platform status:** Engineering Platform 1.x `FEATURE_COMPLETE`

## Purpose

Engineering Platform is the stable, producer-neutral execution platform for
DJConnect engineering work. It accepts compliant Engineering Actions from a
Human Architect, Forge or a future Producer and applies one generic execution
architecture regardless of origin.

This handbook records the frozen boundary. It introduces no runtime capability,
Execution Host behavior or Forge implementation change.

## What Engineering Platform owns

- Engineering Action execution and the execution lifecycle.
- Execution qualification, Host Preflight, Workspace Preflight and Capability
  Preflight.
- Execution Evidence, immutable Execution Receipts and Engineering Reports.
- Generic execution telemetry, dashboard and Prompt History.
- Workspace authorization, host configuration resolution and Execution Host
  evolution.
- Producer-neutral execution semantics.

## What Engineering Platform never owns

- Mission Planning.
- Business Governance or Architecture Governance.
- Mission Recommendation.
- Decision Evidence.
- Runtime Planning or Runtime Instance concepts.
- Portfolio management.
- Forge implementation.

## Producer-neutral execution model

```text
Human Architect ─┐
Forge ──────────┼──> compliant Engineering Action
Future Producer ─┘             │
                                v
                    Engineering Platform Execution Host
                                │
                                v
       qualification → preflight → lifecycle → evidence → report / receipt
```

The action has identical admission, preflight, lifecycle, evidence and terminal
semantics for every Producer. Producer identity is immutable provenance only:
it supports traceability, audit and Execution Evidence. It does not alter
scheduling, reviewer selection, execution behavior or terminal outcomes.

## Forge boundary

Forge defines Mission, planning, Runtime Prompts, Decision Evidence and Runtime
Instance concepts. Engineering Platform executes the resulting Engineering
Actions and owns their execution lifecycle, Execution Evidence, Engineering
Reports and Execution Receipts.

No Engineering Platform capability may duplicate these Forge responsibilities.
An Engineering Report or Execution Receipt is execution evidence; it is not
Forge Decision Evidence and cannot become planning state.

## 1.x freeze and future evolution

Engineering Platform 1.x is feature complete. Future innovation is expected
primarily within Forge. Engineering Platform may evolve only through explicitly
authorized platform revisions focused on:

- platform hardening, security and performance;
- operational tooling;
- generic execution capabilities;
- Execution Host evolution; and
- Forge-driven execution-contract changes.

Forge planning concepts do not automatically authorize Engineering Platform
growth. No Forge-specific planning capability belongs in Engineering Platform.

## Canonical documents

- [Platform status](../../tools/engineering/ENGINEERING_PLATFORM_STATUS.md)
- [Execution Host and Producer Contract](EXECUTION_HOST_CONTRACT.md)
- [Execution Host Operations](EXECUTION_HOST_OPERATIONS.md)
- [Engineering Report Evidence Contract](ENGINEERING_REPORTING.md)
- [Execution Receipts](EXECUTION_RECEIPTS.md)
- [Platform 1.x Completion Report](ENGINEERING_PLATFORM_1_X_COMPLETION_REPORT.md)
- [Platform Governance](../../PLATFORM_GOVERNANCE.md)
- [Platform Evolution Backlog](../../PLATFORM_EVOLUTION_BACKLOG.md)
