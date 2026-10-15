# Workspace execution progress read V1

Owner Engineering Platform, assignment `L2-WORKSPACE-EXECUTION-PROGRESS-READ-V1-20261015`,
JOIN `L2-L4-CENTRAL-MISSION-EA-PROGRESS-V1-20261015`.
Implementation in progress; not yet a qualified consumer pin.
Product 2.3.122, CENTRAL76, closed contract1.0. R34, HOB-EP01 and the local
HOB-EP02 inspection remain closed; L3-R55 retains its qualified 2.3.121 inspection pin.

## Routes and authority

- `GET /v1/execution-progress/projects/{project_id}?mission_id={mission_id}`
- `GET /v1/execution-progress/projects/{project_id}/runs/{run_id}?mission_id={mission_id}`

Both support optional `expected_snapshot=<64hex>`; a different current snapshot
returns409. There is no pagination/archive or mutation endpoint. Both use the
same source generation for the selected known submissions; exact detail narrows
the records without changing the snapshot identity.

Required headers: `Authorization: Bearer …`, `EP-Progress-Contract: 1.0`,
`EP-Instance-ID`, `EP-Consumer-ID`, `Workspace-Instance-ID`, `EP-Audience: workspace`.
All scope values must match trusted owner issuance. Unknown/duplicate transport
fields, bodies and transfer encoding are rejected. No free actor header or
list-all/admin fallback. Workspace Server must additionally check its user and
context authority; credentials do not belong in the native Client.

Normal installation owner uses `engineering-platform-progress issue --data-root
PRIVATE_ROOT --expected-instance-id ACTUAL_INSTANCE --config PRIVATE_JSON`.
Private0600 JSON contains `grant_id`, `consumer_id`, `workspace_instance_id`,
`selections`, `fields`, `operation_id`, `ttl_seconds`. Fields are exactly
`identity`, `progress`, `result`, `history`. Each of at most16 selections names
one existing `project_id`, exact `authority_repository_id`, and finite nonempty
`repository_ids`, `mission_ids`, `producer_ids` sets (maximum32 each).
TTL1..86400s, default supplied explicitly by owner; no indefinite grant.
Maximum128 retained progress grants; no silent credential deletion or rotation. Store
only the returned one-time plaintext bearer through the private Server channel.
`status --grant-id ID` omits the credential; `revoke --grant-id ID --operation-id
ID` is permanent. All owner commands use the same root/expected-instance flags.
Missing initialized CENTRAL is not created. Owner migration75→76 is separate.

The verifier table/domain is separate from discoveryREAD, INSPECT_READ,
installation pairing, submission and Console credentials; none is fallback.
This credential cannot submit, retry, cancel, approve, inspect, register or issue
grants. Read publication rechecks current scope, expiry/revoke, active project,
authority repository and physical source generation. No lock spans serialization
or socket waiting. Delivered bytes remain historical and cannot be recalled.

Local `read --credential-stdin --config PRIVATE_JSON` parity uses the same
reader with explicit `headers`, `query`, `project_id`, optional `run_id`.
Consumers use HTTP, never SQL or a checkout. No provider/Git/model/network poll,
translation, missing report generation or business write occurs on these GETs.

## Field and phase table

| Field | Owning CENTRAL source | Meaning / unknown or legacy |
|---|---|---|
| instance/project/authority/repository | installation metadata, B8R project/repository registrations, explicit grant | Actual existing instance and current logical authority; checkout attachment is not a history-read requirement. Disabled/changed authority denies. |
| producer/Mission/revision/EA/correlation | accepted `ep_submissions.constraints.forge_execution` and canonical admitted columns | Validated exact provenance, never prompt/title matching. Unsupported/unlinked legacy counted explicitly, never silently joined as a modern EA. Conflicting modern evidence409. |
| submission/run | `ep_submissions`, `ep_parity_lifecycle_dispatches`, `ep_execution_runs` | Queue has null run; selected run must belong to exact project/repository and accepted submission. |
| attempt/root/retryparent | `execution_submission_attempt_links`, `execution_submission_attempts`, root submission and qualification lineage | Missing modern preparation explicitly UNAVAILABLE; contradictory root/parent409. Original identities retained; retry is not a new logical EA. |
| description | separately validated persisted Action Context Envelope | At most500 accepted redacted summary characters; missing/private-path content gives NOT_RECORDED. No runtime prompt or generated replacement. |
| current phase and steps | `engineering_transactions`, `execution_lifecycle_events`; existing pure `execution_lifecycle.project_records` | Actual persisted checkpoint/events, intended pending steps separate; missing events unavailable. No timing/log-derived progress or percentage/ETA. |
| historical transitions | same run's original ordered lifecycle events | At most100; missing history unavailable, over-limit409, not silently complete. SQLite CURRENT_TIMESTAMP is owning UTC and normalized explicitly. |
| technical outcome | terminal checkpoint when recorded, otherwise dispatchstate | COMPLETE/BLOCKED/FAILED are EP outcomes, not Forge Mission acceptance. |
| qualification | persisted `execution_run_qualification_snapshots` | PASS/FAIL/UNRESOLVED and QUALIFIED/NOT_QUALIFIED separate; absent UNAVAILABLE. Does not read or generate host reports. |
| observed/source times | snapshot read time versus original recorded update/watermark | Fresh HTTP response is not live executor evidence; source_updated_at includes persisted qualification when available. |

