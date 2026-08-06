"""Read-only projection of a Forge Mission Recommendation handoff."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


STATUSES = frozenset({"PROPOSED", "RECOMMENDED", "NOT_RECOMMENDED", "SUPERSEDED", "UNAVAILABLE"})
_MAX_TEXT = 2_000
_ARTIFACT = re.compile(r"(?mi)^\s*Forge Recommendation Art(?:ifact|efact) Path\s*:\s*([^\r\n]+?)\s*$")
_INLINE = re.compile(r"(?ms)^\s*Forge Recommendation Handoff JSON\s*:\s*\n```(?:json)?\s*(\{.*?\})\s*```")
_REPORT = re.compile(r"(?ms)^## Forge Mission Recommendation Handoff\s*$.*?^```json\s*(\{.*?\})\s*```")


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.strip().split())
    return value[:_MAX_TEXT] if value else None


def _list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for raw in value if (item := _text(raw)))[:20]


def _value(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if (value := _text(raw.get(key))) is not None:
            return value
    return None


def _rank(raw: dict[str, Any]) -> int | None:
    value = raw.get("rank")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


@dataclass(frozen=True)
class Recommendation:
    recommendation_id: str | None
    title: str | None
    recommendation_type: str | None
    mission_origin: str | None
    rank: int | None
    summary: str | None
    business_value: str | None
    engineering_value: str | None
    architectural_value: str | None
    expected_outcome: str | None
    repository_impact: str | None
    risk_if_deferred: str | None
    dependencies: tuple[str, ...]
    confidence: str | None
    alternatives_considered: tuple[str, ...]
    decision_evidence: str | None
    timestamp: str | None
    status: str
    ordering_reason: str | None
    supersedes: str | None

    @classmethod
    def from_mapping(cls, raw: object) -> "Recommendation":
        raw = raw if isinstance(raw, dict) else {}
        status = (_value(raw, "status", "recommendation_status") or "UNAVAILABLE").upper()
        return cls(
            recommendation_id=_value(raw, "recommendation_id", "id"), title=_value(raw, "title", "recommendation_title"),
            recommendation_type=_value(raw, "recommendation_type", "type"), mission_origin=_value(raw, "mission_origin"),
            rank=_rank(raw), summary=_value(raw, "summary", "recommendation_summary"),
            business_value=_value(raw, "business_value"), engineering_value=_value(raw, "engineering_value"),
            architectural_value=_value(raw, "architectural_value"), expected_outcome=_value(raw, "expected_outcome"),
            repository_impact=_value(raw, "expected_repository_impact", "repository_impact"),
            risk_if_deferred=_value(raw, "risk_if_deferred"), dependencies=_list(raw.get("dependencies")),
            confidence=_value(raw, "confidence"), alternatives_considered=_list(raw.get("alternatives_considered")),
            decision_evidence=_value(raw, "decision_evidence_reference", "decision_evidence"),
            timestamp=_value(raw, "recommendation_timestamp", "timestamp"), status=status if status in STATUSES else "UNAVAILABLE",
            ordering_reason=_value(raw, "ordering_reason", "rank_reason"), supersedes=_value(raw, "supersedes", "supersedes_recommendation_id"),
        )

    def as_dict(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


@dataclass(frozen=True)
class RecommendationHandoff:
    artifact_path: str | None
    recommendation: Recommendation
    alternatives: tuple[Recommendation, ...]

    @property
    def missing_fields(self) -> tuple[str, ...]:
        fields = (("Recommendation title", self.recommendation.title), ("Decision Evidence reference", self.recommendation.decision_evidence))
        return tuple(label for label, value in fields if value is None)

    @property
    def projection_status(self) -> str:
        return "INCOMPLETE" if self.missing_fields else "COMPLETE"

    def as_dict(self) -> dict[str, object]:
        return {"artifact_path": self.artifact_path, "projection_status": self.projection_status,
                "missing_fields": list(self.missing_fields), "recommendation": self.recommendation.as_dict(),
                "alternatives": [candidate.as_dict() for candidate in self.alternatives]}


def _artifact(root: Path, value: str) -> tuple[str, dict[str, Any]] | None:
    path = Path(value.strip())
    if path.is_absolute() or ".." in path.parts:
        return None
    candidate = root / path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve())
        content = resolved.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = _INLINE.search(content)
        if match is None:
            return None
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return str(path), data if isinstance(data, dict) else {}


def parse_forge_recommendation_handoff(prompt: str, root: Path, *, producer_type: str) -> RecommendationHandoff | None:
    """Consume only explicit Forge metadata or its declared persisted artefact."""
    if producer_type.upper() != "FORGE":
        return None
    artifact_match = _ARTIFACT.search(prompt)
    raw: dict[str, Any] | None = None
    artifact_path: str | None = None
    if artifact_match:
        resolved = _artifact(root, artifact_match.group(1))
        if resolved is None:
            raw = {}
            artifact_path = _text(artifact_match.group(1))
        else:
            artifact_path, raw = resolved
    else:
        inline = _INLINE.search(prompt)
        if inline:
            try:
                decoded = json.loads(inline.group(1))
                raw = decoded if isinstance(decoded, dict) else {}
            except json.JSONDecodeError:
                raw = {}
    if raw is None:
        return None
    recommended = raw.get("recommended_candidate", raw.get("recommendation", raw))
    alternatives = raw.get("alternative_candidates", raw.get("alternatives", []))
    return RecommendationHandoff(artifact_path, Recommendation.from_mapping(recommended),
                                 tuple(Recommendation.from_mapping(item) for item in alternatives if isinstance(item, dict))[:10])


def handoff_from_report(report: str) -> dict[str, object] | None:
    match = _REPORT.search(report)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def report_lines(handoff: RecommendationHandoff | None, execution_status: str) -> tuple[str, ...]:
    if handoff is None:
        return ()
    candidate = handoff.recommendation
    def value(item: str | None) -> str:
        return item if item else "NOT SUPPLIED"
    lines = ["## Forge Mission Recommendation Handoff", "Forge owns recommendation semantics, ranking, Business governance and Mission creation. Engineering Platform projects this immutable handoff read-only.",
             f"- Execution Status: `{execution_status}`", f"- Recommendation Projection: `{handoff.projection_status}`",
             f"- Recommendation Status: `{candidate.status}`", "- Business Decision: `NOT YET RECORDED`", "- Mission Created: `NO`",
             "- Business Approval: `NOT PERFORMED BY ENGINEERING PLATFORM`", "- Mission Allocation: `NOT PERFORMED`",
             f"- Delivered Artefact: `{value(handoff.artifact_path)}`", f"- Recommended Mission: {value(candidate.title)}",
             f"- Recommendation ID: `{value(candidate.recommendation_id)}`", f"- Recommendation Type: {value(candidate.recommendation_type)}",
             f"- Mission Origin: {value(candidate.mission_origin)}", f"- Rank: {candidate.rank if candidate.rank is not None else 'NOT SUPPLIED'}",
             f"- Recommendation Summary: {value(candidate.summary)}", f"- Business Value: {value(candidate.business_value)}",
             f"- Engineering Value: {value(candidate.engineering_value)}", f"- Architectural Value: {value(candidate.architectural_value)}",
             f"- Expected Outcome: {value(candidate.expected_outcome)}", f"- Expected Repository Impact: {value(candidate.repository_impact)}",
             f"- Risk If Deferred: {value(candidate.risk_if_deferred)}", f"- Dependencies: {', '.join(candidate.dependencies) or 'NOT SUPPLIED'}",
             f"- Confidence: {value(candidate.confidence)}", f"- Alternatives Considered: {', '.join(candidate.alternatives_considered) or 'NOT SUPPLIED'}",
             f"- Decision Evidence: {value(candidate.decision_evidence)}", f"- Recommendation Timestamp: {value(candidate.timestamp)}",
             f"- Supersedes: {value(candidate.supersedes)}"]
    if handoff.missing_fields:
        lines.append("- Missing Contract Fields: " + ", ".join(handoff.missing_fields))
    if handoff.alternatives:
        lines.extend(("### Alternative Candidates", *(f"- Rank {item.rank if item.rank is not None else 'NOT SUPPLIED'}: {value(item.title)} — {value(item.ordering_reason)}" for item in handoff.alternatives)))
    lines.extend(("### Handoff Data", "```json", json.dumps(handoff.as_dict(), sort_keys=True), "```", ""))
    return tuple(lines)
