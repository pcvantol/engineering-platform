# EP-GOLDEN-001 — Engineering Platform Productization

This deterministic scenario verifies repository bootstrap, readiness, identity
and configuration loading, configured provider resolution, runtime execution
simulation, qualification, finalization simulation and repository-handoff
simulation. It never creates a PR, merges, contacts external services, uses
secrets or changes tracked production files.

Run locally with `python3 -c "from pathlib import Path; from
tools.engineering.golden_scenario import run; print(run(Path('.')))"` and
`./tools/engineering/engineering-execution-host qualify`. CI runs both in the Engineering
Platform validation workflow. A failure reports phase, diagnostic, expected
state and remediation.
