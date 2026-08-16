"""Read-only, run-scoped execution lifecycle projection for the console.

This module deliberately projects persisted Execution Host checkpoints only.  It
does not coordinate transitions, repair, retry, resume, liveness, or telemetry.
"""
from __future__ import annotations

import json
from pathlib import Path

from .storage import EngineeringStorageError, open_storage


TERMINAL = frozenset({"COMPLETE", "BLOCKED", "FAILED"})
_MANAGED_PATH = (
    "START", "INITIALIZE", "EXECUTE_AGENT", "REPAIR_AGENT",
    "WAIT_FOR_OPERATOR_MERGE", "FINALIZE_AGENT", "REPOSITORY_CLEANUP", "TERMINAL",
)
# Genesis has no pull-request merge boundary.  This is presentation of the
# existing mode contract, not a new execution sequence.
_GENESIS_PATH = (
    "START", "INITIALIZE", "EXECUTE_AGENT", "REPAIR_AGENT",
    "FINALIZE_AGENT", "REPOSITORY_CLEANUP", "TERMINAL",
)

# This is a presentation-only association. The Execution Host stays the
# authority for phase timing; this projection only groups persisted evidence
# under the corresponding visible lifecycle step.
_STEP_PHASES = {
    "START": frozenset({"QUEUE_WAIT", "SUBMISSION_CLAIM"}),
    "INITIALIZE": frozenset({"INITIALIZATION", "HOST_PREFLIGHT", "WORKSPACE_PREFLIGHT", "CAPABILITY_PREFLIGHT"}),
    "EXECUTE_AGENT": frozenset({"EXECUTION_PREPARATION", "PROVIDER_EXECUTION", "VALIDATION"}),
    "REPAIR_AGENT": frozenset({"REPAIR"}),
    "WAIT_FOR_OPERATOR_MERGE": frozenset({"PR_OR_MERGE", "EXTERNAL_CI_WAIT"}),
    "FINALIZE_AGENT": frozenset({"REPOSITORY_FINALIZATION", "FINALIZATION", "REPORT_GENERATION", "EVIDENCE_PERSISTENCE", "RECONCILIATION"}),
    "REPOSITORY_CLEANUP": frozenset({"REPOSITORY_CLEANUP"}),
}


def intended_path(execution_mode: object) -> tuple[str, ...]:
    """Return the canonical display path for one existing execution mode."""
    return _GENESIS_PATH if execution_mode == "GENESIS" else _MANAGED_PATH


