# EP Server production release

Production publication is main-first and fail-closed. A protected version
preparation PR updates the one canonical version source and its derived EP
projections on `main`; the resulting exact `main` commit is then qualified,
built and published. A release branch can freeze a candidate, but it is never
the exclusive source of a published version change.

Any historic branch-create workflow remains evidence for prior releases only.
It is not authority to publish, re-publish, or rewrite EP release bytes.

## Release-operation lifecycle V1

After both the production-wheel and dashboard qualifications succeed, the
workflow persists one exact `QUALIFIED` release operation before registry side
effects. It binds operation ID, `engineering-platform`/`server`, version,
policy revision, exact post-merge source SHA, exact wheel and sdist SHA-256
identities and the completed EP production qualification. The publication /
readback route then promotes that matching operation through:

```text
PREPARED -> QUALIFIED -> PUBLISHED -> CLEANUP_PENDING -> RELEASE_COMPLETE
                                  \-> RELEASE_COMPLETE
```

`QUALIFIED` is first copied byte-for-byte to a draft GitHub Release before
PyPI can be mutated. The draft is durable provenance rather than a public EP
release. An already-existing PyPI version without that original matching
receipt fails closed; it is never silently adopted. The publisher reloads both
the local record and draft receipt before it can act.

`PUBLISHED` is registry success only; it does not claim closure. The workflow
downloads and hashes both distributions from PyPI, qualifies the actual
downloaded wheel outside the source checkout, and only then writes the
immutable `PUBLISHED` receipt. The terminal job reads that receipt back
byte-for-byte, removes its declared operation-local readback/artifact paths,
records `CLEANUP_PENDING` if any of those removals fail, and only then makes
the draft release visible and writes `RELEASE_COMPLETE`. Workflow concurrency
excludes simultaneous publication. A lost answer or runner crash resumes from
the original draft receipt and exact artifact identity; a retained
`CLEANUP_PENDING` receipt is reloaded and identity-checked before cleanup can
resume.

This source workflow does not itself publish, change a version, or
select/change a local runtime merely by being merged. Executing it is a
separate authorized release operation.

The workflow verifies that the checkout is clean and is the requested current
protected `main` commit. Version preparation has already been merged through
the protected source route; the release workflow never writes a version. The
wheel and source distribution are built from that exact commit, never from a
developer worktree.

Before PyPI publication, the workflow requires: version-consistency and
installed-ingress/API/Postman checks, the complete unit and coverage contract,
dashboard and five-locale browser checks, dependency/static-security checks,
and a fresh-environment wheel smoke. It emits the wheel, source distribution,
checksum, SBOM, coverage report and production-wheel qualification record as
workflow evidence. The publish job receives only the qualified wheel and
source distribution.

PyPI versions are immutable. A failed release run must retain and inspect its
exact operation and artifacts; it must never use a new version or rebuilt bytes
as a shortcut for resolving an ambiguous publication result.

The PyPI action uses PyPI Trusted Publishing. Its job receives only the
ephemeral GitHub OIDC token and no long-lived PyPI API token. Configure the
pending publisher before the first publication with project
`engineering-platform`, owner `pcvantol`, repository
`engineering-platform`, workflow file
`ep-server-production-release.yml`, and no environment. PyPI binds the
publisher to this repository and workflow; a release from another repository
cannot use it.
