"""Versioned, host-owned time limits for bounded provider work.

The limits are deliberately not dashboard preferences.  Primary provider
actions use both an inactivity limit and an absolute maximum.  Observable
command, file, tool, web, or terminal-result progress may extend an action
beyond the inactivity limit, but never beyond the absolute maximum.  This
keeps productive work alive without allowing output spam or an inherited open
pipe to own a project queue indefinitely.
"""
from __future__ import annotations

from dataclasses import dataclass


POLICY_VERSION = "provider-timeouts-v2"


@dataclass(frozen=True)
class ProviderTimeout:
    """One named progress-aware execution boundary."""

    key: str
    # Existing consumers use ``seconds`` for the visible per-action limit. In
    # v2 it is explicitly the maximum interval without observable progress.
    seconds: int
    maximum_seconds: int

    def __post_init__(self) -> None:
        if self.seconds <= 0 or self.maximum_seconds < self.seconds:
            raise ValueError("provider timeout boundaries are invalid")


SPECIALIST_REVIEW = ProviderTimeout("specialist_review", 5 * 60, 5 * 60)
IMPLEMENTATION = ProviderTimeout("implementation", 15 * 60, 45 * 60)
LOCAL_REPOSITORY_VALIDATION = ProviderTimeout("local_repository_validation", 15 * 60, 45 * 60)
AUTONOMOUS_QUALITY_CONTROL = ProviderTimeout("autonomous_quality_control", 10 * 60, 30 * 60)
REPAIR = ProviderTimeout("repair", 15 * 60, 45 * 60)
FINALIZATION = ProviderTimeout("finalization", 15 * 60, 45 * 60)
END_RECONCILIATION = ProviderTimeout("end_reconciliation", 10 * 60, 30 * 60)

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
