"""Immutable, read-only diagnostic evidence for failed host qualification checks.

This module deliberately translates existing qualification results only.  It
does not decide whether execution is admitted, retried, or resumed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid
from typing import Iterable


DRIFT_CATEGORIES = frozenset({
    "Runtime Database", "Runtime Identity", "Runtime Schema",
    "Execution Host Version", "Bootstrap Contract", "Checkpoint Format",
    "Memory Format", "Report Format", "Configuration", "Workspace",
    "Repository", "Capability", "Producer Contract", "Execution Policy",
})


@dataclass(frozen=True)
class DriftEvidence:
    drift_id: str
    category: str
    severity: str
    expected_value: str
    observed_value: str
    resolution_recommendation: str
    detection_timestamp: str
    qualification_stage: str
    affected_component: str
    affected_repository: str
    affected_runtime: str

    def payload(self) -> dict[str, str]:
        return asdict(self)


def category_for(identifier: str, stage: str) -> str:
    """Map stable check IDs to the canonical, extensible drift taxonomy."""
    identifier = identifier.casefold()
    if identifier in {"telemetry_storage", "storage_schema"}:
        return "Runtime Database" if identifier == "telemetry_storage" else "Runtime Schema"
    if identifier in {"host_identity", "workspace_identity", "target_repository_identity"}:
        return "Runtime Identity"
    if identifier in {"execution_host_version", "runner_version"}:
        return "Execution Host Version"
    if identifier == "bootstrap_contract":
        return "Bootstrap Contract"
    if identifier == "checkpoint_format":
        return "Checkpoint Format"
    if identifier == "memory_format":
        return "Memory Format"
    if identifier == "report_format":
        return "Report Format"
    if identifier == "configuration" or identifier == "configuration_schema":
        return "Configuration"
    if identifier in {"runtime_components", "provider_support", "required_capabilities"}:
        return "Capability"
    if "producer" in identifier:
        return "Producer Contract"
    if identifier == "execution_mode":
        return "Execution Policy"
    if stage == "Workspace Preflight":
        return "Repository" if identifier.startswith(("git_", "worktree_", "managed_")) else "Workspace"
    return "Capability" if stage == "Capability Preflight" else "Workspace"


def evidence_for_checks(
    checks: Iterable[object], *, stage: str, repository: str, runtime: str = "Engineering Platform"
) -> tuple[DriftEvidence, ...]:
    """Create deterministic evidence for every failed pre-existing check."""
    now = datetime.now(timezone.utc).isoformat()
    evidence: list[DriftEvidence] = []
    for check in checks:
        if getattr(check, "outcome", None) != "FAIL":
            continue
        identifier = str(getattr(check, "identifier", "unknown"))
        reason = str(getattr(check, "reason", "Observed qualification check failed."))
        recovery = str(getattr(check, "recovery", "Resolve the reported qualification drift."))
        evidence.append(DriftEvidence(
            drift_id=f"drift-{uuid.uuid4().hex}",
            category=category_for(identifier, stage), severity="BLOCKING",
            expected_value=f"{identifier}: PASS", observed_value=reason,
            resolution_recommendation=recovery, detection_timestamp=now,
            qualification_stage=stage, affected_component=identifier,
            affected_repository=repository, affected_runtime=runtime,
        ))
    return tuple(evidence)


def persist(root: Path, evidence: Iterable[DriftEvidence]) -> tuple[dict[str, str], ...]:
    """Append immutable evidence documents; never rewrite a prior observation."""
    items = tuple(evidence)
    if not items:
        return ()
    directory = root / ".engineering" / "drift-evidence"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for item in items:
            descriptor, temporary = tempfile.mkstemp(prefix=".drift-", dir=directory)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(item.payload(), sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, directory / f"{item.drift_id}.json")
    except OSError:
        # Existing fail-closed qualification remains authoritative if local
        # diagnostic persistence is unavailable.
        return tuple(item.payload() for item in items)
    return tuple(item.payload() for item in items)


def summary(evidence: Iterable[dict[str, object]]) -> str:
    """Return one compact, operator-facing explanation without source inspection."""
    items = list(evidence)
    if not items:
        return "No drift detected."
    first = items[0]
    return (
        f"{first.get('qualification_stage', 'Qualification')} blocked by "
        f"{first.get('affected_component', 'an unresolved component')} "
        f"({first.get('category', 'Drift')}). Expected: {first.get('expected_value')}. "
        f"Observed: {first.get('observed_value')}. Required action: "
        f"{first.get('resolution_recommendation')}"
    )


def guidance(evidence: Iterable[dict[str, object]]) -> dict[str, object]:
    """Read-only retry/resume advice; it does not change lifecycle authority."""
    items = list(evidence)
    action = items[0].get("resolution_recommendation") if items else "No action required."
    return {
        "retry_appropriate": bool(items),
        "resume_appropriate": False if items else True,
        "operator_intervention_required": bool(items),
        "prerequisite": action,
    }
