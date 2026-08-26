"""Deterministic, read-only projections from canonical EP evidence.

The module intentionally opens SQLite in read-only mode.  It does not invoke
Git, GitHub, providers, lifecycle transitions, migrations, or action execution.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .models import (
    ActionPolicyDecision,
    AllowedAction,
    CONTRACT_VERSION,
    ContractVersionError,
    EvidenceReference,
    require_compatible_version,
)
from ..storage import EngineeringStorageError, database_path


UNAVAILABLE = "UNAVAILABLE"
PROJECTION_AUTHORITY = "DERIVED_FROM_CANONICAL_EVIDENCE"
POLICY_VERSION = "1.0"
_SAFE_OBJECTIVE_KEYS = frozenset({"objective_summary", "scope_summary", "constraints", "acceptance_summary", "prohibited_changes_summary"})
_UNSAFE_OBJECTIVE_TEXT = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|cookie|authorization|password)\b|\bbearer\b|(?:^|\s)/(?:\S+)|\b[a-z]:[\\/])"
)
_READ_ACTIONS = (
    ("run.inspect.context", "run.inspect.*", "READ_CANONICAL_EVIDENCE"),
    ("delivery.inspect.status", "delivery.inspect.*", "READ_DELIVERY_EVIDENCE"),
    ("repository.inspect.state", "repository.inspect.*", "READ_REPOSITORY_EVIDENCE"),
    ("workspace.inspect.state", "workspace.inspect.*", "READ_WORKSPACE_EVIDENCE"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _readonly_connection(root: Path) -> sqlite3.Connection:
    path = database_path(root)
    if not path.is_file():
        raise EngineeringStorageError("Engineering storage is unavailable.")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _json_object(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _value(value: object) -> object:
    return value if value is not None else UNAVAILABLE


def _safe_objective(metadata: object) -> dict[str, object]:
    raw = _json_object(metadata)
    result: dict[str, object] = {}
    for key in _SAFE_OBJECTIVE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value and len(value) <= 500 and "\n" not in value and not _UNSAFE_OBJECTIVE_TEXT.search(value):
            result[key] = value
        elif isinstance(value, list) and len(value) <= 12 and all(isinstance(item, str) and len(item) <= 160 and "\n" not in item and not _UNSAFE_OBJECTIVE_TEXT.search(item) for item in value):
            result[key] = value
        else:
            result[key] = UNAVAILABLE
    return result


def _snapshot_id(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"snapshot:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _reference(source_type: str, subject: str, observed_at: object, snapshot: str, code: str, freshness: str = "CURRENT") -> dict[str, object]:
    identity = hashlib.sha256(f"{source_type}:{subject}:{snapshot}".encode()).hexdigest()[:24]
    return EvidenceReference(
        id=f"evidence:{identity}", source_type=source_type,
        authority="CANONICAL_EP_EVIDENCE", observed_at=str(_value(observed_at)),
        freshness=freshness, subject=subject, snapshot_identity=snapshot,
        safe_summary_code=code,
    ).to_dict()


def _current_checks(connection: sqlite3.Connection, run_id: str) -> dict[str, dict[str, object]]:
    try:
        rows = connection.execute(
            "SELECT id,pr_number,pr_role,pr_state,merge_state,merge_commit,required_checks_state,evidence_ref,observed_at,currentness "
            "FROM managed_pr_check_observations WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        role = str(row[2])
        candidate = {"pr_number": row[1], "pr_state": row[3], "merge_state": row[4], "merge_commit": row[5], "required_checks_state": row[6], "observed_at": row[8], "currentness": row[9]}
        if role not in result or (int(row[9]), int(row[0])) >= (int(result[role]["currentness"]), -1):
            result[role] = candidate
    return result


def _validation_controls(connection: sqlite3.Connection, run_id: str, snapshot: str) -> list[dict[str, object]]:
    """Select only the newest canonical observation per validation control."""
    try:
        rows = connection.execute(
            "SELECT id,control,state,required,currentness,observed_at FROM managed_validation_observations WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    current: dict[str, sqlite3.Row] = {}
    for row in rows:
        control = str(row[1])
        if control not in current or (int(row[4]), int(row[0])) >= (int(current[control][4]), int(current[control][0])):
            current[control] = row
    return [{"control_id": control, "display_category_code": control, "state": row[2],
             "execution_source": "MANAGED_VALIDATION_OBSERVATION", "required": bool(row[3]),
             "observed_at": row[5], "evidence_reference": _reference("VALIDATION", f"run:{run_id}:{control}", row[5], snapshot, "VALIDATION_CONTROL", "BOUNDARY_SENSITIVE")}
            for control, row in sorted(current.items())]


def _lease_projection(connection: sqlite3.Connection, run_id: str) -> dict[str, object]:
    try:
        rows = connection.execute(
            "SELECT run_id,host_identity,lease_state,last_heartbeat_at FROM execution_run_leases WHERE lease_state='ACTIVE' ORDER BY updated_at DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"workspace_state": UNAVAILABLE, "workspace_occupied": UNAVAILABLE, "active_owner_run_id": UNAVAILABLE,
                "lease_state": UNAVAILABLE, "lease_owner_identity": UNAVAILABLE, "conflict_present": UNAVAILABLE,
                "recovery_required": UNAVAILABLE, "last_verified_at": UNAVAILABLE}
    foreign = next((row for row in rows if row[0] != run_id), None)
    own = next((row for row in rows if row[0] == run_id), None)
    active = foreign or own
    return {"workspace_state": "OCCUPIED" if foreign else ("OWNED" if own else "AVAILABLE"),
            "workspace_occupied": bool(foreign), "active_owner_run_id": foreign[0] if foreign else UNAVAILABLE,
            "lease_state": own[2] if own else (foreign[2] if foreign else UNAVAILABLE),
            "lease_owner_identity": active[1] if active else UNAVAILABLE, "conflict_present": bool(foreign),
            "recovery_required": False, "last_verified_at": active[3] if active else UNAVAILABLE}


def _usage_projection_unavailable() -> tuple[dict[str, object], dict[str, object]]:
    unavailable = {"provider": UNAVAILABLE, "model": UNAVAILABLE, "model_authority": UNAVAILABLE, "provider_invocation_count": UNAVAILABLE,
                   "reviewer_invocation_count": UNAVAILABLE, "run_cumulative_input": UNAVAILABLE, "cached_input": UNAVAILABLE,
                   "uncached_input": UNAVAILABLE, "output": UNAVAILABLE, "provider_execution_time": UNAVAILABLE,
                   "tool_output": UNAVAILABLE, "git_output": UNAVAILABLE, "github_output": UNAVAILABLE, "estimated_credits": UNAVAILABLE,
                   "estimated_eur": UNAVAILABLE, "speed_state": UNAVAILABLE, "authority": "UNAVAILABLE", "availability": "UNAVAILABLE"}
    return unavailable, {"selection_policy_result": UNAVAILABLE, "selected_reviewer_count": UNAVAILABLE, "roles": [], "reviewers": [], "independence_state": UNAVAILABLE}


def _usage_projection(connection: sqlite3.Connection, run_id: str) -> tuple[dict[str, object], dict[str, object]]:
    try:
        rows = connection.execute(
            "SELECT provider,model,role,duration_ms,input_tokens,cached_input_tokens,uncached_input_tokens,output_tokens,usage_authority,speed_state,estimated_credits,estimated_eur,churn FROM provider_invocations WHERE run_id=? ORDER BY ordinal",
            (run_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        return _usage_projection_unavailable()
    def total(index: int) -> int:
        return sum(value for row in rows if isinstance((value := row[index]), int))
    primary = rows[-1]
    reviewers = [row for row in rows if str(row[2]).casefold() == "reviewer"]
    churn = _json_object(primary[12])
    usage = {"provider": primary[0] or UNAVAILABLE, "model": primary[1] or UNAVAILABLE, "model_authority": primary[8], "provider_invocation_count": len(rows),
             "reviewer_invocation_count": len(reviewers), "run_cumulative_input": total(5) + total(6), "cached_input": total(5), "uncached_input": total(6),
             "output": total(7), "provider_execution_time": total(3), "tool_output": churn.get("tool_output_bytes", UNAVAILABLE),
             "git_output": churn.get("git_output_bytes", UNAVAILABLE), "github_output": churn.get("github_output_bytes", UNAVAILABLE),
             "estimated_credits": primary[10] if primary[10] is not None else UNAVAILABLE, "estimated_eur": primary[11] if primary[11] is not None else UNAVAILABLE,
             "speed_state": primary[9], "authority": primary[8], "availability": "AVAILABLE"}
    reviewer_projection = {"selection_policy_result": "AVAILABLE", "selected_reviewer_count": len(reviewers), "roles": sorted({str(row[2]) for row in rows}),
                           "reviewers": [{"role": row[2], "state": "COMPLETE" if row[3] is not None else "ACTIVE", "duration": row[3] if row[3] is not None else UNAVAILABLE, "conclusion": UNAVAILABLE} for row in reviewers],
                           "independence_state": "UNAVAILABLE"}
    return usage, reviewer_projection


def _phase_workflow(phase: object, terminal: object) -> dict[str, object]:
    current = str(_value(phase))
    state = current if terminal is True else "RUNNING"
    expected = {
        "EXECUTE_AGENT": "LOCAL_REPOSITORY_VALIDATION", "LOCAL_REPOSITORY_VALIDATION": "QUALITY_CONTROL_AGENT", "QUALITY_CONTROL_AGENT": "WAIT_FOR_OPERATOR_MERGE",
        "REPAIR_AGENT": "QUALITY_CONTROL_AGENT", "WAIT_FOR_OPERATOR_MERGE": "FINALIZE_AGENT",
        "FINALIZE_AGENT": "WAIT_FOR_FINALIZATION_MERGE", "WAIT_FOR_FINALIZATION_MERGE": "RECONCILE_AGENT",
        "RECONCILE_AGENT": "REPOSITORY_CLEANUP", "REPOSITORY_CLEANUP": "COMPLETE",
    }.get(current, UNAVAILABLE)
    waiting = "EXPECTED_OPERATOR_MERGE_GATE" if current in {"WAIT_FOR_OPERATOR_MERGE", "WAIT_FOR_FINALIZATION_MERGE"} else UNAVAILABLE
    return {"current_phase": current, "current_state": state, "previous_completed_phase": UNAVAILABLE,
            "next_expected_lifecycle_boundary": expected, "waiting_reason": waiting,
            "blocking_reason": UNAVAILABLE, "last_activity_at": UNAVAILABLE,
            "last_verified_at": UNAVAILABLE, "expected_current_authority": "EP_LIFECYCLE"}


def get_run_context(root: Path, run_id: str) -> dict[str, object]:
    """Return one serializable run projection or an unavailable safe projection."""
    generated_at = _now()
    try:
        connection = _readonly_connection(root)
        try:
            transaction = connection.execute("SELECT payload,phase,updated_at FROM engineering_transactions WHERE run_id=?", (run_id,)).fetchone()
            run = connection.execute("SELECT execution_mode,producer_id,producer_type,execution_started_at,execution_finished_at,execution_seconds FROM execution_runs WHERE run_id=?", (run_id,)).fetchone()
            submission = connection.execute("SELECT s.submission_id,s.producer_id,s.producer_type,s.prompt_metadata,s.received_at FROM execution_submissions AS s JOIN execution_submission_links AS l ON l.submission_id=s.submission_id WHERE l.run_id=?", (run_id,)).fetchone()
            lineage = connection.execute("SELECT retry_of,original_run_id FROM execution_runs WHERE run_id=?", (run_id,)).fetchone()
            checks = _current_checks(connection, run_id)
            validation_controls = _validation_controls(connection, run_id, "pending")
            workspace = _lease_projection(connection, run_id)
            usage, reviewers = _usage_projection(connection, run_id)
        finally:
            connection.close()
    except (EngineeringStorageError, sqlite3.Error):
        transaction = run = submission = lineage = None
        checks = {}
        validation_controls = []
        workspace = {"workspace_state": UNAVAILABLE, "workspace_occupied": UNAVAILABLE, "active_owner_run_id": UNAVAILABLE,
                     "lease_state": UNAVAILABLE, "lease_owner_identity": UNAVAILABLE, "conflict_present": UNAVAILABLE,
                     "recovery_required": UNAVAILABLE, "last_verified_at": UNAVAILABLE}
        usage, reviewers = _usage_projection_unavailable()
    checkpoint = _json_object(transaction[0]) if transaction else {}
    phase = transaction[1] if transaction else UNAVAILABLE
    observed_at = transaction[2] if transaction else UNAVAILABLE
    snapshot = _snapshot_id({"run_id": run_id, "checkpoint": checkpoint, "phase": phase, "checks": checks,
                             "validation": validation_controls, "workspace": workspace, "usage": usage, "reviewers": reviewers})
    # References embed the finalized snapshot identity, never an intermediate value.
    validation_controls = [dict(item, evidence_reference=_reference("VALIDATION", f"run:{run_id}:{item['control_id']}", item["observed_at"], snapshot, "VALIDATION_CONTROL", "BOUNDARY_SENSITIVE")) for item in validation_controls]
    evidence = [_reference("CHECKPOINT", f"run:{run_id}", observed_at, snapshot, "RUN_CHECKPOINT", "BOUNDARY_SENSITIVE")]
    if checks:
        evidence.append(_reference("GITHUB", f"run:{run_id}:checks", max(str(item.get("observed_at", "")) for item in checks.values()), snapshot, "PR_CHECKS", "BOUNDARY_SENSITIVE"))
    terminal = checkpoint.get("terminal")
    implementation = checks.get("IMPLEMENTATION", {})
    finalization = checks.get("FINALIZATION", {})
    objective = _safe_objective(submission[3] if submission else {})
    context: dict[str, object] = {
        "contract_name": "run_context", "contract_version": CONTRACT_VERSION, "generated_at": generated_at,
        "run_id": run_id, "evidence_version": snapshot, "projection_authority": PROJECTION_AUTHORITY,
        "run": {"execution_mode": _value(run[0] if run else checkpoint.get("execution_mode")), "terminal": _value(terminal),
                "current_execution_state": _value(phase), "current_phase": _value(phase),
                "fresh_submission_state": "AVAILABLE" if submission else UNAVAILABLE,
                "retry_parent": _value(lineage[0] if lineage else None), "resume_parent": UNAVAILABLE,
                "producer": {"id": _value(submission[1] if submission else (run[1] if run else None)), "type": _value(submission[2] if submission else (run[2] if run else None))},
                "execution_host": UNAVAILABLE, "lease_state": UNAVAILABLE, "recovery_required": False,
                "active_blocking_predecessor": UNAVAILABLE},
        "objective": objective,
        "workflow": _phase_workflow(phase, terminal),
        "blocker": {"blocker_present": phase in {"BLOCKED", "FAILED"} or workspace["workspace_occupied"] is True,
                    "blocker_type": "WORKSPACE_OCCUPIED" if workspace["workspace_occupied"] is True else ("TERMINAL_RUN" if phase in {"BLOCKED", "FAILED"} else UNAVAILABLE),
                    "summary_code": "WORKSPACE_OCCUPIED" if workspace["workspace_occupied"] is True else ("RUN_TERMINAL" if phase in {"BLOCKED", "FAILED"} else UNAVAILABLE),
                    "evidence_references": evidence, "blocking_run_id": workspace["active_owner_run_id"] if workspace["workspace_occupied"] is True else UNAVAILABLE, "blocking_pr": UNAVAILABLE,
                    "detected_at": _value(observed_at), "verified_at": _value(observed_at), "recoverability": UNAVAILABLE},
        "delivery": {"implementation_pr": _value(checkpoint.get("implementation_pull_request") or checkpoint.get("pull_request") or implementation.get("pr_number")),
                     "implementation_pr_current_state": _value(implementation.get("pr_state")), "implementation_merge_state": _value(implementation.get("merge_state")),
                     "implementation_merge_commit": _value(checkpoint.get("implementation_merge_commit") or implementation.get("merge_commit")),
                     "implementation_required_checks_state": _value(implementation.get("required_checks_state")), "implementation_merge_gate": "EXPECTED_OPERATOR_GATE" if phase == "WAIT_FOR_OPERATOR_MERGE" else UNAVAILABLE,
                     "finalization_pr": _value(checkpoint.get("finalization_pull_request") or finalization.get("pr_number")), "finalization_pr_current_state": _value(finalization.get("pr_state")),
                     "finalization_merge_state": _value(finalization.get("merge_state")), "finalization_merge_commit": _value(checkpoint.get("finalization_merge_commit") or finalization.get("merge_commit")),
                     "finalization_required_checks_state": _value(finalization.get("required_checks_state")), "finalization_merge_gate": "EXPECTED_OPERATOR_GATE" if phase == "WAIT_FOR_FINALIZATION_MERGE" else UNAVAILABLE,
                     "run_delivery_commit": _value(checkpoint.get("implementation_head_sha") or checkpoint.get("last_verified_sha")), "current_repository_head": _value(checkpoint.get("last_verified_sha")), "delivery_commit_head_relationship": UNAVAILABLE},
        "validation": {"engineering_platform_qualification": UNAVAILABLE, "controls": validation_controls},
        "repository": {"repository_identity": _value(checkpoint.get("repository")), "expected_branch": "main", "current_branch": _value(checkpoint.get("branch")), "worktree_state": UNAVAILABLE, "main_origin_relationship": UNAVAILABLE, "repository_state": UNAVAILABLE, "delivery_commit_relationship": UNAVAILABLE, "last_verified_at": _value(observed_at)},
        "workspace": workspace,
        "timing": {"run_wall_time": _value(run[5] if run else None), "provider_execution_time": _value(checkpoint.get("agent_execution_seconds")), "reviewer_time": UNAVAILABLE, "validation_time": UNAVAILABLE, "external_wait_time": UNAVAILABLE, "ci_wait_time": UNAVAILABLE, "merge_gate_wait_time": _value(checkpoint.get("waiting_for_merge_since")), "finalization_time": UNAVAILABLE, "reconciliation_time": UNAVAILABLE, "last_activity_at": _value(observed_at)},
        "usage": usage,
        "reviewers": reviewers,
        "historical_report": {"availability": UNAVAILABLE}, "current_projection": {"availability": "AVAILABLE", "projection_generated_at": generated_at, "projection_authority": PROJECTION_AUTHORITY},
        "authority": {"merge_authority": "OPERATOR", "projection_authority": PROJECTION_AUTHORITY}, "evidence": evidence,
    }
    context["allowed_actions"] = get_allowed_actions(root, run_id, context=context)
    return context


def get_allowed_actions(root: Path, run_id: str, *, context: dict[str, object] | None = None) -> list[dict[str, object]]:
    """Return only current EP policy descriptors; no mutating action is invented."""
    context = context or get_run_context(root, run_id)
    evidence_version = str(context.get("evidence_version", UNAVAILABLE))
    known = context.get("run", {}).get("current_execution_state") != UNAVAILABLE if isinstance(context.get("run"), dict) else False
    return [AllowedAction(action_id=action_id, action_namespace=namespace, run_id=run_id, allowed=known,
                          reason_code="READ_ONLY_INSPECTION_AVAILABLE" if known else "RUN_EVIDENCE_UNAVAILABLE",
                          evidence_version=evidence_version, expected_effect_code=effect,
                          blocked_reason_code=None if known else "RUN_EVIDENCE_UNAVAILABLE").to_dict()
            for action_id, namespace, effect in _READ_ACTIONS]


def evaluate_action(root: Path, run_id: str, action: AllowedAction | dict[str, object]) -> dict[str, object]:
    """Re-evaluate one descriptor against fresh evidence before any future action gateway use."""
    descriptor = action.to_dict() if isinstance(action, AllowedAction) else dict(action) if isinstance(action, dict) else {}
    context = get_run_context(root, run_id)
    fresh = str(context["evidence_version"])
    known_action_ids = {action_id for action_id, _, _ in _READ_ACTIONS}
    action_id = str(descriptor.get("action_id")) if descriptor.get("action_id") in known_action_ids else UNAVAILABLE
    try:
        require_compatible_version(descriptor.get("contract_version"))
    except ContractVersionError:
        return ActionPolicyDecision(action_id=action_id, run_id=run_id, decision="UNAVAILABLE", reason_code="INCOMPATIBLE_CONTRACT_VERSION", policy_version=POLICY_VERSION, evaluated_at=_now(), evidence_version=fresh).to_dict()
    if descriptor.get("run_id") != run_id or descriptor.get("evidence_version") != fresh:
        return ActionPolicyDecision(action_id=action_id, run_id=run_id, decision="STALE_REVALIDATION_REQUIRED", reason_code="EVIDENCE_VERSION_CHANGED", policy_version=POLICY_VERSION, evaluated_at=_now(), evidence_version=fresh).to_dict()
    permitted = {item["action_id"] for item in get_allowed_actions(root, run_id, context=context) if item["allowed"] is True}
    decision = "ALLOWED" if action_id in permitted and descriptor.get("classification") == "READ_ONLY" else "DENIED"
    return ActionPolicyDecision(action_id=action_id, run_id=run_id, decision=decision,
                                reason_code="READ_ONLY_INSPECTION_AVAILABLE" if decision == "ALLOWED" else "ACTION_NOT_CURRENTLY_ALLOWED",
                                policy_version=POLICY_VERSION, evaluated_at=_now(), evidence_version=fresh).to_dict()
