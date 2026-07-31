# Engineering Platform Qualification Registry

Engineering Platform capabilities are trusted for production use only after
deterministic local qualification evidence. `dj-engineer qualify` executes the
registry and writes local, git-ignored reports under `.djconnect/qualification/`.

| Capability | Qualification scenario | Expected behavior | Evidence | Current status |
| --- | --- | --- | --- | --- |
| Repository Initialization | Clean and dirty checkout | Reconcile or `BLOCKED` with diagnostics | local qualification report | Registered |
| Checkpoint Resume | Interrupted transaction | Resume without duplicate PR | local qualification report | Registered |
| Implementation Lifecycle, Validation Loop, Repair Loop | bounded PR and failing validation | repair remains bounded and lifecycle continues | local qualification report | Registered |
| Owner Authorization, Ready For Review, Automatic Merge | authorized green PR | only runner-controlled progression | local qualification report | Registered |
| Repository Reconciliation, Finalization, Repository Cleanup | merged and squash-merged transaction | evidence-driven reconciliation and `WORKSPACE_READY` | local qualification report | Registered |
| Engineering Memory, Progress Reporting, Engineering Reports | repeated transaction | bounded advisory memory and explainable output | local qualification report | Registered |
| Capability-aware Reviewers | documentation and product-capability objectives | relevant read-only reviewers only | local qualification report | Registered |
| Diagnostics, BLOCKED Recovery, Failure Recovery | diagnostic and transient failure | bounded explanation and resumable evidence | local qualification report | Registered |
| Long-running Transactions | queued checks | waiting never becomes completion | local qualification report | Registered |
| Remote Status Model, Private Dashboard, Repository Handoff, Remote Engineering Readiness | local projection and discovery contracts | canonical status, private dashboard and sanitized handoff remain available without authority expansion | local qualification report | Registered |
| Genesis Lifecycle | local-only greenfield repository | clean local commit checkpoint reconciles without remote, upstream or PR | local qualification report | Registered |
