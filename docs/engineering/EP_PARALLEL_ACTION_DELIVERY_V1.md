# EP parallel Action delivery V1 (PA-E2)

**Status:** SOURCE_FIXED. **Owner:** Engineering Platform. **Version:** NO_BUMP.

PA-E2 adds one opt-in repository-only profile to the PA-E1 intake. A producer
selects it with policy digest
`sha256:1c41f77be469e6a6b85ee2873d78659c71ecc90bea0a4f72b907721ac38a904c`.
The earlier PA-E1 digest retains project-serial behavior. The new digest is
bound to `DIFFERENT_REPOSITORIES_V1`, `BOUNDED_PARALLEL_PA_E2`,
`provider_child_limit=1`, and `write_scope=repository-only`; other policy
documents fail admission.

The installed worker first rechecks PA-E1 predecessor evidence. It then
evaluates repository and capacity state for each ready Action in submission
order. A dependency wait never reserves a slot. A held repository or GitHub
origin reports `WAITING_RESOURCE`; exhausted provider capacity reports
`WAITING_CAPACITY`. Readback is observational: the dispatcher repeats every
check in its atomic CENTRAL claim before assigning a run and a provider slot.
It may report `DEPENDENCY_ELIGIBLE` with both resource and capacity available,
but it is never itself an execution grant.

The first profile allows at most two provider invocations across this
installation, counting active legacy delivery. A repository reservation is
exclusive on its resolved Git common directory and normalized GitHub fetch
and push origins, so alternate worktrees and separately cloned aliases wait. Unknown
origins and unavailable repository bindings fail closed. Legacy active work
retains its project-wide serial gate; the new profile cannot overlap it in
the same project or on the same repository resource.

Reservations live in CENTRAL's `ep_execution_leases` and are acquired under
the same `BEGIN IMMEDIATE` transaction as run creation. An unreleased
reservation never expires merely because a heartbeat stopped. Provider slots
are released when a delivery stops or reaches a terminal checkpoint;
repository reservations are released on COMPLETE or explicit operator
dismissal/retry after BLOCKED/FAILED. Duplicate claims retain the same run
and cannot acquire a second slot or bypass a held resource. Ready work behind
a resource or capacity wait remains eligible for the next worker pass.

PA-E2 deliveries run in separate operating-system processes because the
preserved historical runner uses process-wide storage environment settings.
The managed Codex CLI invocation disables multi-agent spawning for this
profile and uses a repository workspace-write sandbox with no additional
write roots or writable temporary directories. Network access remains enabled
for the managed GitHub delivery. These overrides enforce one provider child
and repository-only writes per
delivery. Other profiles retain
their prior invocation behavior. The PA-E2 process runs the same dispatcher,
preflight, execution lease, runner, and finalization path as a legacy delivery.
The sandbox overrides use the documented
[Codex configuration keys](https://learn.chatgpt.com/docs/config-file/config-reference).

This source slice qualifies two independent repositories on one host, with
distinct run and provider process identities and overlapping provider
intervals. PA-E3 owns crash and cancellation recovery beyond the fail-closed
reservation, PA-E4 owns complete multi-active telemetry/export evidence, and
PA-EQ owns the installed Forge-to-EP matrix.
