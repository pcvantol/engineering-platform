# EP Server production release

Production publication is main-first and fail-closed. A protected version
preparation PR updates the one canonical version source and its derived EP
projections on `main`; the resulting exact `main` commit is then qualified,
built and published. A release branch can freeze a candidate, but it is never
the exclusive source of a published version change.

Any historic branch-create workflow remains evidence for prior releases only.
It is not authority to publish, re-publish, or rewrite EP release bytes.

## Release-operation lifecycle V1

The production qualification job persists one exact `QUALIFIED` release
operation before registry side effects. It binds operation ID,
`engineering-platform`/`server`, version, policy revision, exact post-merge
source SHA, exact wheel and sdist SHA-256 identities and qualification. The
publication/readback route then promotes a matching operation through:

```text
PREPARED -> QUALIFIED -> PUBLISHED -> CLEANUP_PENDING -> RELEASE_COMPLETE
                                  \-> RELEASE_COMPLETE
```

`PUBLISHED` is registry success only; it does not claim closure. The workflow
stores that exact state as an immutable GitHub release asset after registry
readback, and the terminal job reads it back byte-for-byte before it records a
separate `RELEASE_COMPLETE` asset. Workflow concurrency excludes simultaneous
publication. Publication performs registry readback and digest comparison
before it records `PUBLISHED`.

The remaining release-recovery increment must add a cross-run durable operation
lookup before a retry can adopt an existing registry version, source-provenance
proof for that adoption, and a persisted `CLEANUP_PENDING` recovery route.
Those states are not claimed complete merely because the state model exists.

The initial implementation supplies this durable EP-owned state boundary only.
It neither publishes, changes a version, nor selects/changes a local runtime.

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
