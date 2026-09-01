# EP-GOLDEN-001 — Engineering Platform Productization

This deterministic scenario verifies repository bootstrap, readiness, identity
and configuration loading, configured provider resolution, runtime execution
simulation, qualification, finalization simulation and repository-handoff
simulation. It never creates a PR, merges, contacts external services, uses
secrets or changes tracked production files.

Run locally against the installed package with `python3 -c "from pathlib import Path; from engineering_platform.golden_scenario import run; print(run(Path('.')))"`. CI installs the package and runs
`tests.engineering.test_engineering_platform_golden` through the standalone
Golden Qualification workflow. A failure reports phase, diagnostic, expected
state and remediation.

## Canonical authority

The historical `main` advisory Golden workflow that invoked the removed
DJConnect-era `tools/verification/run_golden_ci_qualification.py` is
**SUPERSEDED_BY_STANDALONE_EP_GOLDEN_FLOW**. It is not retained as a second
qualification authority on the PR #1 candidate. The canonical source-level
Golden qualification is the package-installed `EP-GOLDEN-001` scenario and its
unit test; `Golden Smoke` runs for pull requests and `Golden Regression` runs
for `main`, scheduled, and manually dispatched validation.