| Persisted effective lifecycle | Role / wait meaning |
|---|---|
| INITIALIZE/CAPABILITY_REVIEW | EXECUTION_HOST; actual recorded preparation |
| EXECUTE_AGENT | IMPLEMENTATION_AGENT processing |
| LOCAL_REPOSITORY_VALIDATION | EXECUTION_HOST validation |
| QUALITY_CONTROL_AGENT | AUTONOMOUS_QUALITY_SECURITY processing, **not human review** |
| REPAIR_AGENT/FINALIZATION_REPAIR_AGENT | IMPLEMENTATION_AGENT; repeated original events remain visible |
| WAIT_FOR_TERMINAL_EVIDENCE | automated check-evidence wait, EXECUTION_HOST; not operator merge PASS |
| WAIT_FOR_OPERATOR_MERGE and effective finalization/reconciliation merge wait | OPERATOR only for the explicitly persisted operator boundary; verified resume is not a stale operator request |
| FINALIZE_AGENT/RECONCILE_AGENT/REPOSITORY_CLEANUP | respective recorded finalization/host steps |
| COMPLETE/BLOCKED/FAILED | terminal technical outcome; missing intermediate history stays unavailable |
| QUEUED without run | dispatch not yet recorded; an actual persisted OPEN failed/blocked project operatorgate is separately PROJECT_OPERATOR_GATE/OPERATOR |

## Snapshot, bounds and errors

One consistent read transaction supplies all selected data. A source-generation
monitor conservatively refuses changes before publication; it never combines
incompatible generations. Scope, selection, physical source identity and SQLite's persistent rollback-journal
header generation bind `snapshot_id`. Any CENTRAL commit may conservatively
invalidate the snapshot; exact detail queries only its selected run. A reconnect/lost reply or separate Server OS restart can
read the same original run/attempt identities, and may observe newer actual
progress. Byte-identical forever is not promised. Optional expected_snapshot
fails explicitly if no longer current; no new submission or run is created.

Maximum100 known submissions per project/Mission selection,100 events per run,
262144 stored JSON bytes per record,524288 response bytes and3s reader deadline
with SQLite progress interruption. Limits fail explicitly rather than claim
complete truncated history. No raw prompts, constraints, commands, hostpaths,
provider output, transcripts, credential material or protected logexports.
`business_mutations: 0` is qualified against effect/SQL lineage, separately from
normal server audit/background activity. There is no second status database.

Healthy empty within an authorized exact selection is200. Invalid input400;
invalid/revoked/expired grant401; scope/authority403; selected run absent404;
source/snapshot/provenance/limit conflict409; unsupported contract/schema422;
corrupt source500; missing/unavailable source/deadline503. Missing access or
history never becomes an empty successful substitute. All replies no-store.

## Consumer limits and qualification

This is the limited reporting/readback companion for PB-W3-PROGRESS/HOB-EP03,
not delivery of all onboarding, assessments, provisioning, cancel or recovery.
Forge owns Mission acceptance; `forge_mission_acceptance: NOT_OWNED_BY_EP` is
explicit even for technical COMPLETE. Logical action counts cover only admitted
known scoped records, exclude unknown legacy, and do not double-count retries.
Actions never offered to EP are outside completeness.

The closed packaged schema is `schemas/workspace-execution-progress-v1.schema.json`.
Owning small installed HTTP proof precedes full mandatory regressions, combined
module gates plus strict changed executable-line >80.2%, independent Quality and
Security, all required CI/OwnerAuthorization, protected implementation, separate
nonempty Finalization and new exact-final-main installed HTTP/readback/handoff/
actual-stop/cleanup evidence. No GUI/screenshot wave or consumer ACK is added.
TDE remains separate and honest. Current source/docs are not delivery evidence.
