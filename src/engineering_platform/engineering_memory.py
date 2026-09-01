"""Local, advisory Engineering Memory persistence.

This module deliberately owns only bounded local memory.  The runner remains
the lifecycle orchestrator and repository evidence remains authoritative.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from .agent_state import TransactionState


def _memory_path(root: Path) -> Path:
    return root / ".engineering" / "memory" / "engineering-memory.json"


def load_engineering_memory(root: Path) -> dict[str, object]:
    try:
        raw = json.loads(_memory_path(root).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def retrieve_engineering_memory(root: Path, prompt_path: Path) -> str:
    """Return safe advisory metadata; repository evidence remains authoritative."""
    try:
        entries = load_engineering_memory(root).get("transactions", [])
    except AttributeError:
        return "\n\nEngineering Memory: no prior safe transaction metadata is available."
    objective = prompt_path.stem.lower()
    relevant = [
        entry
        for entry in entries[-10:]
        if any(word in objective for word in entry.get("classification", "").split())
    ]
    return (
        "\n\nEngineering Memory (advisory only; repository evidence overrides it): "
        + json.dumps(relevant[-3:], sort_keys=True)
    )


def capture_engineering_memory(
    root: Path, state: TransactionState, reviewer_records: tuple[dict[str, object], ...] = ()
) -> None:
    """Atomically store bounded metadata, never prompts, source content or credentials."""
    path = _memory_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = load_engineering_memory(root)
    classification = " ".join(
        part
        for part in Path(state.prompt_path).stem.lower().replace("_", "-").split("-")
        if part.isalpha()
    )[:120]
    entry = {
        "classification": classification,
        "repository": state.repository,
        "outcome": state.phase,
        "repair_iterations": state.repair_iterations,
        "implementation_pr": state.implementation_pull_request,
        "finalization_pr": state.finalization_pull_request,
        "confidence": 1.0,
        "usage_count": 0,
        "last_successful_use": datetime.now(timezone.utc).isoformat(),
    }
    reviewer_index = {
        item.get("reviewer"): dict(item)
        for item in raw.get("reviewers", [])
        if isinstance(item, dict) and isinstance(item.get("reviewer"), str)
    }
    for record in reviewer_records:
        reviewer = record.get("reviewer")
        if not isinstance(reviewer, str):
            continue
        previous = reviewer_index.get(reviewer, {})
        usage = int(previous.get("usage_count", 0)) + 1
        successful = int(previous.get("successful_outcomes", 0)) + (
            0 if record.get("failed") else 1
        )
        accepted = int(previous.get("accepted_recommendations", 0)) + int(
            record.get("accepted_recommendations", 0)
        )
        recommended = (
            int(previous.get("recommendation_count", 0))
            + int(record.get("accepted_recommendations", 0))
            + int(record.get("rejected_recommendations", 0))
        )
        confidence = round(successful / usage, 2)
        reviewer_index[reviewer] = {
            "reviewer": reviewer,
            "capability": record.get("capability", "engineering"),
            "usage_count": usage,
            "successful_outcomes": successful,
            "accepted_recommendations": accepted,
            "recommendation_count": recommended,
            "recommendation_acceptance_rate": round(accepted / recommended, 2)
            if recommended
            else 0.0,
            "average_duration": 0,
            "last_successful_use": datetime.now(timezone.utc).isoformat()
            if not record.get("failed")
            else previous.get("last_successful_use"),
            "future_confidence": confidence,
        }
    reviewers = list(reviewer_index.values())[-50:]
    raw = {
        "schema_version": 2,
        "transactions": [item for item in raw.get("transactions", []) if isinstance(item, dict)][
            -49:
        ]
        + [entry],
        "reviewers": reviewers,
        "capability_metrics": {
            "most_frequently_used": max(reviewers, key=lambda item: item["usage_count"])["reviewer"]
            if reviewers
            else None,
            "highest_value": max(reviewers, key=lambda item: item["future_confidence"])["reviewer"]
            if reviewers
            else None,
            "repository_areas": sorted(
                {str(item.get("capability", "engineering")) for item in reviewers}
            ),
        },
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".memory.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
