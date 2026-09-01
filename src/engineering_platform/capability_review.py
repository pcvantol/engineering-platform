"""Deterministic, read-only capability reviewer selection for the Execution Host."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Callable, Protocol

from .agent_state import redact_diagnostic
from .provider_context import ProviderRole, project_context
from .provider_context_scope import ContextScope, POLICY_ID, provider_instruction
from .reviewer_evidence import ReviewerEvidence


REVIEWER_ORDER = (
    "apple_platform",
    "windows_platform",
    "home_assistant_integration",
    "esphome_firmware",
    "pi_renderer",
    "universal_receiver",
    "website",
    "api",
    "repository_governance",
    "validation",
    "documentation",
    "finalization",
)
REVIEWER_LABELS = {
    "apple_platform": "Apple Platform Reviewer",
    "windows_platform": "Windows Platform Reviewer",
    "home_assistant_integration": "Home Assistant Integration Reviewer",
    "esphome_firmware": "ESPHome Firmware Reviewer",
    "pi_renderer": "Pi Renderer Reviewer",
    "universal_receiver": "Universal Receiver Reviewer",
    "website": "Website Reviewer",
    "api": "API Reviewer",
    "repository_governance": "Repository Governance Reviewer",
    "validation": "Validation Reviewer",
    "documentation": "Documentation Reviewer",
    "finalization": "Finalization Reviewer",
}
PRODUCT_MATCHERS = {
    "apple_platform": (("apps/apple/", "djconnect-app", "swiftui", "watchos", "macos", "ios"), "Apple platform capability"),
    "windows_platform": (("apps/windows/", "djconnect-windows", "maui", "windows packaging"), "Windows platform capability"),
    "home_assistant_integration": (("custom_components/djconnect", "home assistant", "config flow", "options flow", "coordinator", "entity model"), "Home Assistant integration capability"),
    "esphome_firmware": (("esphome", "djconnect-esp32", "firmware yaml", ".yaml"), "ESPHome firmware capability"),
    "pi_renderer": (("djconnect-pi", "pi renderer", "raspberry pi", "display lifecycle"), "Pi renderer capability"),
    "universal_receiver": (("universal receiver", "vibecast", "browser receiver", "receiver transport"), "Universal Receiver capability"),
    "website": (("djconnect-website", "website", "static site", "product messaging"), "Website capability"),
    "api": (("djconnect-api", "rest api", "api contract", "api documentation"), "API capability"),
}


@dataclass(frozen=True)
class ReviewerSelection:
    reviewer: str
    selected_because: str
    confidence: float
    capability: str = "engineering"


@dataclass(frozen=True)
class ReviewerResult:
    reviewer: str
    contribution: str
    recommendations: tuple[str, ...] = ()
    failed: bool = False
    usage: dict[str, object] = field(default_factory=dict)
    runtime_metadata: dict[str, object] = field(default_factory=dict)
    churn: dict[str, object] = field(default_factory=dict)
    duration_seconds: float | None = None
    usage_snapshots: tuple[dict[str, int], ...] = ()


class ReviewerClient(Protocol):
    def review(
        self,
        root: Path,
        selection: ReviewerSelection,
        objective: str,
        evidence: ReviewerEvidence | None = None,
    ) -> ReviewerResult: ...


def select_reviewers(objective: str, prompt_path: Path, transaction_kind: str, memory: object) -> tuple[ReviewerSelection, ...]:
    """Select only registered reviewers from objective, lifecycle and safe memory evidence."""
    text = f"{prompt_path.name} {objective}".lower()
    selected: dict[str, str] = {}
    for reviewer, (markers, reason) in PRODUCT_MATCHERS.items():
        if any(marker in text for marker in markers):
            selected[reviewer] = reason
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
        capability = reviewer if reviewer in PRODUCT_MATCHERS else "engineering"
        result.append(ReviewerSelection(reviewer, reason, confidence, capability))
    return tuple(result)


ReviewerProgressCallback = Callable[[ReviewerSelection, str, ReviewerResult | None], None]


def run_reviews(
    root: Path,
    selections: tuple[ReviewerSelection, ...],
    objective: str,
    client: ReviewerClient | None,
    progress: ReviewerProgressCallback | None = None,
    evidence: ReviewerEvidence | None = None,
) -> tuple[ReviewerResult, ...]:
    """Run independent read-only reviews in parallel; any failure remains advisory."""
    if not selections:
        return ()
    if client is None:
        results = tuple(ReviewerResult(item.reviewer, "Reviewer client unavailable; primary review continues.", failed=True) for item in selections)
        if progress:
            for selection, result in zip(selections, results, strict=True):
                progress(selection, "failed", result)
        return results

    def invoke(selection: ReviewerSelection) -> ReviewerResult:
        if progress:
            progress(selection, "started", None)
        try:
            result = client.review(root, selection, objective, evidence)
            if result.reviewer != selection.reviewer:
                result = ReviewerResult(selection.reviewer, "Reviewer identity mismatch; primary review continues.", failed=True)
            else:
                result = ReviewerResult(
                selection.reviewer,
                redact_diagnostic(result.contribution, limit=240),
                tuple(redact_diagnostic(value, limit=240) for value in result.recommendations[:3]),
                result.failed,
                result.usage,
                result.runtime_metadata,
                result.churn,
                result.duration_seconds,
            )
        except Exception:  # Reviewer failure is advisory and cannot block the transaction.
            result = ReviewerResult(selection.reviewer, "Reviewer failed; primary review continues.", failed=True)
        if progress:
            progress(selection, "failed" if result.failed else "completed", result)
        return result

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
            "capability": selection.capability,
            "selected_because": selection.selected_because,
            "confidence": selection.confidence,
            "contribution": result.contribution,
            "accepted_recommendations": len(result.recommendations) if not result.failed else 0,
            "rejected_recommendations": 0,
            "failed": result.failed,
            "codex_commands_executed": _command_count(result.churn),
        })
    return tuple(records)


def _command_count(churn: object) -> int:
    """Return the safe, per-review command total without sharing agent state."""
    if not isinstance(churn, dict):
        return 0
    value = churn.get("tool_loop_operations", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


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


def reviewer_prompt(
    selection: ReviewerSelection,
    objective: str,
    evidence: ReviewerEvidence | None = None,
) -> str:
    """Build the bounded read-only reviewer instruction without lifecycle authority."""
    projection = project_context(ProviderRole.SPECIALIST_REVIEW, objective)
    prompt: dict[str, object] = {
        "reviewer": REVIEWER_LABELS[selection.reviewer],
        "capability": selection.capability,
        "selected_because": selection.selected_because,
        "objective": projection.text,
        "context_projection": {
            "role": projection.role.value,
            "budget_version": projection.budget_version,
            "source_item_count": projection.source_item_count,
            "omitted_low_priority_count": projection.omitted_low_priority_count,
        },
        "provider_context_scope": {
            "policy": POLICY_ID,
            "initial_scope": ContextScope.NORMAL.value,
            "instruction": provider_instruction(ContextScope.NORMAL),
        },
        "authority": "Read-only inspection and recommendations only. Do not edit, commit, push, merge, create pull requests, finalize, or change lifecycle state.",
        "scope": "Analyse only the declared capability. Cross-capability analysis requires objective repository evidence.",
    }
    if evidence is not None:
        prompt["run_scoped_repository_evidence"] = evidence.to_dict()
        prompt["evidence_instructions"] = (
            "These are host-observed facts for this exact Run ID, collected after "
            "synchronization and before this reviewer wave. Reuse them for ordinary "
            "repository-state questions; do not rediscover branch, HEAD, worktree, "
            "repository identity, or main ancestry with Git/GitHub. They are facts, "
            "not conclusions. Retrieve only narrower additional evidence that your "
            "capability review genuinely requires. This snapshot expires at any "
            "repository mutation, validation, PR/merge, finalization, or cleanup boundary."
        )
    prompt["invocation_read_reuse"] = (
        "Within this one reviewer invocation, reuse already inspected immutable file "
        "content for factual inspection rather than accidentally rereading it. "
        "Do not share content, conclusions, or reasoning with another reviewer or "
        "Run ID. Reread after an edit, generated-artifact refresh, repository "
        "change, validation, PR/merge, finalization, cleanup, or whenever freshness "
        "is uncertain. Deliberate verification reads remain required."
    )
    return json.dumps(prompt, sort_keys=True)
