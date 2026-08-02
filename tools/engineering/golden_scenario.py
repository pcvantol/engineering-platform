"""Deterministic, side-effect-bounded Engineering Platform Golden Scenarios."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from .platform_api import PlatformConfiguration, provider_registry
from .platform_bootstrap import provision_workspace, validate_repository
from .qualification import execute_qualification

SCENARIO_ID = "EP-GOLDEN-001"


def run(root: Path, *, fail_phase: str | None = None) -> dict[str, object]:
    """Prove the productized lifecycle without PRs, merges, secrets or network writes."""
    phases: list[dict[str, object]] = []
    try:
        for name, operation in (
            ("repository_bootstrap", lambda: validate_repository(root)),
            ("readiness", lambda: provision_workspace(root)),
            ("configuration", lambda: PlatformConfiguration.load(root)),
            ("providers", lambda: provider_registry(root)),
            ("runtime_execution_simulation", lambda: True),
            ("qualification", lambda: execute_qualification(root)),
            ("finalization_simulation", lambda: {"state": "MERGED_RECONCILED"}),
            ("repository_handoff_simulation", lambda: {"generated": True}),
        ):
            if fail_phase == name:
                raise RuntimeError("deterministic fixture failure")
            result = operation()
            # EP-GOLDEN-001 validates provider selection and contracts, not
            # host-specific executable availability or private connectivity.
            if name == "qualification" and result["qualification"] != "PASS":
                raise RuntimeError("qualification failed")
            phases.append({"phase": name, "status": "PASS"})
        payload = {"scenario_id": SCENARIO_ID, "result": "ENGINEERING_PLATFORM_GOLDEN_PASS", "executed_at": datetime.now(timezone.utc).isoformat(), "phases": phases, "evidence": ["platform_identity", "workspace_identity", "providers", "readiness", "qualification", "handoff_simulation"]}
    except Exception as error:
        payload = {"scenario_id": SCENARIO_ID, "result": "ENGINEERING_PLATFORM_GOLDEN_FAIL", "executed_at": datetime.now(timezone.utc).isoformat(), "phases": phases, "failed_phase": fail_phase or (phases[-1]["phase"] if phases else "repository_bootstrap"), "diagnostic": str(error), "expected_state": "ENGINEERING_PLATFORM_GOLDEN_PASS", "remediation": "Correct the reported configuration, provider, readiness or qualification failure and rerun EP-GOLDEN-001."}
    directory = root / ".engineering" / "qualification"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "ep-golden-001.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload
