"""Append-only Managed-autonomy evidence and fail-closed qualification read model."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .storage import EngineeringStorageError, load_run_lineage, load_validation_context, open_storage

AUTHORITIES = frozenset(
    {
        "AUTONOMOUS_EP_ACTION",
        "EXPECTED_OPERATOR_GATE",
        "EXTERNAL_PLATFORM_EVENT",
        "UNPLANNED_MANUAL_INTERVENTION",
        "UNKNOWN_AUTHORITY",
    }
)
GATE_TYPES = frozenset({"IMPLEMENTATION_MERGE_APPROVAL", "FINALIZATION_MERGE_APPROVAL"})
GATE_STATUSES = frozenset({"NOT_REQUIRED", "WAITING", "SATISFIED", "UNAVAILABLE"})
VALIDATION_STATES = frozenset(
    {"PASS", "FAIL", "NOT_EXECUTED", "NOT_APPLICABLE", "UNAVAILABLE", "WAITING"}
)
PR_CHECK_STATES = frozenset({"PASS", "FAIL", "WAITING", "UNAVAILABLE"})
PR_ROLES = frozenset({"IMPLEMENTATION", "FINALIZATION"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(value: object, limit: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\n" in value:
        raise EngineeringStorageError("Managed autonomy evidence value is invalid.")
    return value


def append_action(
    root,
    *,
    run_id: str,
    action: str,
    authority: str,
    actor: str = "execution_host",
    evidence_ref: str = "runtime",
    observed_at: str | None = None,
) -> None:
    """Persist identifier-only evidence; raw prompts and tool output are rejected by shape."""
    if authority not in AUTHORITIES:
        raise EngineeringStorageError("Managed autonomy action authority is invalid.")
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT INTO managed_autonomy_actions(run_id,action,authority,actor,evidence_ref,observed_at) VALUES(?,?,?,?,?,?)",
            (
                _safe(run_id),
                _safe(action),
                authority,
                _safe(actor),
                _safe(evidence_ref),
                observed_at or _now(),
            ),
        )
    finally:
        connection.close()


def record_gate(
    root,
    *,
    run_id: str,
    gate_type: str,
    status: str,
    related_pr: int | None,
    phase: str,
    resolution_actor: str | None = None,
    requested_at: str | None = None,
    resolved_at: str | None = None,
) -> None:
    if (
        gate_type not in GATE_TYPES
        or status not in GATE_STATUSES
        or (
            related_pr is not None
            and (isinstance(related_pr, bool) or not isinstance(related_pr, int) or related_pr < 1)
        )
    ):
        raise EngineeringStorageError("Managed governance gate is invalid.")
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT INTO managed_governance_gates(run_id,gate_type,gate_authority,status,requested_at,resolved_at,resolution_actor,related_pr,phase) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,gate_type) DO UPDATE SET status=excluded.status,resolved_at=excluded.resolved_at,resolution_actor=excluded.resolution_actor,related_pr=excluded.related_pr,phase=excluded.phase",
            (
                _safe(run_id),
                gate_type,
                "OPERATOR",
                status,
                requested_at or _now(),
                resolved_at,
                resolution_actor,
                related_pr,
                _safe(phase),
            ),
        )
    finally:
        connection.close()


def append_validation_observation(
    root,
    *,
    run_id: str,
    control: str,
    state: str,
    required: bool,
    currentness: int = 0,
    observed_at: str | None = None,
) -> None:
    if state not in VALIDATION_STATES or currentness < 0:
        raise EngineeringStorageError("Managed validation observation is invalid.")
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT INTO managed_validation_observations(run_id,control,state,required,currentness,observed_at) VALUES(?,?,?,?,?,?)",
            (
                _safe(run_id),
                _safe(control),
                state,
                int(required),
                currentness,
                observed_at or _now(),
            ),
        )
    finally:
        connection.close()


def append_pr_check_observation(
    root,
    *,
    run_id: str,
    pr_number: int,
    pr_role: str,
    pr_state: str,
    merge_commit: str | None,
    required_checks_state: str,
    evidence_ref: str,
    currentness: int = 0,
    observed_at: str | None = None,
) -> None:
    """Append a bounded GitHub required-check observation without changing lifecycle."""
    if (
        pr_role not in PR_ROLES
        or required_checks_state not in PR_CHECK_STATES
        or isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or pr_number < 1
        or currentness < 0
        or (merge_commit is not None and (not isinstance(merge_commit, str) or len(merge_commit) != 40))
    ):
        raise EngineeringStorageError("Managed PR check observation is invalid.")
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT INTO managed_pr_check_observations(run_id,pr_number,pr_role,pr_state,merge_state,merge_commit,required_checks_state,evidence_ref,observed_at,currentness) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                _safe(run_id), pr_number, pr_role, _safe(pr_state),
                "MERGED" if pr_state == "MERGED" else "NOT_MERGED", merge_commit,
                required_checks_state, _safe(evidence_ref), observed_at or _now(), currentness,
            ),
        )
    finally:
        connection.close()


def _current(rows: Iterable[tuple[object, ...]]) -> tuple[dict[str, str], bool]:
    grouped: dict[str, list[tuple[str, int]]] = {}
    for control, state, _required, currentness, _at in rows:
        grouped.setdefault(str(control), []).append((str(state), int(currentness)))
    result, conflict = {}, False
    for control, values in grouped.items():
        latest = max(item[1] for item in values)
        states = {item[0] for item in values if item[1] == latest}
        conflict |= len(states) != 1
        result[control] = next(iter(states)) if len(states) == 1 else "UNAVAILABLE"
    return result, conflict


def _current_pr_checks(rows: Iterable[tuple[object, ...]]) -> tuple[dict[str, dict[str, object]], bool]:
    """Keep historical observations append-only while selecting current terminal evidence."""
    grouped: dict[str, list[tuple[object, ...]]] = {}
    for row in rows:
        grouped.setdefault(str(row[2]), []).append(row)
    result: dict[str, dict[str, object]] = {}
    conflict = False
    for role, values in grouped.items():
        latest = max((int(row[9]), int(row[0])) for row in values)
        current = [row for row in values if (int(row[9]), int(row[0])) == latest]
        signatures = {(row[3], row[4], row[5], row[6], row[7]) for row in current}
        if len(signatures) != 1:
            conflict = True
            result[role] = {"required_checks_state": "UNAVAILABLE", "conflict": True}
            continue
        row = current[-1]
        result[role] = {
            "pr_number": row[1], "pr_state": row[3], "merge_state": row[4],
            "merge_commit": row[5], "required_checks_state": row[6],
            "evidence_ref": row[7], "observed_at": row[8], "conflict": False,
            "historical_observation_count": len(values) - len(current),
        }
    return result, conflict


def terminal_snapshot(
    root,
    *,
    run_id: str,
    execution_outcome: str,
    implementation_pr: int | None,
    finalization_pr: int | None,
    repository_state: str,
    workspace_state: str,
    main_origin_sync: str,
    worktree_state: str,
    active_blocker: str,
    recovery_required: str,
    retry_parent: str | None = None,
    resume_parent: str | None = None,
    submission_id: str | None = None,
    lineage_available: bool = False,
    reviewer_records: tuple[dict[str, object], ...] = (),
    action_intent: str = "MUTATING_DELIVERY",
) -> dict[str, object]:
    connection = open_storage(root)
    try:
        actions = connection.execute(
            "SELECT action,authority FROM managed_autonomy_actions WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        gates = connection.execute(
            "SELECT gate_type,status,related_pr FROM managed_governance_gates WHERE run_id=?",
            (run_id,),
        ).fetchall()
        rows = connection.execute(
            "SELECT control,state,required,currentness,observed_at FROM managed_validation_observations WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        pr_rows = connection.execute(
            "SELECT id,pr_number,pr_role,pr_state,merge_state,merge_commit,required_checks_state,evidence_ref,observed_at,currentness FROM managed_pr_check_observations WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    finally:
        connection.close()
    validation, conflict = _current(rows)
    pr_checks, pr_check_conflict = _current_pr_checks(pr_rows)
    try:
        persisted_lineage = load_run_lineage(root, run_id)
        validation_context = load_validation_context(root, run_id)
    except EngineeringStorageError:
        persisted_lineage = validation_context = None
    if persisted_lineage is not None:
        lineage_available = True
        submission_id = str(persisted_lineage["submission_id"])
        retry_parent = persisted_lineage["retry_parent"] if isinstance(persisted_lineage["retry_parent"], str) else None
        resume_parent = persisted_lineage["resume_parent"] if isinstance(persisted_lineage["resume_parent"], str) else None
        fresh = "YES" if persisted_lineage["fresh_submission"] else "NO"
    else:
        fresh = "YES" if lineage_available and retry_parent is None and resume_parent is None else "NO" if lineage_available else "UNAVAILABLE"
    required = {str(row[0]) for row in rows if int(row[2])}
    required_state = "UNRESOLVED"
    profile_projection: dict[str, object] = {}
    if validation_context is not None:
        required = set(validation_context["required_validation_controls"])
        controls = validation_context["controls"]
        results = [controls.get(control, {}).get("result") for control in required]
        required_state = "FAIL" if any(result == "FAIL" for result in results) else "PASS" if results and all(result == "PASS" for result in results) else "UNRESOLVED"
        profile_projection = {key: validation_context[key] for key in (
            "selected_validation_tier", "validation_profile_version", "profile_reference",
            "profile_selection_source", "required_validation_controls", "control_bindings",
        )}
        validation = {**validation, **{key: value.get("result", "UNRESOLVED") for key, value in controls.items()}}
    elif required:
        # Historical evidence has no persisted profile: do not promote it.
        required_state = "UNRESOLVED"
    authorities = [str(row[1]) for row in actions]
    snapshot = {
        "run_id": run_id,
        "terminal_execution_state": execution_outcome,
        "managed_authority_profile": "OPERATOR_OWNED_PR_MERGE",
        "action_intent": action_intent,
        "fresh_submission": fresh,
        "retry_parent": retry_parent or ("NONE" if lineage_available else "UNAVAILABLE"),
        "resume_parent": resume_parent or ("NONE" if lineage_available else "UNAVAILABLE"),
        "submission_id": submission_id or "UNAVAILABLE",
        "implementation_pr": implementation_pr,
        "finalization_pr": finalization_pr,
        "gates": [{"gate_type": row[0], "status": row[1], "related_pr": row[2]} for row in gates],
        "actions": [{"action": row[0], "authority": row[1]} for row in actions],
        "validation_current": validation,
        "required_validation_state": required_state,
        "validation_profile": profile_projection or "UNAVAILABLE",
        "validation_projection_conflict": conflict,
        "pr_checks": pr_checks,
        "pr_check_projection_conflict": pr_check_conflict,
        "repository_state": repository_state,
        "workspace_state": workspace_state,
        "main_origin_sync": main_origin_sync,
        "worktree_state": worktree_state,
        "active_blocking_predecessor": active_blocker,
        "recovery_required": recovery_required,
        "expected_operator_gate_count": len(gates),
        "autonomous_ep_action_count": authorities.count("AUTONOMOUS_EP_ACTION"),
        "external_platform_event_count": authorities.count("EXTERNAL_PLATFORM_EVENT"),
        "unplanned_manual_intervention_count": authorities.count("UNPLANNED_MANUAL_INTERVENTION"),
        "unknown_authority_count": authorities.count("UNKNOWN_AUTHORITY"),
        "reviewer_independence": "NOT_APPLICABLE"
        if not reviewer_records
        else (
            "PROVEN" if all(not row.get("failed") for row in reviewer_records) else "UNAVAILABLE"
        ),
        "observed_at": _now(),
    }
    snapshot["run_qualification"], snapshot["qualification_failure_reasons"] = evaluate(snapshot)
    # Compatibility projection retained for existing consumers.  It has the
    # same run-scoped meaning and must never be mistaken for platform-wide
    # qualification evidence.
    snapshot["managed_autonomy_qualification"] = snapshot["run_qualification"]
    return snapshot


def evaluate(snapshot: dict[str, object]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    terminal = snapshot.get("terminal_execution_state")
    if terminal == "FAILED":
        return "NOT_QUALIFIED", ["TERMINAL_EXECUTION_FAILED"]
    if terminal == "BLOCKED":
        return "NOT_QUALIFIED", ["TERMINAL_EXECUTION_BLOCKED"]
    if snapshot.get("fresh_submission") != "YES":
        reasons.append("FRESH_SUBMISSION_UNPROVEN")
    validation_only = snapshot.get("action_intent") == "VALIDATION_ONLY"
    if snapshot.get("terminal_execution_state") != "COMPLETE":
        reasons.append("VALIDATION_EXECUTION_UNPROVEN" if validation_only else "IMPLEMENTATION_DELIVERY_UNPROVEN")
    if snapshot.get("unknown_authority_count", 0):
        reasons.append("EP_ACTION_AUTHORITY_UNPROVEN")
    if snapshot.get("unplanned_manual_intervention_count", 0):
        reasons.append("UNEXPECTED_MANUAL_INTERVENTION")
    gate = {
        row.get("gate_type"): row.get("status")
        for row in snapshot.get("gates", [])
        if isinstance(row, dict)
    }
    if not validation_only:
        if gate.get("IMPLEMENTATION_MERGE_APPROVAL") != "SATISFIED":
            reasons.append("IMPLEMENTATION_MERGE_GATE_UNPROVEN")
        if gate.get("FINALIZATION_MERGE_APPROVAL") != "SATISFIED":
            reasons.append("FINALIZATION_MERGE_GATE_UNPROVEN")
    autonomous = {
        row.get("action")
        for row in snapshot.get("actions", [])
        if isinstance(row, dict) and row.get("authority") == "AUTONOMOUS_EP_ACTION"
    }
    if not validation_only and (
        not {
            "IMPLEMENTATION",
            "POST_IMPLEMENTATION_MERGE",
            "FINALIZATION",
            "RECONCILIATION",
            "CLEANUP",
        }
        <= autonomous
    ):
        reasons.append("EP_ACTION_AUTHORITY_UNPROVEN")
    if snapshot.get("required_validation_state") != "PASS":
        reasons.append("REQUIRED_VALIDATION_UNRESOLVED")
    if snapshot.get("validation_projection_conflict"):
        reasons.append("EVIDENCE_CONFLICT")
    if snapshot.get("pr_check_projection_conflict"):
        reasons.append("EVIDENCE_CONFLICT")
    for role in (() if validation_only else ("IMPLEMENTATION", "FINALIZATION")):
        if snapshot.get(f"{role.lower()}_pr") is not None:
            check = snapshot.get("pr_checks", {}).get(role, {})
            if check.get("required_checks_state") != "PASS":
                reasons.append(f"{role}_REQUIRED_CHECKS_UNRESOLVED")
    for key, wanted in {
        "repository_state": "MERGED_RECONCILED",
        "workspace_state": "WORKSPACE_READY",
        "main_origin_sync": "YES",
        "worktree_state": "CLEAN",
        "active_blocking_predecessor": "NONE",
        "recovery_required": "NO",
    }.items():
        if snapshot.get(key) != wanted:
            reasons.append(
                "REPOSITORY_FINAL_STATE_UNPROVEN"
                if key in {"repository_state", "main_origin_sync", "worktree_state"}
                else "WORKSPACE_FINAL_STATE_UNPROVEN"
            )
    if snapshot.get("reviewer_independence") not in {"PROVEN", "NOT_APPLICABLE"}:
        reasons.append("REVIEWER_POLICY_UNPROVEN")
    reasons = list(dict.fromkeys(reasons))
    return (
        ("NOT_QUALIFIED", reasons)
        if "UNEXPECTED_MANUAL_INTERVENTION" in reasons
        else (("QUALIFIED", []) if not reasons else ("EVIDENCE_INSUFFICIENT", reasons))
    )
