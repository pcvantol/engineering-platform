"""Bounded, role-specific provider context for Engineering Platform work.

The execution host remains the authority for lifecycle and deterministic
admission.  This module only decides whether a *provider action* is meaningful
and projects the already-authoritative prompt into a role-appropriate input.
It never stores prompt text or command output.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


class ProviderRole(StrEnum):
    SPECIALIST_REVIEW = "SPECIALIST_REVIEW"
    IMPLEMENTATION = "IMPLEMENTATION"
    QUALITY_REVIEW = "QUALITY_REVIEW"
    REPAIR = "REPAIR"
    FINALIZATION = "FINALIZATION"


@dataclass(frozen=True)
class ProviderNeedDecision:
    required: bool
    reason: str


@dataclass(frozen=True)
class ContextProjection:
    role: ProviderRole
    text: str
    source_item_count: int
    omitted_low_priority_count: int
    budget_version: str = "provider-context-v1"

    @property
    def telemetry(self) -> dict[str, int]:
        return {
            "context_source_item_count": self.source_item_count,
            "context_omitted_low_priority_count": self.omitted_low_priority_count,
            "context_projected_bytes": len(self.text.encode("utf-8")),
        }


_ROLE_BUDGETS = {
    ProviderRole.SPECIALIST_REVIEW: 18_000,
    ProviderRole.IMPLEMENTATION: 60_000,
    ProviderRole.QUALITY_REVIEW: 22_000,
    ProviderRole.REPAIR: 18_000,
    ProviderRole.FINALIZATION: 18_000,
}
_MANDATORY_HEADINGS = re.compile(
    r"\b(?:objective|doel|acceptance|acceptatie|constraint|beperking|"
    r"safety|veilig|authority|autoriteit|validation|validatie|required|"
    r"verplicht|non-negotiable|niet-onderhandelbaar|scope|niet wijzigen|do not)\b",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def provider_need_for_phase(phase: str, *, passive_observation: bool = False) -> ProviderNeedDecision:
    """Make provider need explicit; passive/deterministic phases never need one."""
    if passive_observation:
        return ProviderNeedDecision(False, "passive observation is deterministic")
    if phase in {"EXECUTE_AGENT", "LOCAL_REPOSITORY_VALIDATION"}:
        return ProviderNeedDecision(True, "bounded implementation work requires reasoning")
    if phase == "QUALITY_CONTROL_AGENT":
        return ProviderNeedDecision(True, "autonomous quality review requires reasoning")
    if phase == "REPAIR_AGENT":
        return ProviderNeedDecision(True, "scoped repair requires reasoning")
    if phase in {"FINALIZE_AGENT", "RECONCILE_AGENT"}:
        return ProviderNeedDecision(True, "bounded finalization work requires reasoning")
    return ProviderNeedDecision(False, "deterministic lifecycle transition")


def role_for_phase(phase: str, *, repair: bool = False, quality: bool = False) -> ProviderRole:
    if repair or phase == "REPAIR_AGENT":
        return ProviderRole.REPAIR
    if quality or phase == "QUALITY_CONTROL_AGENT":
        return ProviderRole.QUALITY_REVIEW
    if phase in {"FINALIZE_AGENT", "RECONCILE_AGENT"}:
        return ProviderRole.FINALIZATION
    return ProviderRole.IMPLEMENTATION


def project_context(role: ProviderRole, objective: str) -> ContextProjection:
    """Keep all mandatory sections while omitting lower-priority prompt history.

    Prompts without recognisable Markdown sections are deliberately retained in
    full: safety beats an unproven token reduction.  Initial implementation
    also receives the complete prompt once; downstream roles receive the
    mandatory contract rather than replaying the complete transcript.
    """
    if role == ProviderRole.IMPLEMENTATION:
        return ContextProjection(role, objective, 1, 0)
    sections = _markdown_sections(objective)
    selected = [section for heading, section in sections if heading == "preamble" or _MANDATORY_HEADINGS.search(heading)]
    if not selected:
        return ContextProjection(role, objective, 1, 0)
    budget = _ROLE_BUDGETS[role]
    included: list[str] = []
    used = 0
    for section in selected:
        size = len(section.encode("utf-8"))
        # Mandatory material is never silently truncated.  A single oversized
        # mandatory section is kept whole and may exceed the nominal budget.
        if included and used + size > budget:
            continue
        included.append(section)
        used += size
    text = "\n\n".join(included)
    return ContextProjection(role, text, len(sections), max(0, len(sections) - len(included)))


def _markdown_sections(value: str) -> list[tuple[str, str]]:
    lines = value.splitlines()
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match:
            starts.append((index, match.group(1)))
    if not starts:
        return []
    result: list[tuple[str, str]] = []
    if starts[0][0]:
        preamble = "\n".join(lines[:starts[0][0]]).strip()
        if preamble:
            result.append(("preamble", preamble))
    for ordinal, (start, heading) in enumerate(starts):
        end = starts[ordinal + 1][0] if ordinal + 1 < len(starts) else len(lines)
        result.append((heading, "\n".join(lines[start:end]).strip()))
    return result
