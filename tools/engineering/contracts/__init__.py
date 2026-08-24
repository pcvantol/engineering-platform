"""Stable, read-only contracts for external Engineering Platform consumers.

The contract layer projects canonical Engineering Platform evidence.  It is
deliberately downstream from lifecycle code and contains no action executor.
"""

from .models import ActionAuditRecord, ActionPolicyDecision, AllowedAction, EvidenceReference
from .projection import evaluate_action, get_allowed_actions, get_run_context

__all__ = (
    "ActionAuditRecord",
    "ActionPolicyDecision",
    "AllowedAction",
    "EvidenceReference",
    "evaluate_action",
    "get_allowed_actions",
    "get_run_context",
)
