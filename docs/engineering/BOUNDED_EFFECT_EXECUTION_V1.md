# Bounded effect execution and result evidence V1

**Status:** QUALIFIED_ON_MAIN / source and noneditable installed profile readback complete.
**Package:** 2.3.111. **Owner:** LANE_2_WORK, existing EP source writer.
**Assignment:** `L2-EP-MANAGED-PUBLICATION-RECOVERY-V1-20261006`.

The owner approved this FME runtime extension in the existing Work session.
[Pickup and first execution proof](https://github.com/pcvantol/forge/issues/142#issuecomment-6014921887)
bind that approval to the [Forge contract request](https://github.com/pcvantol/forge/issues/142#issuecomment-6012266264).
MPR remains completed at 2.3.109, GP remains closed, and historical #175 remains
closed/unmerged. The r30 dependency/hold dispositions remain applicable. There
is no new assignment, writer, engine, queue, live EP environment, publication
canary or public release. The two previously consumed assignment correction
rounds remain counted; the FME review corrections use the third and final round.

## FIE-24 validation-profile readback follow-up

The [concrete L3 request](https://github.com/pcvantol/forge/issues/142#issuecomment-6018928661)
identified that result 1.0 omitted the selected validation bindings used by the
profile digest. The existing r31 assignment and sole writer publish those inputs, now qualified
on protected main2.3.111; execution scope and three consumed correction rounds
are unchanged.
The [2.3.110 exact-final-main receipt](https://github.com/pcvantol/engineering-platform/pull/341#issuecomment-6019329270)
remains historical evidence, not qualification of this follow-up.

Effect-result **1.1** and newly materialized terminal evidence **1.6** expose
`validation_profile`, with strict `effect-validation-profile-v1.schema.json`.
The object contains exactly `version: "effect-validation@1.0"`, the same
`subject` as the result, ordered `controls` pairs `[validation_id, authority]`,
and the exact ordered selected `validation_bindings`. Each binding has
`validation_id`, `category` and the command argument array `command`. Empty
bindings are explicit for modes without an owning validator. They are frozen
checkpoint inputs already bound to the immutable control/review observations;
readback does not select or execute a new profile.

A consumer computes `sha256:` plus SHA-256 of this whole object serialized as
ASCII JSON, sorted keys, `ensure_ascii=true`, separators `,`/`:` and no newline.
It checks the profile subject against the result, the ordered control pairs
against the required observed controls, and the digest against every control
and exact-subject review. Omitting bindings, changing their order/content or
uniformly replacing the advertised digest must not pass. No server-private
input is needed. Command arguments are inert authenticated evidence, never an
instruction for the consumer to execute.

The digest algorithm and all existing accepted request/checkpoint/report inputs
are unchanged. Request and report-envelope contracts remain 1.0. Result 1.0 and
terminal 1.5 schemas and pinned historical captures remain unchanged. Current
result readback advertises 1.1; old strict consumers must negotiate that version
before accepting it. Existing immutable terminal 1.5 artifacts remain readable;
new terminal materialization uses 1.6. A consumer requiring FIE-24 must reject an
older artifact without these inputs or join its exact report/subject to the
authenticated 1.1 result. Non-FME terminal 1.4 is unchanged. NOT_STARTED in result
1.1 remains explicitly unqualified and contains no fabricated profile.

The same producer/project credential and corruption checks protect these inputs.
Only the existing supported hermetic validators are covered. No live EP test
environment, new mode, execution privilege, repair budget or publication is added.

## Completed FIE-24 producer delivery

[PR #342](https://github.com/pcvantol/engineering-platform/pull/342) merged normally
through the configured protected squash route to **`bc2e8d6800cc64ef44990ce9412d6b9c1c8824ff`**, tree
`df1d8a316cf6bdc6e2e64b6f3afa56ef796747d2`, identical to reviewed candidate
`c8f3fe1bc63436076d8ee65c20491e2fca1cd48f`.
[Independent reviews/source qualification](https://github.com/pcvantol/engineering-platform/pull/342#issuecomment-6020297420)
and [exact protected-main installed receipt](https://github.com/pcvantol/engineering-platform/pull/342#issuecomment-6020710204) bind this completed
producer boundary.

Source qualification:2,245 discovered,2,243 passed/two existing skips. All19
changed production files across the approved FME assignment strictly exceed
80.2% executable-line coverage (minimum83.0488289%); all163 module gates pass,
aggregate84.87256197%. Normal Linux CI passed2,263 discovered/2,262 passed/one
existing skip and coverage2,245 discovered/2,244 passed/one existing skip, native
enforcement, installed matrices, HTTP, four browser shards, localization,
security/CodeQL and version/projection. Exact-SHA Owner Authorization is the
supported success/not-required LOW_RISK-or-NORMAL_RISK disposition.

Exact main noneditable Python3.14.8 qualification outside checkout:67tests PASS
(118.263s),190package files equal to wheel and committed source before/after
testing,192committed test files verified,15version components PASS and all three
actual new-process repair recovery boundaries PASS. Each preserves one PR,
one repair/two attempts/six provider invocations and unchanged target; repeated
resume completes without replay.

Wheel SHA-256 `4d588b831b0979389ed5238f1b78ee0eff8f487b46f5ee938892cd5127bb643e`;
sdist SHA-256 `2903afc5959da6c39c4543d526913d039006b28fd92582463df2f3f4066e73a9`.
The normal documentary finalization is NO_BUMP and carries the further
exact-final-main installed receipt after its own protected merge.

[Installed1.1/1.6 producer captures](fixtures/fme-producer-v1.1/README.md) supply
all five supported combinations and five actual errors for L3's stateful HTTP
simulator. Manifest SHA-256 `4490eb31468028138d4942acda5380464f99fc70cb4666f32278b809a1922896`. It binds the exact protected producer,
five schemas, five serializer/digest sources and six capture files. Every profile
independently reconstructs all control/review digests, including nonempty selected
bindings for BOUNDED_REPOSITORY_CHANGE/GIT. Old captures remain immutable.
`AVAILABLE_TO_CONSUMER=TRUE` for this qualified boundary; Forge FCI remains L3-owned.
Actual TDE policy FAIL/repository qualification FAILED remains nonblocking
NFR-TDE-001, never workflow-derived policy PASS. GP/MPR closure, r30/native holds,
hermetic validator scope and correction consumption3 remain intact.

## Composition and required controls

Forge owns Candidate, approvals, Mission/Action criteria, successors and scope
amendments. EP executes one immutable accepted Action. The existing HTTP/CLI
submission service, CENTRAL dispatcher, EngineeringRunner, leases, provider,
control observations and managed-publication service remain authoritative.
Absent FME opt-in retains legacy behavior; malformed present opt-in cannot
fall back to it.

| Mode | Delivery | Controls before independent Quality/Security |
| --- | --- | --- |
| `READ_ONLY_ASSESSMENT` | `EVIDENCE_ONLY`, explicit empty writes | Source binding, result integrity, scope containment, source-backed criterion report |
| `DOCUMENTATION_ONLY` | Approved document paths, `GIT` | Four report controls plus document content, local links and schema |
| `ARCHITECTURE_DESIGN_ONLY` | Explicit `EVIDENCE_ONLY` or `GIT` | Document controls plus alternatives, boundaries, decision and open questions |
| `BOUNDED_REPOSITORY_CHANGE` | Explicit ordinary file scope, `GIT` | Four report controls plus the repository's committed command/script validator |

Docs/design do not acquire an arbitrary application build requirement. An
explicit existing registry profile adds its complete controls, frozen before
execution. Proposed configuration cannot weaken that baseline. Evidence-only
with an explicit repository profile, additional post-merge control tokens, and
parallel-Action/Genesis composition are unsupported in V1 and reject explicitly.
Skipped, unavailable or failed required controls cannot become PASS.

Owning command environment assignments and script entrypoints are interpreted
as arguments without a shell wrapper. Validators receive a disposable copy
for temporary build/test outputs; original files and Git metadata in that copy
must still match afterwards, while the delivery candidate remains read-only.
The owning declaration cannot override host isolation or Git safety settings.
The selected Python runtime supplies script launchers and dependency paths.

**Supported boundary:** hermetic consumer validators are qualified. The current
full EP and Forge validators include host-loopback HTTP tests; their complete
execution is unsupported by this no-network validator profile. Those controls
fail rather than being skipped or replaced by a weaker profile. This document
does not qualify those default validators as executable FME consumers. A future
isolated loopback capability requires its own implementation and qualification.

## Public producer contracts

`constraints.effect_contract` on the existing submission endpoint has exactly:
`contract_version: "1.0"`, `mode`, `delivery`, `source_revision` (full lowercase
40-character SHA), `read_paths`, `write_paths`, and `criteria` (unique `id` plus
substantive `description`). Evidence-only requires `write_paths: []`; Git needs
nonempty explicit writes. The sibling repository-revision binding must agree.
The source is the current accepted target HEAD; drift blocks execution.

Paths are relative ASCII files or directory prefixes ending in `/`. Traversal,
globs, case aliases, control/credential paths, links, special or executable file
proposals reject. V1 supports ordinary UTF-8 text additions/replacements, not
deletion, binary output, chmod, rename or arbitrary command effects. Docs/design
outputs require `.md`, `.txt`, `.rst`, `.adoc`, `.mmd` or `.puml`. Security still
assesses content; a document suffix grants no executable authority.

Scoped authenticated `/v1/producer-compatibility` advertises effect request 1.0,
effect result 1.1, validation profile 1.0 and terminal evidence 1.6. The legacy public declaration and
non-FME terminal 1.4 retain compatibility. Current strict schemas ship in the wheel; prior versioned schemas remain available:

- `schemas/effect-request-v1.schema.json`
- `schemas/effect-report-envelope-v1.schema.json`
- `schemas/effect-result-v1.1.schema.json`
- `schemas/effect-validation-profile-v1.schema.json`
- `schemas/terminal-evidence-v1.6.schema.json`

`GET /v1/projects/{project}/submissions/{submission}/effect-result` returns actual
report bytes represented by `artifact.content`. The SHA-256 digest covers ASCII
JSON with sorted keys, `ensure_ascii=true`, separators `,`/`:` and no trailing
newline. The envelope binds source and manifest, request digest, criteria,
contract, run/submission/project/repository/producer/version, correlation,
Mission/Action, provider invocation and repair ordinal. Source references must
exist in the accepted snapshot. Quality judges relevance and usefulness;
minimum text length alone is insufficient.

Only the submitting producer's project credential can read the result or its
terminal artifact. Another producer receives 404; invalid credentials 401;
missing/corrupt result evidence 409; CENTRAL failure 503. Before a transaction
exists the result is explicitly `NOT_STARTED`. Existing submission readback
distinguishes in-progress work from missing terminal evidence.

Terminal 1.6 links the same report ID/digest/readback path and embeds the result
projection without duplicating content. `effect_qualified` requires all selected
controls and both exact-subject reviews. Evidence-only uses `REPORT_ARTIFACT`,
source provenance and null candidate/merge/PR. A useful no-change conclusion may
qualify; generic COMPLETE, empty work, irrelevant criteria and write-mode
unchanged trees cannot. Consumers must pin qualified producer contracts; a
simulator cannot invent fields or turn an unsupported contract into success.

## Enforced effects and owned storage

The provider receives only approved committed regular files in a private
snapshot outside the target. Git metadata, untracked/ignored files and links
are excluded. Source is bounded to 8 MiB total and 1 MiB/file; output JSON to
1 MiB. Blob sizes are checked before reading their bodies. Git observations
disable replacement objects, lazy fetch, fsmonitor and automatic index refresh;
missing promised objects block without contacting a remote or writing target
metadata. Detectable token formats, credential assignments/URLs and private keys
reject before persistence. Scoped reads and mandatory Security review remain
necessary: this is not a universal secret detector.

Codex CLI **0.160.1** is the qualified native sandbox runtime, pinned with npm
integrity in CI. Other versions fail closed. Each execution/review uses a unique
permissions profile with snapshot reads, no target writes, no tool network,
approval `never`, strict config and no user/project instructions. MCP, plugins,
browser/computer/cloud tools, skills, subagents and other effectful integrations
are disabled. The host provider may contact its model; tools cannot. Admission
runs a real negative sandbox probe. Unavailable enforcement has no permissive
fallback.

The native runtime executable receives an exact-file read grant, including the
qualified npm launcher's platform binary. Linux requires that binary for its
in-sandbox re-execution. Installation directories and sibling files receive no
grant. Outside-scope paths may be hidden by Linux mount isolation, have a
read-only mount ancestor, or be denied by macOS. Admission accepts only
`EACCES`, `EPERM`, `ENOENT` or `EROFS` for the forbidden file operation; other
errors cannot prove enforcement. The network probe still requires permission
denial. CI runs actual native tool and lifecycle admission probes before the
complete suite.
The write probe targets the actual host-backed source mount. Linux may provide
private tmpfs ancestors for its mount layout; test readback proves writes in
that namespace cannot create the corresponding host files.

Validators execute real sandbox children with owned candidate/result reads,
private scratch writes and no inherited credentials or ambient Git/Python
configuration. On macOS the selected interpreter's resolved binary avoids a
denied venv symlink chain; selected venv site-packages are explicitly supplied.
This does not claim that validator sys.prefix remains the venv. Host installed
qualification separately verifies interpreter/package identity. Control output
is discarded, actual exit/timing/lineage is persisted, owned process groups are
killed/reaped on exit/timeout and validation scratch is removed in finally.

CENTRAL `artifacts/effects/{run}` uses owned, non-shared real directories and
exclusive/no-follow writes. Reports/reviews have immutable insert-and-compare
entries in the existing artifact catalog. Readback checks catalog/file digests
and immutable provider and command start/terminal observations. Checkpoint PASS
alone cannot qualify. Reports, reviews, source manifest/snapshot and owned
delivery checkouts remain run evidence under existing operator retention
authority. Scratch is disposable; no automatic evidence deletion service is
introduced and no report is placed in the target without approved Git effects.

For Git output, only the host applies proposals in its owned checkout. It checks
exact paths/bytes, creates a commit without hooks/filters, validates and reviews,
then uses MPR publication. Uncertain publication performs exact readback before
any retry. Existing protected merge/delegation gates apply, followed by verified
main ancestry and merged bytes in the owned delivery checkout. Failed hosted
checks consume the same persisted repair budget and advance the same PR through
an exact expected-head lease. Lost acknowledgements recover by readback; the
initial immutable MPR publication intent is retained. Non-blocking review
observations retain their disposition. Finalization cannot silently invoke a provider
with broader effects.

## Recovery and qualification

One existing lease serializes execution. Live consumer/binding/source authority
is rechecked before provider/review/publication. Identity and previous attempts
are immutable. Each attempt has one implementation invocation and distinct
mandatory 3.0 Quality/Security reviews. At most three repairs apply to the same
run; prior blockers need explicit dispositions. A start without durable result
blocks as uncertain instead of replaying. Completed resume verifies evidence
without invoking providers. Forge owns any successor or approved scope change.

The owning tests are `test_effect_execution`, `test_effect_boundaries`,
`test_effect_recovery` and `test_effect_integration`. They use actual HTTP, CENTRAL, dispatcher, runner, Git
objects, controls, sandbox and new processes. Only external provider/GitHub
transports are fixtures. A separate real-CLI test replaces only model HTTP and
attempts forbidden tool reads/writes/network/escalation with dangerous inherited
user configuration. Required native enforcement is not skipped into PASS.

Disposable GitHub-hosted Linux qualification enables user-namespace creation
before the native sandbox starts. The setup script refuses other hosts and
does not alter product permissions; all filesystem/network denial tests remain
mandatory. The runner setup addresses Ubuntu's
[AppArmor user-namespace restriction](https://discourse.ubuntu.com/t/understanding-apparmor-user-namespace-restriction/58007).

## Previously qualified 2.3.110 producer baseline — 2026-10-06

[PR #340](https://github.com/pcvantol/engineering-platform/pull/340) merged through
the normal protected route to **`ff2f5072bcf5df7aa2828dde0b2d1b0a6ad32433`**.
Tree `ca5669c3b7df75e7d19742fee046a494060e94df` is identical to independently
reviewed candidate `cbf76eb1c525fbc9934fa450cca09d9e3f0ceae7`.
The [exact-head review and source receipt](https://github.com/pcvantol/engineering-platform/pull/340#issuecomment-6018046195)
and [protected-main installed receipt](https://github.com/pcvantol/engineering-platform/pull/340#issuecomment-6018236216)
bind the completed evidence chain.

| Gate | Observed result |
| --- | --- |
| Complete local suite, Python 3.14.8 | 2,243 discovered; 2,241 passed, two existing skips |
| Changed production executable-line coverage | All 19 files strictly >80.2%; minimum 83.0488289% |
| Existing module coverage contract | All 163 modules >=80.2% combined; minimum 80.3203661%, aggregate 84.8195205% |
| Independent exact-head Quality and Security | PASS; earlier findings closed, all subsequent platform deltas reviewed; total assignment correction consumption remains three |
| Hosted Linux qualification | [Run 37474472062](https://github.com/pcvantol/engineering-platform/actions/runs/37474472062) PASS: native containment/admission, 2,261-test suite (2,260 passed/one existing skip), installed matrices, 2,243-test coverage run (2,242 passed/one existing skip), all module gates and 84.79% aggregate |
| Browser and product gates | Four browser shards, localization, HTTP/OpenAPI/Postman, CodeQL/security, projection/version, Trusted Delivery technical gate and exact-head supported Owner Authorization PASS |
| Exact-main noneditable wheel | 187 package files byte-equal to wheel and committed source; Python 3.14.8, outside checkout |
| Exact-main installed FME/MPR | 65 tests PASS; all three actual new-process repair recovery scenarios PASS, one PR/one repair/six provider invocations, unchanged target and idempotent resume |
| Exact-main version readback | 15 source/installed components agree on 2.3.110 |
| Actual TDE observation | Assessment FAIL / repository qualification FAILED, coverage unavailable; existing nonblocking NFR-TDE-001, never policy PASS |

Exact implementation-main wheel SHA-256:
`e2383703217f2071ff55c3ef3661acab6f7d774a7270052c3c7f5e0ce4d5986a`.
Sdist SHA-256:
`4961978307648265a3fc0bccf57e69ce792971f3c1421d555f6ea75944d2c178`.
The ordinary documentary finalization is `NO_BUMP`; its PR carries the further
exact-final-main build/install receipt after its own protected merge.

## Historical 1.0/1.5 HTTP simulator handoff

[Installed producer captures](fixtures/fme-producer-v1/README.md) are pinned to
qualified producer `ff2f5072bcf5df7aa2828dde0b2d1b0a6ad32433` / 2.3.110.
The manifest contains exact schema, serializer and capture SHA-256 digests.
The five supported mode/delivery combinations contain actual request,
acceptance, NOT_STARTED, qualified report and terminal1.5 responses. Git cases
also retain an unqualified pre-merge response. Actual errors include invalid
credentials, foreign producer, unsupported request and corrupt/missing report.
The captures label the external provider/Git/GitHub fixtures explicitly; EP's
HTTP serializer, CENTRAL, dispatcher, runner, controls, native sandbox,
publication persistence and readback are real installed services.

`AVAILABLE_TO_CONSUMER=TRUE` for this bounded producer contract. L3 retains its
stateful EP HTTP simulator and independently qualifies Forge's intake,
observation and completion behavior. This producer delivery does not claim
Forge's whole FCI suite is complete and requires no EP import or live instance.
The supported and unsupported boundaries above remain part of the contract.
GP, MPR, historical #175 and r30/native hold dispositions remain unchanged;
closure starts no new Mission, public release or operational installation.
