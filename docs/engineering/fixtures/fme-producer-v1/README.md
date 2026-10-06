# Qualified FME HTTP producer captures

These are actual HTTP responses captured from the noneditable installed EP
**2.3.110** wheel built from protected producer source
**`ff2f5072bcf5df7aa2828dde0b2d1b0a6ad32433`**, outside the source checkout.
The [installed receipt](https://github.com/pcvantol/engineering-platform/pull/340#issuecomment-6018236216)
and [owning contract](../../BOUNDED_EFFECT_EXECUTION_V1.md) define the proof and
supported boundaries. L3 consumes these fixtures through its stateful HTTP
simulator; no live EP test environment or EP source import is required.

## Integrity and interpretation

[manifest.json](manifest.json) pins the producer version/SHA, all four packaged
JSON schemas, the three owning serializer source files and every capture file
with SHA-256. Capture-file digests cover the exact UTF-8 JSON file bytes,
including formatting and trailing newline. The nested report artifact digest
covers its content serialized as ASCII JSON with sorted keys,
`ensure_ascii=true`, separators `,`/`:` and **no** trailing newline.

Each positive capture contains `request`, `acceptance`, `before_dispatch`,
`effect_result` and `terminal_evidence`. Git cases additionally contain
`before_protected_merge`: an actual report with `effect_qualified=false` and no
delivery revision. Only the later protected-merge readback is qualified. IDs,
fixture repository revisions, timestamps, PR references and digests belong to
that capture; a simulator must keep their bindings consistent when modelling
new accepted Actions. Fixture identities grant no operational authority.

| Capture | Meaning |
| --- | --- |
| `read_only_assessment-evidence_only.json` | Durable source-bound report; no commit or PR |
| `documentation_only-git.json` | Scoped document output and protected Git delivery |
| `architecture_design_only-evidence_only.json` | Durable design report; no Git delivery |
| `architecture_design_only-git.json` | Scoped design document and protected Git delivery |
| `bounded_repository_change-git.json` | Explicit ordinary-file change with the real hermetic owning validator |
| `errors.json` | Actual 400 unsupported request, 401 invalid credential, 404 foreign producer, and 409 corrupt/missing result |

NOT_STARTED and pre-merge reports are explicitly unqualified. Neither
`COMPLETE` by itself nor an unbound report may become a qualified result.
Full EP/Forge validators requiring host-loopback HTTP remain unsupported by
this V1 no-network validator profile; their controls cannot be skipped into PASS.

## Capture method

The producer used the same-commit `EffectIntegrationTests` fixture and actual
authenticated local HTTP submission/result/terminal endpoints. The fixture
created committed Git source and isolated CENTRAL storage. Actual dispatcher,
runner, filesystem/network sandbox, controls, persistence, managed publication
and readback executed. Target bytes were unchanged after every scenario.
All request/result/report/terminal documents validated against installed schemas.
The packaged source and schema bytes matched the manifest after capture.

Only external provider/model output (including test review responses), Git
remote transport and GitHub API/publication/protected-merge responses were
deterministic fixtures. Independent development Quality/Security reviews are
separate receipts; these transport fixtures do not impersonate those reviews.
No authentication headers, credentials or private infrastructure were retained.
