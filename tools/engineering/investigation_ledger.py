"""Ephemeral factual-deduplication rules for one primary provider invocation.

The ledger deliberately contains fact identifiers and freshness only.  It never
accepts source text, paths, command arguments, tool output, conclusions, or
reviewer advice, and is rendered only into the primary-provider prompt.
"""

from __future__ import annotations

from dataclasses import dataclass


RUN_STABLE = "RUN-STABLE"
MUTABLE = "MUTABLE"
BOUNDARY_SENSITIVE = "BOUNDARY-SENSITIVE"

_FACT_FRESHNESS = {
    "repository_identity": RUN_STABLE,
    "repository_status": MUTABLE,
    "git_ancestry": MUTABLE,
    "pull_request_state": BOUNDARY_SENSITIVE,
    "source_inspection": MUTABLE,
    "test_surface": MUTABLE,
    "validation_surface": BOUNDARY_SENSITIVE,
    "finalization_state": BOUNDARY_SENSITIVE,
    "reconciliation_state": BOUNDARY_SENSITIVE,
}


@dataclass(frozen=True)
class InvocationInvestigationLedger:
    """A small, non-persistent checklist of facts usable in one invocation."""

    completed: frozenset[str] = frozenset()

    def record(self, *facts: str) -> "InvocationInvestigationLedger":
        """Record only known fact identifiers after a real narrow check."""
        unknown = set(facts).difference(_FACT_FRESHNESS)
        if unknown:
            raise ValueError("Unknown investigation fact identifier.")
        return InvocationInvestigationLedger(self.completed.union(facts))

    def reusable(self, fact: str) -> bool:
        """Return whether the fact is currently established in this invocation."""
        if fact not in _FACT_FRESHNESS:
            raise ValueError("Unknown investigation fact identifier.")
        return fact in self.completed

    def invalidate(self, boundary: str) -> "InvocationInvestigationLedger":
        """Fail closed at mutation and lifecycle boundaries.

        RUN-STABLE identity remains valid.  Every other fact must be checked
        again after any boundary that could have changed repository or remote
        state.  This is intentionally conservative: a caller may always do a
        real check sooner when freshness is uncertain.
        """
        if boundary not in {
            "repository_mutation",
            "validation",
            "pull_request_mutation",
            "merge",
            "finalization",
            "repository_cleanup",
            "freshness_uncertain",
        }:
            raise ValueError("Unknown freshness boundary.")
        return InvocationInvestigationLedger(
            frozenset(
                fact for fact in self.completed if _FACT_FRESHNESS[fact] == RUN_STABLE
            )
        )

    def to_prompt_dict(self) -> dict[str, object]:
        """Return an identifier-only primary prompt projection."""
        return {
            "scope": "one_primary_provider_invocation",
            "persistence": "none",
            "completed_fact_ids": sorted(self.completed),
            "fact_freshness": dict(_FACT_FRESHNESS),
            "invalidating_boundaries": [
                "repository_mutation",
                "validation",
                "pull_request_mutation",
                "merge",
                "finalization",
                "repository_cleanup",
                "freshness_uncertain",
            ],
        }
