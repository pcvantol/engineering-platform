"""Read-only projection of an immutable Forge governance handoff snapshot."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


FORGE_GOVERNANCE_HANDOFF_VERSION = "1.0"
_MAX_TEXT = 2_000
_REPORT = re.compile(r"(?ms)^## Forge Governance Handoff\s*$.*?^### Immutable Handoff Snapshot\s*$\n```json\s*(\{.*?\})\s*```")
_LEGACY_REPORT = re.compile(r"(?ms)^## Forge Mission Recommendation Handoff\s*$.*?^```json\s*(\{.*?\})\s*```")


class ForgeGovernanceHandoffError(ValueError):
    """Raised only when a supplied Forge handoff cannot meet its transport contract."""


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.strip().split())
    return value[:_MAX_TEXT] if value else None


def _value(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _text(raw.get(key))
        if value is not None:
            return value
    return None


def _items(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for entry in value if (item := _text(entry)))[:50]


def _rank(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def validate_forge_governance_handoff(payload: object) -> dict[str, object]:
    """Validate transport shape without deriving any Forge governance meaning.

    Unknown future fields are retained verbatim.  Known fields are checked only
    when supplied, so legacy and incrementally richer Forge producers remain
    compatible while malformed supplied handoffs fail closed at ingress.
    """
    if not isinstance(payload, dict):
        raise ForgeGovernanceHandoffError("Forge Governance Handoff payload must be an object.")
    if payload.get("version") != FORGE_GOVERNANCE_HANDOFF_VERSION:
        raise ForgeGovernanceHandoffError("Forge Governance Handoff version is unsupported.")
    for name in ("recommendation_set", "selected_recommendation", "decision_evidence", "business_workspace", "governance", "mission_runtime"):
        if name in payload and payload[name] is not None and not isinstance(payload[name], dict):
            raise ForgeGovernanceHandoffError(f"Forge Governance Handoff {name} must be an object when supplied.")
    alternatives = payload.get("alternatives", payload.get("alternative_recommendations"))
    if alternatives is not None:
        if not isinstance(alternatives, list) or not all(isinstance(item, dict) for item in alternatives):
            raise ForgeGovernanceHandoffError("Forge Governance Handoff alternatives must be an array of objects when supplied.")
    selected = payload.get("selected_recommendation")
    if selected is not None and not isinstance(selected, dict):
        raise ForgeGovernanceHandoffError("Forge Governance Handoff selected_recommendation must be an object when supplied.")
    return payload


@dataclass(frozen=True)
class Recommendation:
    recommendation_id: str | None
    rank: int | None
    title: str | None
    mission_origin: str | None
    lifecycle_status: str | None
    business_summary: str | None
    engineering_summary: str | None
    business_value: str | None
    architecture_value: str | None
    engineering_value: str | None
    risk_if_deferred: str | None
    dependencies: tuple[str, ...]
    constraints: tuple[str, ...]
    confidence: str | None
    evidence_references: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: object) -> "Recommendation":
        mapping = raw if isinstance(raw, dict) else {}
        return cls(
            recommendation_id=_value(mapping, "recommendation_id", "id"), rank=_rank(mapping.get("rank")),
            title=_value(mapping, "title", "recommendation_title"), mission_origin=_value(mapping, "mission_origin"),
            lifecycle_status=_value(mapping, "lifecycle_status", "status", "recommendation_status"),
            business_summary=_value(mapping, "business_summary", "summary"), engineering_summary=_value(mapping, "engineering_summary"),
            business_value=_value(mapping, "business_value"), architecture_value=_value(mapping, "architecture_value", "architectural_value"),
            engineering_value=_value(mapping, "engineering_value"), risk_if_deferred=_value(mapping, "risk_if_deferred"),
            dependencies=_items(mapping.get("dependencies")), constraints=_items(mapping.get("constraints")),
            confidence=_value(mapping, "confidence"), evidence_references=_items(mapping.get("evidence_references")),
        )


@dataclass(frozen=True)
class ForgeGovernanceHandoff:
    snapshot: dict[str, object]
    recommendation_set_id: str | None
    recommendation_set_timestamp: str | None
    recommendation_count: int | None
    selected: Recommendation
    alternatives: tuple[Recommendation, ...]
    decision_evidence: dict[str, object] | None
    business_workspace: dict[str, object] | None
    governance: dict[str, object] | None
    mission_runtime: dict[str, object] | None

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, object]) -> "ForgeGovernanceHandoff":
        data = validate_forge_governance_handoff(snapshot)
        recommendation_set = data.get("recommendation_set") if isinstance(data.get("recommendation_set"), dict) else {}
        selected_raw = data.get("selected_recommendation") if isinstance(data.get("selected_recommendation"), dict) else {}
        alternatives_raw = data.get("alternatives", data.get("alternative_recommendations", []))
        alternatives = tuple(Recommendation.from_mapping(item) for item in alternatives_raw if isinstance(item, dict)) if isinstance(alternatives_raw, list) else ()
        count = recommendation_set.get("count", data.get("recommendation_count"))
        return cls(
            snapshot=data, recommendation_set_id=_value(recommendation_set, "id", "recommendation_set_id") or _value(data, "recommendation_set_id"),
            recommendation_set_timestamp=_value(recommendation_set, "timestamp", "created_at") or _value(data, "recommendation_set_timestamp"),
            recommendation_count=count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None,
            selected=Recommendation.from_mapping(selected_raw), alternatives=alternatives,
            decision_evidence=data.get("decision_evidence") if isinstance(data.get("decision_evidence"), dict) else None,
            business_workspace=data.get("business_workspace") if isinstance(data.get("business_workspace"), dict) else None,
            governance=data.get("governance") if isinstance(data.get("governance"), dict) else None,
            mission_runtime=data.get("mission_runtime") if isinstance(data.get("mission_runtime"), dict) else None,
        )

    @property
    def completeness(self) -> str:
        required = (self.selected.recommendation_id, self.selected.title, self.selected.rank, self.selected.lifecycle_status, self.selected.confidence, self.decision_evidence_id, self.alternatives, self.business_approval_state)
        return "COMPLETE" if all(value is not None and value != () for value in required) else "INCOMPLETE"

    @property
    def decision_evidence_id(self) -> str | None:
        return _value(self.decision_evidence or {}, "id", "decision_evidence_id")

    @property
    def business_approval_state(self) -> str | None:
        return _value(self.governance or {}, "business_approval_state")


def _display(value: object) -> str:
    if isinstance(value, tuple):
        return ", ".join(value) or "NOT SUPPLIED BY PRODUCER"
    return str(value) if value is not None else "NOT SUPPLIED BY PRODUCER"


def _provenance(source: str) -> str:
    return f"[Source: {source}]"


def handoff_from_report(report: str) -> dict[str, object] | None:
    """Read the already-rendered immutable snapshot for legacy report detail views."""
    match = _REPORT.search(report) or _LEGACY_REPORT.search(report)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def report_lines(handoff: ForgeGovernanceHandoff | None, execution_status: str) -> tuple[str, ...]:
    """Render only values explicitly supplied in the immutable snapshot."""
    if handoff is None:
        return (
            "## Forge Governance Handoff", "- Forge Governance Handoff: `NOT SUPPLIED BY PRODUCER`",
            "- Governance Status: `FORGE GOVERNANCE HANDOFF NOT SUPPLIED`", "- Decision Evidence: `NOT SUPPLIED BY PRODUCER`",
            "- Governance Handoff Completeness: `INCOMPLETE`", "- Business Review Readiness: `NOT SUPPLIED BY PRODUCER`", "",
        )
    selected = handoff.selected
    evidence = handoff.decision_evidence or {}
    workspace = handoff.business_workspace or {}
    governance = handoff.governance or {}
    runtime = handoff.mission_runtime or {}
    governance_status = _value(governance, "status", "governance_status") or "FORGE GOVERNANCE HANDOFF SUPPLIED"
    readiness = _value(governance, "business_review_readiness", "readiness")
    lines = [
        "## Forge Governance Handoff",
        "Forge owns governance truth. Engineering Platform persists and projects this supplied snapshot immutably; completeness is factual reporting, not governance authority.",
        "- Forge Governance Handoff: `SUPPLIED BY PRODUCER; PERSISTED IMMUTABLY`",
        f"- Governance Status: `{governance_status}` {_provenance('Forge Governance Handoff payload')}",
        f"- Governance Handoff Completeness: `{handoff.completeness}` {_provenance('Forge Governance Handoff payload')}",
        f"- Business Review Readiness: `{_display(readiness)}` {_provenance('Forge Governance Handoff payload')}",
        f"- Recommendation Set ID: `{_display(handoff.recommendation_set_id)}` {_provenance('Forge Governance Handoff payload')}",
        f"- Recommendation Set Timestamp: {_display(handoff.recommendation_set_timestamp)} {_provenance('Forge Governance Handoff payload')}",
        f"- Recommendation Count: {_display(handoff.recommendation_count)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Recommendation ID: `{_display(selected.recommendation_id)}` {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Recommendation Rank: {_display(selected.rank)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Recommendation Title: {_display(selected.title)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Mission Origin: {_display(selected.mission_origin)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Lifecycle Status: {_display(selected.lifecycle_status)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Business Summary: {_display(selected.business_summary)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Engineering Summary: {_display(selected.engineering_summary)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Business Value: {_display(selected.business_value)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Architecture Value: {_display(selected.architecture_value)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Engineering Value: {_display(selected.engineering_value)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Risk If Deferred: {_display(selected.risk_if_deferred)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Dependencies: {_display(selected.dependencies)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Constraints: {_display(selected.constraints)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Confidence: {_display(selected.confidence)} {_provenance('Forge Governance Handoff payload')}",
        f"- Selected Evidence References: {_display(selected.evidence_references)} {_provenance('Forge Governance Handoff payload')}",
        f"- Decision Evidence ID: `{_display(_value(evidence, 'id', 'decision_evidence_id'))}` {_provenance('Forge Governance Handoff payload')}",
        f"- Decision Evidence Type: {_display(_value(evidence, 'type', 'decision_evidence_type'))} {_provenance('Forge Governance Handoff payload')}",
        f"- Decision Evidence Timestamp: {_display(_value(evidence, 'timestamp', 'created_at'))} {_provenance('Forge Governance Handoff payload')}",
        f"- Decision Evidence Confidence: {_display(_value(evidence, 'confidence'))} {_provenance('Forge Governance Handoff payload')}",
        f"- Decision Evidence References: {_display(_items(evidence.get('references')))} {_provenance('Forge Governance Handoff payload')}",
        f"- Business Workspace Availability: {_display(_value(workspace, 'availability', 'state'))} {_provenance('Forge Governance Handoff payload')}",
        f"- Business Workspace Reference: {_display(_value(workspace, 'reference', 'id'))} {_provenance('Forge Governance Handoff payload')}",
        f"- Business Workspace Projected Selected Recommendation ID: {_display(_value(workspace, 'projected_selected_recommendation_id'))} {_provenance('Forge Governance Handoff payload')}",
        f"- Business Workspace Projection Match State: {_display(_value(workspace, 'projection_match_state'))} {_provenance('Forge Governance Handoff payload')}",
        f"- Business Approval State: {_display(_value(governance, 'business_approval_state'))} {_provenance('Forge Governance Handoff payload')}",
        f"- Architecture Approval State: {_display(_value(governance, 'architecture_approval_state'))} {_provenance('Forge Governance Handoff payload')}",
        f"- Mission Candidate ID: `{_display(_value(runtime, 'mission_candidate_id'))}` {_provenance('Forge Governance Handoff payload')}",
        f"- Mission ID: `{_display(_value(runtime, 'mission_id'))}` {_provenance('Forge Governance Handoff payload')}",
        f"- Mission Runtime State: {_display(_value(runtime, 'mission_runtime_state'))} {_provenance('Forge Governance Handoff payload')}",
        f"- Scheduler State: {_display(_value(runtime, 'scheduler_state'))} {_provenance('Forge Governance Handoff payload')}",
        "### Alternatives",
    ]
    if not handoff.alternatives:
        lines.append("- Alternatives: `NOT SUPPLIED BY PRODUCER`")
    for item in handoff.alternatives:
        lines.extend((
            f"- Recommendation ID: `{_display(item.recommendation_id)}`; Rank: {_display(item.rank)}; Title: {_display(item.title)}",
            f"  - Mission Origin: {_display(item.mission_origin)}; Lifecycle Status: {_display(item.lifecycle_status)}",
            f"  - Business Value: {_display(item.business_value)}; Architecture Value: {_display(item.architecture_value)}; Engineering Value: {_display(item.engineering_value)}",
            f"  - Risk If Deferred: {_display(item.risk_if_deferred)}; Dependencies: {_display(item.dependencies)}; Confidence: {_display(item.confidence)} {_provenance('Forge Governance Handoff payload')}",
        ))
    lines.extend(("### Immutable Handoff Snapshot", "```json", json.dumps(handoff.snapshot, sort_keys=True), "```", ""))
    return tuple(lines)
