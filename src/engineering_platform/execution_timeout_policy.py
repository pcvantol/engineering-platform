"""Versioned, host-owned time limits for bounded provider work.

The limits are deliberately not dashboard preferences.  They cap autonomous
provider authority and keep a provider process that leaves its output open
from owning a project queue indefinitely.
"""
from __future__ import annotations

from dataclasses import dataclass


POLICY_VERSION = "provider-timeouts-v1"


@dataclass(frozen=True)
class ProviderTimeout:
    """One named execution boundary and its immutable maximum duration."""

    key: str
    seconds: int


SPECIALIST_REVIEW = ProviderTimeout("specialist_review", 5 * 60)
IMPLEMENTATION = ProviderTimeout("implementation", 15 * 60)
LOCAL_REPOSITORY_VALIDATION = ProviderTimeout("local_repository_validation", 15 * 60)
AUTONOMOUS_QUALITY_CONTROL = ProviderTimeout("autonomous_quality_control", 10 * 60)
REPAIR = ProviderTimeout("repair", 15 * 60)
FINALIZATION = ProviderTimeout("finalization", 15 * 60)
END_RECONCILIATION = ProviderTimeout("end_reconciliation", 10 * 60)

WORKFLOW_TIMEOUTS = (
    SPECIALIST_REVIEW,
    IMPLEMENTATION,
    LOCAL_REPOSITORY_VALIDATION,
    AUTONOMOUS_QUALITY_CONTROL,
    REPAIR,
    FINALIZATION,
    END_RECONCILIATION,
)


def agent_timeout(*, phase: str, repair: bool = False, quality: bool = False,
                  local_validation: bool = False) -> ProviderTimeout:
    """Return the deadline for one primary-provider workflow action."""
    if repair:
        return REPAIR
    if local_validation:
        return LOCAL_REPOSITORY_VALIDATION
    if quality:
        return AUTONOMOUS_QUALITY_CONTROL
    normalized = phase.upper()
    if normalized == "FINALIZATION":
        return FINALIZATION
    if normalized in {"RECONCILE_AGENT", "END_RECONCILIATION"}:
        return END_RECONCILIATION
    return IMPLEMENTATION
