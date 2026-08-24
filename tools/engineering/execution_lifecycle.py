"""Read-only, run-scoped execution lifecycle projection for the console.

This module deliberately projects persisted Execution Host checkpoints only.  It
does not coordinate transitions, repair, retry, resume, liveness, or telemetry.
"""
from __future__ import annotations

import json
from pathlib import Path

from .storage import EngineeringStorageError, open_storage
from .agent_state import TransactionState
from .status_reconciliation import is_stale_rolling_status_block


TERMINAL = frozenset({"COMPLETE", "BLOCKED", "FAILED"})
_MANAGED_PATH = (
    "START", "INITIALIZE", "EXECUTE_AGENT", "REPAIR_AGENT",
    "WAIT_FOR_OPERATOR_MERGE", "FINALIZE_AGENT", "WAIT_FOR_FINALIZATION_MERGE",
    "RECONCILE_AGENT", "REPOSITORY_CLEANUP", "TERMINAL",
)
# Genesis has no pull-request merge boundary.  This is presentation of the
# existing mode contract, not a new execution sequence.
_GENESIS_PATH = (
    "START", "INITIALIZE", "EXECUTE_AGENT", "REPAIR_AGENT",
    "FINALIZE_AGENT", "REPOSITORY_CLEANUP", "TERMINAL",
)
_STATUS_RECONCILIATION_PATH = (
    "START", "INITIALIZE", "RECONCILE_AGENT", "REPOSITORY_CLEANUP", "TERMINAL",
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
    "FINALIZE_AGENT": frozenset({"REPOSITORY_FINALIZATION", "FINALIZATION"}),
    "RECONCILE_AGENT": frozenset({"RECONCILIATION", "REPORT_GENERATION", "EVIDENCE_PERSISTENCE"}),
    "REPOSITORY_CLEANUP": frozenset({"REPOSITORY_CLEANUP"}),
}


def intended_path(
    execution_mode: object,
    transaction_kind: object = "IMPLEMENTATION",
    implementation_pull_request: object = None,
) -> tuple[str, ...]:
    """Return the canonical display path for one existing execution mode."""
    if transaction_kind == "RECONCILIATION" or (
        transaction_kind == "FINALIZATION" and implementation_pull_request is None
    ):
        return _STATUS_RECONCILIATION_PATH
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