def _checkpoint(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _display_phase(phase: str, checkpoint: dict[str, object]) -> str:
    """Map internal evidence polling onto its visible lifecycle boundary."""
    if phase != "WAIT_FOR_TERMINAL_EVIDENCE":
        return phase
    # Required-check polling happens before the operator merge hand-off.  It
    # is not a completed merge, even though its PR timing evidence exists.
    # A finalization transaction uses the same internal polling phase after
    # the implementation merge, so it remains on its own visible step.
    return "FINALIZE_AGENT" if checkpoint.get("transaction_kind") == "FINALIZATION" else "WAIT_FOR_OPERATOR_MERGE"


def projection(root: Path, run_id: str | None) -> dict[str, object]:
    """Project persisted lifecycle evidence for exactly one ``run_id``.

    Missing event history is explicitly represented as unavailable.  The
    terminal checkpoint may still be shown, but no intermediate progress is
    invented from reports, commits, timing or prompt content.
    """
    if not isinstance(run_id, str) or not run_id:
        return {"run_id": run_id, "available": False, "steps": []}
    try:
        connection = open_storage(root, create=False)
        try:
            row = connection.execute(
                "SELECT payload,phase FROM engineering_transactions WHERE run_id=?", (run_id,)
            ).fetchone()
            events = connection.execute(
                "SELECT phase,checkpoint,recorded_at FROM execution_lifecycle_events WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
            mode_row = connection.execute(
                "SELECT execution_mode FROM execution_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            phase_spans = connection.execute(
                "SELECT phase_name,attempt,started_at,completed_at,duration_ms,outcome "
                "FROM execution_phase_spans WHERE run_id=? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        finally:
            connection.close()
    except EngineeringStorageError:
        return {"run_id": run_id, "available": False, "steps": []}
    if row is None:
        return {"run_id": run_id, "available": False, "steps": []}
    checkpoint = _checkpoint(row[0])
    phase = str(row[1])
    mode = checkpoint.get("execution_mode") or (mode_row[0] if mode_row else None) or "MANAGED"
    path = intended_path(mode)
    display_phase = _display_phase(phase, checkpoint)
    observed: dict[str, dict[str, object]] = {}
    repair_iterations = 0
    for event_phase, event_checkpoint, recorded_at in events:
        event = _checkpoint(event_checkpoint)
        event_step = _display_phase(str(event_phase), event)
        if event_step in path and event_step not in {"START", "TERMINAL"}:
            observed.setdefault(event_step, {"started_at": recorded_at})
        if event_phase == "REPAIR_AGENT":
            repair_iterations = max(repair_iterations, _nonnegative_int(event.get("repair_iterations")))
    repair_iterations = max(repair_iterations, _nonnegative_int(checkpoint.get("repair_iterations")))
    evidence_available = bool(events)
    terminal_state = phase if phase in TERMINAL else None
    # Reaching the pull-request hand-off is not evidence that the pull request
    # was merged. A later finalization step (or a successful terminal state)
    # is the first lifecycle evidence that can make the merge node complete.
    merge_completed = (
        terminal_state == "COMPLETE"
        or "FINALIZE_AGENT" in observed
        or "REPOSITORY_CLEANUP" in observed
    )
    steps: list[dict[str, object]] = []
    for order, step_id in enumerate(path):
        state = "PENDING"
        step: dict[str, object] = {
            "id": step_id,
            "order": order,
            "presentation_key": f"lifecycle.step.{step_id.lower()}",
            "state": state,
        }
        if step_id == "START":
            step["state"] = "START"
        elif step_id == "TERMINAL":
            step["state"] = terminal_state or "PENDING"
            if terminal_state:
                step["terminal_outcome"] = terminal_state
        elif step_id == "WAIT_FOR_OPERATOR_MERGE" and step_id in observed:
            step.update(observed[step_id])
            if merge_completed:
                step["state"] = "COMPLETED"
            elif display_phase == "WAIT_FOR_OPERATOR_MERGE" and terminal_state is None:
                step["state"] = "ACTIVE"
            else:
                # The PR exists, but it has not been merged. In particular,
                # bounded validation repair must leave the merge visibly
                # blocked and must not render a completion checkmark.
                step["state"] = "BLOCKED"
                if display_phase == "REPAIR_AGENT":
                    step["action_key"] = "state.repair_bounded_validation_failure"
        elif step_id in observed:
            step.update(observed[step_id])
            step["state"] = "ACTIVE" if display_phase == step_id and terminal_state is None else "COMPLETED"
        elif step_id == display_phase and terminal_state is None:
            step["state"] = "ACTIVE"
        if step_id == "REPAIR_AGENT" and repair_iterations:
            step["iteration_count"] = repair_iterations
        phase_names = _STEP_PHASES.get(step_id, frozenset())
        spans = [
            {
                "phase": phase_name,
                "attempt": attempt,
                "started_at": started_at,
                "finished_at": completed_at,
                "duration_ms": duration_ms,
                "outcome": outcome,
            }
            for phase_name, attempt, started_at, completed_at, duration_ms, outcome in phase_spans
            if phase_name in phase_names
        ]
        if spans:
            step["timing"] = {
                "started_at": min(str(span["started_at"]) for span in spans if span["started_at"]),
                "finished_at": max((str(span["finished_at"]) for span in spans if span["finished_at"]), default=None),
                "spans": spans,
            }
        steps.append(step)
    return {
        "run_id": run_id,
        "execution_mode": mode,
        "available": evidence_available,
        "terminal_state": terminal_state,
        "current_step": display_phase if display_phase in path else None,
        "steps": steps,
    }
