# EP Server production release

Production publication is main-first and fail-closed. A protected version
preparation PR updates the one canonical version source and its derived EP
projections on `main`; the resulting exact `main` commit is then qualified,
built and published. A release branch can freeze a candidate, but it is never
the exclusive source of a published version change.

The existing branch-create workflow is a predecessor and must be replaced by
the `RELEASE_OPERATION_LIFECYCLE_V1` delivery increment before another EP
publication. It remains evidence for prior releases; it is not authority to
re-publish or rewrite their bytes.

## Release-operation lifecycle V1

EP persists one durable release operation before registry side effects. It binds
operation ID, `engineering-platform`/`server`, version, policy revision, exact
post-merge source SHA, exact wheel and sdist SHA-256 identities, qualification,
publication receipt and cleanup result. The state sequence is:

```text
PREPARED -> QUALIFIED -> PUBLISHED -> CLEANUP_PENDING -> RELEASE_COMPLETE
                                  \-> RELEASE_COMPLETE
```

`PUBLISHED` is registry success only; it does not claim closure. Re-entry with
the same operation reloads the record and exact artifacts. A second concurrent
operation is excluded. An already published version can be adopted only when
source provenance and both artifact digests match; differing bytes or
provenance fail closed. Publication performs registry readback and digest
comparison before it records `PUBLISHED`. Cleanup is operation-scoped and its
failure remains `CLEANUP_PENDING`; recovery artifacts have explicit retention.

The initial implementation supplies this durable EP-owned state boundary only.
It neither publishes, changes a version, nor selects/changes a local runtime.

The workflow verifies that the checkout is clean and descended from `main`.
It then writes the exact branch version to every canonical EP projection and
commits only those version files to the release branch. The wheel and source
distribution are built from that resulting commit, not from a developer
worktree.

Before PyPI publication, the workflow requires: version-consistency and
installed-ingress/API/Postman checks, the complete unit and coverage contract,
dashboard and five-locale browser checks, dependency/static-security checks,
and a fresh-environment wheel smoke. It emits the wheel, source distribution,
checksum, SBOM, coverage report and production-wheel qualification record as
workflow evidence. The publish job receives only the qualified wheel and
source distribution.

PyPI versions are immutable. A failed release run can be repaired on the same
release branch before publication; after publication, create a new
`release-X.Y.Z` branch with a new version rather than rerunning publication of
the same distribution.

The PyPI action uses PyPI Trusted Publishing. Its job receives only the
ephemeral GitHub OIDC token and no long-lived PyPI API token. Configure the
pending publisher before the first publication with project
`engineering-platform`, owner `pcvantol`, repository
`engineering-platform`, workflow file
`ep-server-production-release.yml`, and no environment. PyPI binds the
publisher to this repository and workflow; a release from another repository
cannot use it.
