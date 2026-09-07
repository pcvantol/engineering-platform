# EP Server production release

Production publication is branch-driven and fail-closed. Create a branch from
the current `main` with the exact name `release-X.Y.Z`, for example
`release-2.2.0`. The creation event starts **EP Server production release**.
Ordinary pushes, pull requests and branches with any other name cannot publish.

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

The PyPI action uses `PYPI_API_TOKEN` from the EP repository's GitHub Actions
secret scope (or an equivalent configured trusted-publishing credential). A
secret stored in another repository is intentionally not readable by this
workflow; configure the same authorized publishing credential in EP before
creating the first release branch.
