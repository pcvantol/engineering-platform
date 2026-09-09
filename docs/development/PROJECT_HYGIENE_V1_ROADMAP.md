# EP Project Hygiene V1 roadmap

Increment: `PROJECT_HYGIENE_AND_REPOSITORY_RECONCILIATION_V1`.
Owning design: [Repository observation and safe cleanup](../engineering/REPOSITORY_HYGIENE_AND_SAFE_CLEANUP.md).
Parent: [EP Roadmap](ENGINEERING_PLATFORM_ROADMAP.md).
Coordinated [DAG](https://github.com/pcvantol/forge/blob/main/docs/roadmap/project-hygiene-v1.json)
is documentary; EP retains its own implementation/admission authority.

| Node | EP deliverable | Dependencies | Status |
| --- | --- | --- | --- |
| HY-0 | Shared documented boundary, adopted in owning EP main | Owning documentation merges | DOCUMENTATION_ONLY |
| HY-E | Scoped observation/provider contracts, freshness and coverage | HY-0 | PLANNED |
| HY-C | Safe own-run primitives reused for typed project-maintenance commands, current authority/ref checks, retention and durable per-target receipts | HY-E | PLANNED |
| HY-Q | Forge/EP cross-contract qualification of actual cleanup and recovery; no Mission fiction or delivery downgrade | Forge HY-F/HY-S, EP HY-C | PLANNED |

HY-E first proves metadata observation without hooks, checkout mutation, second
queue or direct consumer SQL. HY-C then proves the separate mutation boundary:
actor/scope, no active owner, exact expected refs, protected targets, ignored-file
and runtime preservation, conditional external mutation and verified retention.
Use actual existing provider/lease/finalization services; no generalized Agent
fleet is required for a single-host implementation.

HY-Q must include ambiguous squash/supersession, stale proposal, new branch commit,
external writer, duplicate request, wrong project, denied/expired capability,
archive failure, partial remote/local effects and restart/lost-ack tests. Unsupported
atomic provider operations remain unavailable rather than getting a dangerous
fallback. Own-run cleanup remains usable without Forge or Workspace online.

All non-documentation nodes are PLANNED. This increment does not activate a
command route, grant, database schema or schedule and changes no package version.
It is not a new gate before the first serial installed canary. Existing required
EP safety checks remain mandatory; only actual relevant conflicts block later
admission. A completed delivery plus cleanup warning is not changed to FAILED.
