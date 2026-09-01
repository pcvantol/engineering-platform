"""Provider-neutral, current-delta-first execution context policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


POLICY_ID = "provider-context-v1"
MAX_HISTORICAL_COMMITS = 10
MAX_HISTORICAL_PULL_REQUESTS = 10
MAX_HISTORICAL_CONTEXT_BYTES = 65_536


class ContextScope(StrEnum):
    NORMAL = "NORMAL"
    RETRY_REPAIR = "RETRY_REPAIR"
    INVESTIGATION = "INVESTIGATION"


class ContextEscalationReason(StrEnum):
    REGRESSION_ORIGIN_UNKNOWN = "REGRESSION_ORIGIN_UNKNOWN"
    MERGE_ANCESTRY_REQUIRED = "MERGE_ANCESTRY_REQUIRED"
    DIRECT_LINEAGE_REQUIRED = "DIRECT_LINEAGE_REQUIRED"
    CONTRACT_HISTORY_REQUIRED = "CONTRACT_HISTORY_REQUIRED"
    BLAME_REQUIRED = "BLAME_REQUIRED"
    OPERATOR_REQUESTED_AUDIT = "OPERATOR_REQUESTED_AUDIT"
    OTHER_BOUNDED_INVESTIGATION = "OTHER_BOUNDED_INVESTIGATION"


class HistoryBoundaryKind(StrEnum):
    COMMITS_TOUCHING_PATH = "COMMITS_TOUCHING_PATH"
    DIRECT_ANCESTRY = "DIRECT_ANCESTRY"
    DIRECT_PREDECESSOR = "DIRECT_PREDECESSOR"
    REFERENCED_PULL_REQUESTS = "REFERENCED_PULL_REQUESTS"


@dataclass(frozen=True)
class ContextEscalationRequest:
    """One bounded, invocation-local admission to historical evidence."""

    reason: ContextEscalationReason
    boundary_kind: HistoryBoundaryKind
    boundary: str
    limit: int
    diagnostic: str

    def validate(self) -> "ContextEscalationRequest":
        if not self.boundary.strip() or len(self.boundary) > 240:
            raise ValueError("A non-empty bounded history boundary is required.")
        if not self.diagnostic.strip() or len(self.diagnostic) > 240:
            raise ValueError("A bounded evidence-gap diagnostic is required.")
        maximum = (
            MAX_HISTORICAL_PULL_REQUESTS
            if self.boundary_kind == HistoryBoundaryKind.REFERENCED_PULL_REQUESTS
            else MAX_HISTORICAL_COMMITS
        )
        if not 1 <= self.limit <= maximum:
            raise ValueError(f"History limit must be between 1 and {maximum}.")
        return self


def initial_context_scope(*, phase: str, repair_iterations: int = 0, objective: str = "") -> ContextScope:
    """Choose scope from lifecycle evidence, never producer identity."""
    if phase == "REPAIR_AGENT" or repair_iterations > 0:
        return ContextScope.RETRY_REPAIR
    # An explicit audit/history task is itself the bounded objective.  This is
    # intentionally narrow; ordinary mentions of history do not broaden scope.
    if re.search(r"\b(?:historical\s+(?:audit|investigation)|(?:audit|investigate)\s+(?:history|historical))\b", objective, re.I):
        return ContextScope.INVESTIGATION
    return ContextScope.NORMAL


def provider_instruction(scope: ContextScope) -> str:
    """Return the short operational contract injected into every provider turn."""
    direct_lineage = (
        "Direct predecessor run, terminal diagnostic, failed controls, and its direct delta are admitted; do not load older ancestors."
        if scope == ContextScope.RETRY_REPAIR
        else "No predecessor lineage is admitted unless the lifecycle supplies it."
    )
    return (
        f"Provider Context Scope: {scope.value}; Policy: {POLICY_ID}. "
        "Start with the supplied objective, current branch/worktree/status, merge-base delta against canonical base, current source, relevant tests and configuration. "
        "Do not enumerate historical pull requests or broad git history for orientation. "
        f"{direct_lineage} "
        "If current evidence has a concrete gap, before a historical query run "
        "`djconnect-context-escalate REASON BOUNDARY_KIND BOUNDARY LIMIT --diagnostic 'evidence gap'`; "
        "use only its admitted boundary (maximum 10 commits or 10 PRs) and continue."
    )
