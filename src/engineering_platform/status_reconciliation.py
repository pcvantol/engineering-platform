"""Fail-closed recognition of a stale rolling-status preflight block."""
from __future__ import annotations

import re

from .agent_state import TransactionState


_ROLLING_RECORDS = re.compile(r"\brolling(?:\s+status)?\s+records?\b", re.IGNORECASE)
_STALE_STATUS_INDICATOR = re.compile(
    r"\b(?:finalization|reconcil(?:e|iation|ed)|pending|stale|state)\b",
    re.IGNORECASE,
)


def is_stale_rolling_status_block(state: TransactionState) -> bool:
    """Recognize only the no-PR preflight block eligible for Finalization.

    Diagnostics are advisory and are never delivery evidence.  They merely
    identify the already blocked, no-PR transaction for the existing dedicated
    governance-only Finalization path.  Any run-scoped PR evidence, a different
    terminal state, or an unrelated diagnostic remains fail-closed.
    """
    diagnostic = state.diagnostic or ""
    return (
        state.phase == "BLOCKED"
        and state.terminal_condition == "external_blocked"
        and state.transaction_kind == "IMPLEMENTATION"
        and state.pull_request is None
        and state.implementation_pull_request is None
        and state.finalization_pull_request is None
        and bool(_ROLLING_RECORDS.search(diagnostic))
        and bool(_STALE_STATUS_INDICATOR.search(diagnostic))
    )
