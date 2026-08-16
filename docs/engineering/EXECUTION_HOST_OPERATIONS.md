# Execution Host Operations

This is the current operator guide for the Engineering Platform Execution Host.
It consolidates the operational outcome of PRs #715–#723 without replacing
their immutable Prompt History or finalization evidence.

## Stable execution boundary

The Execution Host is generic. It executes compliant Engineering Actions with
the same lifecycle, preflight, evidence and terminal semantics for every
Producer. Human Architect, Forge and future Producer identity is retained only
as provenance; operators must not use it to infer planning authority or alter
execution behaviour.

Forge owns Mission planning, Runtime Prompts, Decision Evidence and Runtime
Instance concepts. The Execution Host owns action execution, qualification,
Execution Evidence, Engineering Reports and Execution Receipts. It does not
implement Forge, recommend Missions or perform business, architecture or
runtime planning.

## Admission and target safety

Execution Host Preflight validates host readiness before an Inbox claim.
Workspace Preflight then validates the selected target, Git safety and mode
requirements. Workspace authorization is trusted host configuration: roots,
scopes and repository allow/deny lists are evaluated fail-closed. Managed
execution remains subject to its branch, remote and upstream requirements.

## Configuration and transport

The Execution Host Configuration Resolver is the only host-specific location
resolver. It selects Runtime Prompt transport, local evidence stores, runtime
and safe host identity. The current iCloud Inbox is transport only; Forge never
receives its path or any dashboard, launchd or local-storage detail.

## Operator actions

- **Pull-request merge hand-off** is shown as a persistent, dashboard-native
  wait state with a direct GitHub link once required checks are green. Closing
  the browser does not cancel it: the watcher polls the persisted run and
  resumes it after the operator merges, even on a later day. **Abort
  execution** is the only explicit way to end this hand-off without merging;
  it records the dismissal and archives the execution as failed, without
  deleting its evidence.
- **Retry Execution** creates a new execution from a terminal `BLOCKED` or
  `FAILED` run and records immutable retry lineage.
- **Queue Recovery** is a separate explicit retry for a blocked predecessor
  when dependent Inbox work is waiting. It does not bypass queue ordering.
- **Dismiss Execution** is a confirmed acknowledgement of the current terminal
  execution. It clears operational attention only and records dismissal audit
  fields. It never deletes reports, telemetry, Prompt History or retry lineage,
  and it never resumes the queue.

Repository truth and engineering history are immutable under all three actions.
Only a new retry performs engineering work.
