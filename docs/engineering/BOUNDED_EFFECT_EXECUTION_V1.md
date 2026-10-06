# Bounded effect execution and result evidence V1

**Status:** IMPLEMENTED / qualification in progress; main and installed acceptance pending.
**Package:** 2.3.110. **Owner:** LANE_2_WORK, existing EP source writer.
**Assignment:** `L2-EP-MANAGED-PUBLICATION-RECOVERY-V1-20261006`.

The owner approved this FME runtime extension in the existing Work session.
[Pickup and first execution proof](https://github.com/pcvantol/forge/issues/142#issuecomment-6014921887)
bind that approval to the [Forge contract request](https://github.com/pcvantol/forge/issues/142#issuecomment-6012266264).
MPR remains completed at 2.3.109, GP remains closed, and historical #175 remains
closed/unmerged. The r30 dependency/hold dispositions remain applicable. There
is no new assignment, writer, engine, queue, live EP environment, publication
canary or public release. The two consumed assignment correction rounds remain.

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
| `BOUNDED_REPOSITORY_CHANGE` | Explicit ordinary file scope, `GIT` | Four report controls plus the repository's committed command validator |

Docs/design do not acquire an arbitrary application build requirement. An
explicit existing registry profile adds its complete controls, frozen before
execution. Proposed configuration cannot weaken that baseline. Evidence-only
with an explicit repository profile, additional post-merge control tokens, and
parallel-Action/Genesis composition are unsupported in V1 and reject explicitly.
Skipped, unavailable or failed required controls cannot become PASS.

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
effect result 1.0 and terminal evidence 1.5. The legacy public declaration and
non-FME terminal 1.4 retain compatibility. Four strict schemas ship in the wheel:

- `schemas/effect-request-v1.schema.json`
- `schemas/effect-report-envelope-v1.schema.json`
- `schemas/effect-result-v1.schema.json`
- `schemas/terminal-evidence-v1.5.schema.json`

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

Terminal 1.5 links the same report ID/digest/readback path and embeds the result
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
1 MiB. Detectable token formats, credential assignments/URLs and private keys
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
main ancestry and merged bytes. Finalization cannot silently invoke a provider
with broader effects.

## Recovery and qualification

One existing lease serializes execution. Live consumer/binding/source authority
is rechecked before provider/review/publication. Identity and previous attempts
are immutable. Each attempt has one implementation invocation and distinct
mandatory 3.0 Quality/Security reviews. At most three repairs apply to the same
run; prior blockers need explicit dispositions. A start without durable result
blocks as uncertain instead of replaying. Completed resume verifies evidence
without invoking providers. Forge owns any successor or approved scope change.

The owning tests are `test_effect_execution`, `test_effect_boundaries` and
`test_effect_recovery`. They use actual HTTP, CENTRAL, dispatcher, runner, Git
objects, controls, sandbox and new processes. Only external provider/GitHub
transports are fixtures. A separate real-CLI test replaces only model HTTP and
attempts forbidden tool reads/writes/network/escalation with dangerous inherited
user configuration. Required native enforcement is not skipped into PASS.

Complete-suite/module coverage, security, OpenAPI/Postman, independent reviews,
protected merge and installed-wheel evidence are required before closure.
L3 continues with its EP-HTTP simulator, without EP imports or a live EP test
environment. Final main/installed receipts must replace this in-progress status;
source-only evidence is not installation or publication evidence.
