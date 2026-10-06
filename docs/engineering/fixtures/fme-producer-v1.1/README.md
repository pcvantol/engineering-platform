# Qualified FME profile-input producer captures

Actual authenticated HTTP captures from the noneditable installed EP **2.3.111**
wheel, protected producer source **`bc2e8d6800cc64ef44990ce9412d6b9c1c8824ff`**, outside the checkout.
The [installed receipt](https://github.com/pcvantol/engineering-platform/pull/342#issuecomment-6020710204) binds the qualification. L3 consumes these
bytes through its stateful HTTP simulator. The older `fme-producer-v1` directory
remains unchanged historical result1.0/terminal1.5 evidence.

[manifest.json](manifest.json) binds all six capture files, five packaged schemas
and five serializer/digest source files. Manifest SHA-256:
`4490eb31468028138d4942acda5380464f99fc70cb4666f32278b809a1922896`. Capture-file hashes include formatting and trailing newline.
Both report-content and profile digests use ASCII JSON with sorted keys,
`ensure_ascii=true`, separators `,`/`:` and **no** trailing newline.

## Public digest inputs

Effect-result **1.1** and terminal **1.6** publish `validation_profile`:
`version: "effect-validation@1.0"`, `subject`, ordered `controls` pairs and ordered
`validation_bindings` with `validation_id`, `category` and command argument array.
`effect-validation-profile-v1.schema.json` defines that strict object. Request
and report-envelope remain1.0. The profile subject equals the result subject;
control pairs equal the required observed control IDs/authorities. Hash the whole
profile and compare the `sha256:` value to **every** control and review digest.
The terminal profile must equal the result profile. Omitting bindings, changing
order/content or uniformly replacing the advertised digest must be rejected.

A binding may contain the captured disposable fixture interpreter path. It is
opaque, inert evidence; the consumer does not execute it or resolve it on its
own host. Normalizing that path before hashing would invalidate the digest.
IDs, source revisions and timestamps belong to each fixture. A simulator that
rebinds an accepted Action must keep request, source, report, profile, reviews
and terminal identities/digests consistent; these bytes grant no authority.

## Captures and qualification boundary

| Capture | Supported output |
| --- | --- |
| read_only_assessment-evidence_only.json | Useful durable report, empty selected bindings, no commit/PR |
| documentation_only-git.json | Scoped document output and protected delivery |
| architecture_design_only-evidence_only.json | Durable design report, no Git delivery |
| architecture_design_only-git.json | Scoped design document and protected delivery |
| bounded_repository_change-git.json | Ordinary-file change with selected hermetic owning validator; nonempty bindings |
| errors.json | Actual400 unsupported request,401 invalid credential,404 foreign producer,409 corrupt/missing report |

Each positive contains request, acceptance, NOT_STARTED, qualified effect result
and terminal1.6. Git cases also retain unqualified pre-merge output. COMPLETE
alone does not qualify evidence. Actual target bytes were unchanged. All five
schemas validate and every profile independently reconstructs the receipt digest.

HTTP, submission, CENTRAL, dispatcher, runner, native sandbox, validation,
persistence, publication reconciliation and readback are actual installed code.
Only external model/provider output (including test review responses), Git remote
transport and GitHub publication/merge responses are explicit deterministic
fixtures. Independent development reviews are separate PR receipts.
No authentication headers, credentials or private infrastructure are retained.

Hermetic validators are qualified. Complete EP/Forge validators needing host
loopback remain unsupported under network=false; their controls cannot be
skipped or weakened into PASS. L3 independently qualifies Forge consumer FCI.
