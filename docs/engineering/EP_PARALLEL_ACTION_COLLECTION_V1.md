# EP parallel Action collection V1 (PA-E4)

**Status:** PA-E4 SOURCE_FIXED. **Owner:** Engineering Platform. **Version:** NO_BUMP.

The authenticated producer may read
`GET /v1/projects/{project_id}/parallel-action-intakes/{intake_id}/collection`.
The selected immutable graph fixes the Action population and revision. The
consumer credential must own the intake in that project; other producers see
404, unauthenticated callers see 401. The response is one
`telemetry-export@1.1` envelope with
`ep-parallel-action-collection/v1` data and a retained snapshot ID. Every
Action in the graph appears, including Actions that have not been staged.

An Action accepted under an earlier graph revision is included when EP's
existing exact accepted-predecessor comparison proves its target and dependency
contract identical to the selected graph. Its original intake and source
graph ID remain visible. Ambiguous or mismatched accepted identities fail
closed. Root and operator retry submissions are listed as attempts within
their own Action; sibling Actions never become one execution chain. Accepted
request, target, run and terminal outcome identities are checked against the
stored project, producer, Mission, Action and repository. A COMPLETE delivery
is distinguished from a verified canonical terminal artifact and from Forge
Mission acceptance. The current admission decision is evaluated for the
selected staged or queued Action. Other pending Actions carry
`NOT_EVALUATED_IN_COLLECTION` and can be selected through their own intake
for a bounded live decision. All durable attempts and run evidence remain
included. Current PA-E3 recovery and cancellation holds are projected for
every run and Action. No collection decision grants dispatch authority.

Canonical `provider_usage` and `execution_timing` reducers supply full run
detail. EP sums observed usage over distinct run IDs once and keeps partial or
conflicting metric coverage. It labels this as EP-observed usage only; Forge
planning and Mission acceptance are outside the sum. Completed UTC provider
intervals with a compatible observed process duration supply busy union and
time with at least two distinct Actions overlapping. Conflicting wall and
process clocks are excluded and reported as conflicts. Run intervals supply
the execution envelope, busy union and
Action elapsed spans. Open or invalid intervals lower coverage; two RUNNING
labels alone never assert provider overlap. Existing measured queue wait is
reported when available. Historical dependency, resource and capacity wait
durations are explicitly unavailable because wait transitions were not
persisted; the selected Action's current typed wait state remains visible.
No speedup or savings estimate is manufactured.

`GET .../collection/export?format=json|markdown&snapshot_id=sha256:...`
downloads the same retained, complete, privacy-safe snapshot. The ID is bound
to project, producer, intake and locale; a missing, expired or mismatched ID
returns 409 rather than silently rebuilding a different view. Both formats
carry the same selected Action and run population, usage coverage, timing,
attempts, provider invocations and phase spans. The Markdown view includes
the full safe collection data as an indented JSON block after its tables, so
nested evidence remains available without truncation. Downloads add only their own
timestamp. The existing ten-minute bounded snapshot cache and size rejection
apply. Construction uses one consistent read-only CENTRAL transaction and the
existing batch telemetry loaders; serializers do no database reads or metric
recalculation. Only the selected Action performs live baseline/resource probes,
so a 256-Action graph cannot multiply those probes within one response. Reads
make no provider call or repository mutation.

This slice does not perform PA-EQ's installed Forge-to-EP concurrency matrix,
Forge PA-F3 consumer acceptance, live provider work, production CENTRAL,
Mission-3, reset/T0 or an EP release.
