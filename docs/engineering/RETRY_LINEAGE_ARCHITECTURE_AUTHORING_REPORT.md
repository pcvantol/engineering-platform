# Retry Lineage Finalization — Architecture Authoring Report

## Decision

Retry lineage is a read-only projection of immutable execution evidence.

The child run carries the existing explicit `retry_of` parent reference. The
platform derives the reverse parent-to-child relation, retry chain and current
run from that relationship instead of mutating the parent terminal state.

## Consequences

- A terminal parent remains historical evidence.
- A retry is a new execution; it never becomes Resume.
- At most one child is accepted for a parent execution.
- Only the child is eligible for subsequent operational actions.

## Scope

This authoring decision is confined to Engineering Platform storage and
dashboard projections. Forge and runtime ownership are unchanged.
