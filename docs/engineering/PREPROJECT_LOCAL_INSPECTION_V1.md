# Local pre-project INSPECT_READ V1

Owner: Engineering Platform. Assignment `L2-PREPROJECT-LOCAL-INSPECTION-V1-20261015`.
Implementation/qualification in progress; this document does not claim delivery.
Product allocation **2.3.121**, contract **1.0**, separate private ledger **1**;
CENTRAL remains **75**. R34 and HOB-EP01 remain closed at their own bindings.

This is only the first local HOB-EP02 INSPECT_READ subset under PB-E0/PB-E1.
It reuses trusted Genesis workspace placement, local installation ownership,
the fixed installed Python runtime and existing native FME read/network isolation.
It does not implement bootstrap, optional assessments, analysis plans, transfers,
registration, remote inspection or a second general runner/scanner service.

## Authority and setup

The normal local installation owner initializes `engineering-platform-inspection
init --data-root PRIVATE_ROOT --expected-instance-id ACTUAL_INSTANCE` once.
An initialized private store that disappears or becomes corrupt is an error;
init never recreates it. No normal command initializes CENTRAL or a target.

The owner uses `bind-source --config PRIVATE_SOURCE_JSON` with the same data-root
and expected-instance flags. The closed setup selection is one `source_ref`,
`host_root`, `workspace_root` and literal `child`. `host_root` is the existing
local execution host with trusted `.engineering/engineering-platform.local.json`.
Its policy must explicitly authorize that workspace root with `direct_children`
and reject symlinks; deny-list entries remain effective. The EP host/store may
not be inspected as a target. Absolute paths stay in private host bindings.
Root and target device/inode identities are bound and rechecked; absent is an
explicit bound state. Replacement needs a new owner binding, not HTTP input.

`issue --config PRIVATE_GRANT_JSON` explicitly binds `grant_id`, `source_ref`,
`consumer_id`, `principal`, `audience`, selected `paths`, `content_paths`,
`ttl_seconds` and `max_operations`. Defaults: TTL900 seconds, four operations;
hard bounds: TTL1..3600, operations1..10. Grant also binds the actual EP instance,
local host identity, fixed metadata profile and hard resource limits. The output
contains the one-time plaintext bearer; transfer/store it through the consumer's
private server credential channel. Never put it in argv, public logs or commits.
`status --grant-id ID` omits the bearer; `revoke --grant-id ID` is permanent.

Discovery READ, project-submission, Console/admin and provider credentials do
not authorize this service. No project identity or registration is required.
A source handle is not a B8R project/repository identity or ownership receipt.
The profile supports only the selected local owning host, not arbitrary CENTRAL
filesystem paths or a remote Agent/clone/provider inspection adapter.

## HTTP and local parity

`POST /v1/onboarding/inspections` admits a new explicit operation:

```json
{"contract_version":"1.0","operation_id":"own-op-001","source_ref":"own-source-001"}
```

The caller knows both references before transport. Required headers are
`Authorization: Bearer …`, `EP-Inspection-Contract: 1.0`, `EP-Instance-ID`,
`EP-Host-ID`, `EP-Consumer-ID`, `EP-Principal`, and `EP-Audience`, exactly as
issued. Unknown fields, duplicate security headers, queries, unsupported
versions and extra transport bodies are rejected. No HTTP input grants access
to an absolute path, new target, field or content class.

`GET /v1/onboarding/inspections/{operation_id}` and an identical POST replay
return the original immutable operation result; neither starts another scan.
Changed request or cross-grant operation identity conflicts. An interrupted
intent with no complete result remains explicit uncertainty/readback-only across
Server OS restarts. A new inspection requires a new explicit operation ID.
Revoked/expired scope blocks retained private content as well as new reads.
No lock spans a scan or a client socket wait.

The installed closed packaged schema is
`schemas/preproject-local-inspection-v1.schema.json`. HTTP responses use no-store.
Local `run`/`read` parity needs explicit `--credential-stdin`, a private `--config`
with source/header bindings, and `--operation-id`. It uses the same admission and
ledger. Inter-product consumers use HTTP only, never this CLI or SQL fallback.

## Source and output meaning

States distinguish ABSENT, EMPTY_DIRECTORY, DIRECTORY, UNBORN_GIT, EXISTING_GIT,
and typed denied/unavailable/unsupported/drift outcomes. Existing Git includes
actual SHA1 HEAD/tree, staged/unstaged counts, untracked candidates, unresolved
index entries, local remote-presence boolean and precise observation time.
Remote URLs and credentials are never returned or contacted. Ignore rules are
NOT_EVALUATED; ignored files are not silently claimed absent or clean.

The first finite Git profile supports ordinary local loose SHA1 commit/tree
objects and index2/3. Packed-only/promised/alternate objects, unknown formats,
bare/nested/submodule/linked worktrees, gitfiles, sparse/split indices and unsafe
symlinks are explicitly unsupported. There is no Git subprocess and no evaluation
of repository hooks, filters, configuration includes, fsmonitor, scripts or tests.
No source/history copy is made. Volatile worktree hashing only computes state;
source-code bodies are not included in results or evidence.

At most16 explicit document/manifest paths are selected. Ordinary text docs may
include README, vision, architecture, roadmap and engineering files with other
explicit names. Hidden/secret/env/key/database/code extraction is not admitted.
Only separate `content_paths` consent returns exact UTF-8 regular document bytes;
metadata-only selections return presence/hash without content. Finite supported
manifests are pyproject.toml, package.json, Cargo.toml, go.mod, pom.xml and
build.gradle, with metadata only. The canonical B8R declaration can be selected
for observed identity/invalid-or-conflicting status, never automatic adoption.
Not selected means NOT_INSPECTED via exclusions, never proven ABSENT.

Limits:2048 metadata entries/depth16;64KiB per selected document/256KiB total;
bounded Git metadata and4MiB volatile worktree hashing;15-second owned worker
deadline/1MiB output cap. Reads refuse links, hardlinks, FIFO/device/socket and
case/Unicode collisions. The existing native0.160.1 sandbox must be qualified;
no permissive fallback exists. Own worker group is actually reaped on success,
timeout, cancellation or revoked authority. Scanner context cannot write the
target, read ungranted files, contact a network or import the target's code.

Snapshots are coherent as of original observation, not permanently current.
Source movement/drift during capture refuses a mixed snapshot. Returned target
write count0 and B8R write count0 must be backed by actual qualification bytes;
two administrative intent/result records are separately admitted private writes.
Architecture, security, code analysis, tests/CI execution, TDE, model assessment
and effort remain NOT_ASSESSED. Merely seeing a CI/test file does not execute it.

## Consumer boundary and delivery

R51's existing supplied inventory remains SUPPLIED_UNVERIFIED. This producer
adds source-bound evidence for allowed fields; Forge still owns interpretation,
semantic canonicalization, adoption and readiness. A true EP-to-Forge adoption
adapter is separate future work, not silently implied by an inventory upload.
Workspace's broader HOB-EP02 proposals are not all delivered by these two routes.

Owning small installed qualification precedes complete regressions/strict changed
executable-line >80.2% coverage, independent full Quality/Security, CI, protected
exact-head implementation merge, nonempty separately reviewed Finalization and
NEW exact-final-main outside-checkout installed HTTP/readback/actual stop/cleanup.
The immutable terminal qualified handoff owns the final source/artifact binding;
an updated main or this in-progress document does not change a consumer pin.
