"""Deterministic, read-only capability reviewer selection for dj-engineer."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Protocol

from .agent_state import redact_diagnostic


REVIEWER_ORDER = (
    "repository_governance",
    "validation",
    "documentation",
    "finalization",
)
REVIEWER_LABELS = {
    "repository_governance": "Repository Governance Reviewer",
    "validation": "Validation Reviewer",
    "documentation": "Documentation Reviewer",
    "finalization": "Finalization Reviewer",
}


@dataclass(frozen=True)
class ReviewerSelection:
    reviewer: str
    selected_because: str
    confidence: float


@dataclass(frozen=True)
class ReviewerResult:
    reviewer: str
    contribution: str
    recommendations: tuple[str, ...] = ()
    failed: bool = False


class ReviewerClient(Protocol):
    def review(self, root: Path, selection: ReviewerSelection, objective: str) -> ReviewerResult: ...


def select_reviewers(objective: str, prompt_path: Path, transaction_kind: str, memory: object) -> tuple[ReviewerSelection, ...]:
    """Select only registered reviewers from objective, lifecycle and safe memory evidence."""
    text = f"{prompt_path.name} {objective}".lower()
    selected: dict[str, str] = {}
    if any(token in text for token in ("governance", "repository", "roadmap", "policy", "bootstrap")):
        selected["repository_governance"] = "repository-governance objective"
    if any(token in text for token in ("test", "ruff", "bandit", "assurance", "validation", "failure")):
        selected["validation"] = "validation-related objective"
    if prompt_path.suffix.lower() == ".md" or any(token in text for token in ("document", "wording", "readme", "backlog")):
        selected["documentation"] = "documentation-oriented objective"
    if transaction_kind == "FINALIZATION" or "finalization" in text:
        selected["finalization"] = "Finalization lifecycle evidence"
    history = _reviewer_memory(memory)
    result: list[ReviewerSelection] = []
    for reviewer in REVIEWER_ORDER:
        reason = selected.get(reviewer)
        if reason is None:
            continue
        confidence = min(1.0, 0.5 + history.get(reviewer, 0.0))
        result.append(ReviewerSelection(reviewer, reason, confidence))
    return tuple(result)


def run_reviews(root: Path, selections: tuple[ReviewerSelection, ...], objective: str, client: ReviewerClient | None) -> tuple[ReviewerResult, ...]:
    """Run independent read-only reviews in parallel; any failure remains advisory."""
    if not selections:
        return ()
    if client is None:
        return tuple(ReviewerResult(item.reviewer, "Reviewer client unavailable; primary review continues.", failed=True) for item in selections)

    def invoke(selection: ReviewerSelection) -> ReviewerResult:
        try:
            result = client.review(root, selection, objective)
            if result.reviewer != selection.reviewer:
                return ReviewerResult(selection.reviewer, "Reviewer identity mismatch; primary review continues.", failed=True)
            return ReviewerResult(
                selection.reviewer,
                redact_diagnostic(result.contribution, limit=240),
                tuple(redact_diagnostic(value, limit=240) for value in result.recommendations[:3]),
                result.failed,
            )
        except Exception:  # Reviewer failure is advisory and cannot block the transaction.
            return ReviewerResult(selection.reviewer, "Reviewer failed; primary review continues.", failed=True)

    with ThreadPoolExecutor(max_workers=len(selections)) as executor:
        completed = {item.reviewer: executor.submit(invoke, item) for item in selections}
        return tuple(completed[item.reviewer].result() for item in selections)


def reconciled_recommendations(results: tuple[ReviewerResult, ...]) -> tuple[str, ...]:
    """Deduplicate safe advisory recommendations for the primary agent prompt."""
    accepted: list[str] = []
    for result in results:
        if result.failed:
            continue
        for recommendation in result.recommendations:
            if recommendation and recommendation not in accepted:
                accepted.append(recommendation)
    return tuple(accepted[:8])


def records_for_storage(selections: tuple[ReviewerSelection, ...], results: tuple[ReviewerResult, ...]) -> tuple[dict[str, object], ...]:
    by_reviewer = {result.reviewer: result for result in results}
    records: list[dict[str, object]] = []
    for selection in selections:
        result = by_reviewer.get(selection.reviewer, ReviewerResult(selection.reviewer, "No result.", failed=True))
        records.append({
            "reviewer": selection.reviewer,
            "selected_because": selection.selected_because,
            "confidence": selection.confidence,
            "contribution": result.contribution,
            "accepted_recommendations": len(result.recommendations) if not result.failed else 0,
            "rejected_recommendations": 0,
            "failed": result.failed,
        })
    return tuple(records)


def _reviewer_memory(memory: object) -> dict[str, float]:
    if not isinstance(memory, dict):
        return {}
    result: dict[str, float] = {}
    for entry in memory.get("reviewers", []):
        if isinstance(entry, dict) and entry.get("reviewer") in REVIEWER_ORDER:
            confidence = entry.get("future_confidence")
            if isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0:
                result[str(entry["reviewer"])] = float(confidence)
    return result


def reviewer_prompt(selection: ReviewerSelection, objective: str) -> str:
    """Build the bounded read-only reviewer instruction without lifecycle authority."""
    return json.dumps({
        "reviewer": REVIEWER_LABELS[selection.reviewer],
        "selected_because": selection.selected_because,
        "objective": objective,
        "authority": "Read-only inspection and recommendations only. Do not edit, commit, push, merge, create pull requests, finalize, or change lifecycle state.",
    }, sort_keys=True)
