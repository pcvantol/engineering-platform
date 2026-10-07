"""Bounded, role-specific provider context for Engineering Platform work.

The execution host remains the authority for lifecycle and deterministic
admission.  This module only decides whether a *provider action* is meaningful
and projects the already-authoritative prompt into a role-appropriate input.
It never stores prompt text or command output.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .provider_context_scope import POLICY_ID


class ProviderRole(StrEnum):
    SPECIALIST_REVIEW = "SPECIALIST_REVIEW"
    IMPLEMENTATION = "IMPLEMENTATION"
    QUALITY_REVIEW = "QUALITY_REVIEW"
    SECURITY_REVIEW = "SECURITY_REVIEW"
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
    budget_version: str = POLICY_ID

    @property
    def telemetry(self) -> dict[str, int]:
        return {
            "context_source_item_count": self.source_item_count,
            "context_omitted_low_priority_count": self.omitted_low_priority_count,
            "context_projected_bytes": len(self.text.encode("utf-8")),
            "context_budget_overflow_bytes": max(
                0, len(self.text.encode("utf-8")) - _ROLE_BUDGETS[self.role]
            ),
        }


_ROLE_BUDGETS = {
    ProviderRole.SPECIALIST_REVIEW: 18_000,
    ProviderRole.IMPLEMENTATION: 60_000,
    ProviderRole.QUALITY_REVIEW: 22_000,
    ProviderRole.SECURITY_REVIEW: 22_000,
    ProviderRole.REPAIR: 18_000,
    ProviderRole.FINALIZATION: 18_000,
}
def provider_need_for_phase(phase: str, *, passive_observation: bool = False) -> ProviderNeedDecision:
    """Make provider need explicit; passive/deterministic phases never need one."""
    if passive_observation:
        return ProviderNeedDecision(False, "passive observation is deterministic")
    if phase == "EXECUTE_AGENT":
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
    """Preserve the complete authoritative objective for every provider role.

    Markdown text cannot prove that a section contains only optional history.
    The approved objective remains one indivisible input; role-specific rubrics
    and authority are supplied separately by the host. Nominal role-budget
    overflow is observable, never permission to omit safety or acceptance text.
    This safety subset makes no historical-context/token-reduction claim.
    """
    return ContextProjection(role, objective, 1, 0)
