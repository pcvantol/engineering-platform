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

## Current runtime architecture — EP 2.0 repository-local

> **Scope notice.** This section describes the current repository-local
> Engineering Platform runtime. The EP 2.x extraction changes deployment,
> package and storage topology; it preserves the execution authority,
> admission, lifecycle, evidence and operator-merge semantics described here.

EP is an execution operating system, not a script that merely starts an AI
provider. Its current runtime accepts an Engineering Action through either a
physical Inbox route or a compliant producer route, admits one bounded run,
and carries that run through checkpoints, repository evidence, review,
operator hand-offs, finalization and reconciliation.

### Authority and evidence

```text
Forge / Human Architect / future Producer
                    │  Engineering Action + provenance
                    v
          Engineering Platform Execution Host
                    │
     admission → lifecycle → evidence → receipt / report
                    │
                    v
            operator-owned merge hand-off
```

- **Forge** owns planning intent, Missions, Engineering Actions, dependencies,
  Runtime Prompts and Decision Evidence. Workspace may initiate a submission
  through a consumer route, but it is not thereby the planning or producer
  authority.
- **EP** owns admission, execution, queueing, leases, checkpoints, execution
  telemetry, evidence, Engineering Reports, Execution Receipts, Prompt
  History, finalization and governed recovery.
- **The operator** owns pull-request merge authority. Owner authorization is a
  SHA-specific admission/merge gate where policy requires it; it is neither a
  formal review nor a merge.
- **SQLite and the persisted checkpoint** are operational truth. Status files,
  dashboard cards, telemetry, reviewer projections and AI analysis are
  read-only projections. Repository and GitHub evidence outrank every
  projection.

The dashboard can explain state and expose expressly allowed operator actions,
but it never becomes a second lifecycle, planning or repository authority.

### Common lifecycle invariants

Every run is admitted only after host, workspace and capability preflight.
Unknown required facts fail closed. The Execution Host holds one exclusive
lease, writes durable checkpoints and records evidence before advancing a
state. A restart or recovery never blindly continues from an old checkpoint:
it revalidates the checkpoint together with current repository and, where
applicable, GitHub evidence.

Reviewer selection is policy-driven and may select zero reviewers. When it
does select reviewers, they receive bounded, fresh, read-only repository facts
and produce advisory observations only. The primary runner retains lifecycle
authority; a reviewer cannot merge, authorize, mutate the repository or turn
its recommendation into final evidence.

```text
admission → preflight → initialize → reviewer selection / review where selected
          → implementation → autonomous quality control
          → evidence and delivery → finalization → reconciliation → terminal report
```

The exact delivery branch differs by execution mode; evidence, bounded scope,
lease ownership and terminal reporting do not.

### Managed execution flow

Managed is the normal flow for a configured repository with its required
remote, upstream, branch and pull-request rules.

1. EP claims the admitted Inbox or producer submission and acquires the run
   lease.
2. It validates the Managed repository profile, synchronizes the authorized
   baseline and captures fresh repository evidence.
3. It initializes the transaction, optionally conducts policy-selected
   specialist review, and performs the bounded implementation.
4. It runs autonomous quality control and applicable validation. Bounded
   repair may address failing required checks within the original objective;
   it cannot expand authority or create an unrelated branch or PR.
5. EP creates or updates the implementation pull request and observes its
   checks. A green PR moves to `WAIT_FOR_OPERATOR_MERGE`; it remains an active
   persisted hand-off, not a completed merge.
6. The operator grants any required owner authorization and merges in GitHub.
   EP observes the merge and proves the resulting commit is reachable from
   `origin/main`; it never merges on the operator's behalf.
7. EP performs finalization, which may itself create a distinct finalization
   PR. That PR has its own operator merge hand-off and must not be confused
   with the completed implementation merge.
8. After all required merge and repository evidence is proven, EP reconciles
   the checkout, writes terminal evidence and restores the workspace to its
   declared ready state.

```text
Managed repository
  → synchronize / preflight
  → implement / validate / PR checks
  → WAIT_FOR_OPERATOR_MERGE
  → operator merge proven on main
  → finalization (optional finalization PR + separate hand-off)
  → end reconciliation
  → COMPLETE / BLOCKED / FAILED
```

### Genesis execution flow

Genesis is a deliberately separate local-only lifecycle for an authorized
new or greenfield target. It never falls back to Managed.

1. The prompt declares `Execution Mode: Genesis` and one explicit,
   authorized local target repository.
2. EP validates the Genesis target profile: local Git safety, writable
   metadata, clean worktree, no conflicting lock and workspace ownership.
3. It initializes or evolves the local repository, performs reviewer selection
   and specialist review where policy selects it, then implements the bounded
   objective.
4. It performs autonomous quality control and local validation. Its generic
   quality/repair phase is not a Managed PR-control phase.
5. It writes a clean local commit checkpoint, gathers local reconciliation
   evidence, and emits its report and receipt.

Genesis neither requires nor contacts an upstream remote, `origin/main` or a
pull request. It has no operator merge hand-off. A target that is malformed,
outside authorization or indistinguishable from the Managed host repository is
blocked before provider execution starts.

```text
Genesis target
  → local preflight
  → initialize / implement / autonomous quality control
  → local commit checkpoint
  → local reconciliation
  → COMPLETE / BLOCKED / FAILED
```

### Current and target topology

| Concern | Current EP 2.0 runtime | Standalone EP 2.x target |
| --- | --- | --- |
| Product source | `djconnect` repository-local source | neutral EP package in its own repository and immutable wheel |
| Store | repository/workspace-local operational store | installation-owned SQLite store outside consumer repositories |
| Scope | current Managed checkout or Genesis target | immutable `project_id` plus registered workspace/repository |
| Display name | current workspace metadata | mutable Workspace-supplied `project_name`, used only as a label |
| Queues and Inbox | current configured route | one isolated Inbox route, FIFO queue and lease domain per project |
| Consumers | current local routes and dashboard | independently authenticated DJConnect, Forge and Workspace consumers through a versioned Local Consumer API |
| UI position | local Operations Console | same Operations Console semantics, selectable project projection |

In EP 2.x, `project_id` becomes the canonical cross-system scope. A path,
repository name or mutable display label is never an identity substitute. The
topology change must not weaken the current authority split: Forge remains the
planner, EP remains the execution authority, Workspace remains a presentation
and interaction consumer, and only repository/GitHub evidence proves delivery.

## Canonical documents

- [Platform status](../../tools/engineering/ENGINEERING_PLATFORM_STATUS.md)
- [Execution Host and Producer Contract](EXECUTION_HOST_CONTRACT.md)
- [Current lifecycle display and phase vocabulary](EXECUTION_LIFECYCLE_FLOW.md)
- [Genesis Mode](GENESIS_MODE.md)
- [Execution Host Operations](EXECUTION_HOST_OPERATIONS.md)
- [EP 2.x extraction and migration plan](../development/ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md)
- [Engineering Report Evidence Contract](ENGINEERING_REPORTING.md)
- [Execution Receipts](EXECUTION_RECEIPTS.md)
- [Platform 1.x Completion Report](ENGINEERING_PLATFORM_1_X_COMPLETION_REPORT.md)
- [Platform Governance](../../PLATFORM_GOVERNANCE.md)
- [Platform Evolution Backlog](../../PLATFORM_EVOLUTION_BACKLOG.md)