def _pull_request_recorded(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _display_phase(phase: str, checkpoint: dict[str, object]) -> str:
    """Map internal evidence polling onto its visible lifecycle boundary."""
    # Finalization has its own PR hand-off. Do not project it back onto the
    # implementation merge once that implementation PR is already merged.
    if phase == "WAIT_FOR_OPERATOR_MERGE" and checkpoint.get("transaction_kind") == "FINALIZATION":
        return (
            "WAIT_FOR_RECONCILIATION_MERGE"
            if checkpoint.get("implementation_pull_request") is None
            else "WAIT_FOR_FINALIZATION_MERGE"
        )
    if phase == "WAIT_FOR_OPERATOR_MERGE" and checkpoint.get("transaction_kind") == "RECONCILIATION":
        return "WAIT_FOR_RECONCILIATION_MERGE"
    if phase != "WAIT_FOR_TERMINAL_EVIDENCE":
        return phase
    # Required-check polling happens before the operator merge hand-off.  It
    # is not a completed merge, even though its PR timing evidence exists.
    # A finalization transaction uses the same internal polling phase after
    # the implementation merge, so it remains on its own visible step.
    if checkpoint.get("transaction_kind") == "FINALIZATION":
        return "RECONCILE_AGENT" if checkpoint.get("implementation_pull_request") is None else "FINALIZE_AGENT"
    if checkpoint.get("transaction_kind") == "RECONCILIATION":
        return "RECONCILE_AGENT"
    return "WAIT_FOR_OPERATOR_MERGE"


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
    transaction_kind = checkpoint.get("transaction_kind") or "IMPLEMENTATION"
    path = intended_path(mode, transaction_kind, checkpoint.get("implementation_pull_request"))
    display_phase = _display_phase(phase, checkpoint)
    observed: dict[str, dict[str, object]] = {}
    repair_iterations = 0
    implementation_merge_required = _pull_request_recorded(checkpoint.get("implementation_pull_request"))
    finalization_merge_required = _pull_request_recorded(checkpoint.get("finalization_pull_request"))
    reconciliation_merge_required = _pull_request_recorded(checkpoint.get("reconciliation_pull_request"))
    recorded_pull_request = any(
        _pull_request_recorded(checkpoint.get(field))
        for field in ("pull_request", "implementation_pull_request", "finalization_pull_request", "reconciliation_pull_request")
    )
    if checkpoint.get("transaction_kind") == "FINALIZATION":
        finalization_merge_required = finalization_merge_required or _pull_request_recorded(checkpoint.get("pull_request"))
    if checkpoint.get("transaction_kind") == "RECONCILIATION":
        reconciliation_merge_required = reconciliation_merge_required or _pull_request_recorded(checkpoint.get("pull_request"))
    else:
        implementation_merge_required = implementation_merge_required or _pull_request_recorded(checkpoint.get("pull_request"))
    for event_phase, event_checkpoint, recorded_at in events:
        event = _checkpoint(event_checkpoint)
        recorded_pull_request = recorded_pull_request or any(
            _pull_request_recorded(event.get(field))
            for field in ("pull_request", "implementation_pull_request", "finalization_pull_request", "reconciliation_pull_request")
        )
        event_step = _display_phase(str(event_phase), event)
        if event_step in path and event_step not in {"START", "TERMINAL"}:
            observed.setdefault(event_step, {"started_at": recorded_at})
        if event_phase == "REPAIR_AGENT":
            repair_iterations = max(repair_iterations, _nonnegative_int(event.get("repair_iterations")))
        implementation_merge_required = (
            implementation_merge_required or event_step == "WAIT_FOR_OPERATOR_MERGE"
        )
        finalization_merge_required = (
            finalization_merge_required or event_step == "WAIT_FOR_FINALIZATION_MERGE"
        )
        reconciliation_merge_required = (
            reconciliation_merge_required or event_step == "WAIT_FOR_RECONCILIATION_MERGE"
        )
    repair_iterations = max(repair_iterations, _nonnegative_int(checkpoint.get("repair_iterations")))
    evidence_available = bool(events)
    terminal_state = phase if phase in TERMINAL else None
    try:
        status_reconciliation_block = is_stale_rolling_status_block(
            TransactionState.from_dict(checkpoint)
        ) and not recorded_pull_request
    except ValueError:
        status_reconciliation_block = False
    # Reaching the pull-request hand-off is not evidence that the pull request
    # was merged. A later finalization step (or a successful terminal state)
    # is the first lifecycle evidence that can make the merge node complete.
    implementation_merge_completed = (
        terminal_state == "COMPLETE"
        or "FINALIZE_AGENT" in observed
        or "REPOSITORY_CLEANUP" in observed
    )
    finalization_merge_completed = terminal_state == "COMPLETE" or "REPOSITORY_CLEANUP" in observed
    reconciliation_merge_completed = terminal_state == "COMPLETE" or "REPOSITORY_CLEANUP" in observed
    # The managed contract permits a no-PR path and a single implementation-PR
    # path. A terminal pre-flight block is not merge evidence. Omit boundaries
    # that have no persisted PR evidence instead of inventing an operator wait.
    if mode != "GENESIS":
        path = tuple(
            step_id for step_id in path
            if not (
                step_id == "WAIT_FOR_OPERATOR_MERGE"
                and (not implementation_merge_required or status_reconciliation_block)
                and ("FINALIZE_AGENT" in observed or "REPOSITORY_CLEANUP" in observed or terminal_state == "COMPLETE" or status_reconciliation_block)
            )
            and not (
                step_id == "WAIT_FOR_FINALIZATION_MERGE"
                and (not finalization_merge_required or status_reconciliation_block)
                and ("REPOSITORY_CLEANUP" in observed or terminal_state == "COMPLETE" or status_reconciliation_block)
            )
            and not (
                step_id == "WAIT_FOR_RECONCILIATION_MERGE"
                and (not reconciliation_merge_required or status_reconciliation_block)
                and ("REPOSITORY_CLEANUP" in observed or terminal_state == "COMPLETE" or status_reconciliation_block)
            )
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
        elif step_id in {"WAIT_FOR_OPERATOR_MERGE", "WAIT_FOR_FINALIZATION_MERGE", "WAIT_FOR_RECONCILIATION_MERGE"} and step_id in observed:
            step.update(observed[step_id])
            merge_completed = (
                implementation_merge_completed
                if step_id == "WAIT_FOR_OPERATOR_MERGE"
                else finalization_merge_completed if step_id == "WAIT_FOR_FINALIZATION_MERGE" else reconciliation_merge_completed
            )
            if merge_completed:
                step["state"] = "COMPLETED"
            elif display_phase == step_id and terminal_state is None:
                step["state"] = "ACTIVE"
            else:
                # The PR exists, but it has not been merged. In particular,
                # bounded validation repair must leave the merge visibly
                # blocked and must not render a completion checkmark.
                step["state"] = "BLOCKED"
                if step_id == "WAIT_FOR_OPERATOR_MERGE" and display_phase == "REPAIR_AGENT":
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
    recovery = None
    if status_reconciliation_block:
        recovery = {"kind": "status_reconciliation", "run_id": run_id}
    return {
        "run_id": run_id,
        "execution_mode": mode,
        "available": evidence_available,
        "terminal_state": terminal_state,
        "current_step": display_phase if display_phase in path else None,
        "steps": steps,
        "recovery": recovery,
    }
