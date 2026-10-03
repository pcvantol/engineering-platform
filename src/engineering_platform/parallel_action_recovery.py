"""Process-fenced PA-E3 dispatch and cancellation evidence in CENTRAL leases.

The first recovery profile keeps uncertain effects held. A dead dispatcher is
only replaceable before it crossed the runner boundary; a later restart never
guesses whether a provider child survived its parent process.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
from uuid import uuid4

from . import central_database, parallel_action_delivery
from .provider_process_identity import ProcessIdentity, capture_process_identity, verify_process_identity
from .storage import sqlite_connection


_ATTEMPT = "pa-e3:attempt:"
_ENTERED = "pa-e3:entered:"
_RETURNED = "pa-e3:returned:"
_UNCERTAIN = "pa-e3:uncertain:"
_CANCEL = "pa-e3:cancel:"
_CANCEL_OUTCOME = "pa-e3:cancel-outcome:"
_FOREVER = "9999-12-31T23:59:59+00:00"


class RecoveryFenceError(ValueError):
    """One stable, fail-closed PA-E3 recovery disposition."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner(submission_id: str, project_id: str, repository_id: str) -> str:
    identity = capture_process_identity(os.getpid())
    if identity is None:
        raise RecoveryFenceError("PA_E3_PROCESS_IDENTITY_UNAVAILABLE")
    return json.dumps({
        "version": 1, "host": socket.gethostname(),
        "pid": identity.pid, "group": identity.process_group,
        "birth": identity.start_fingerprint, "executable": identity.executable_identity,
        "submission_id": submission_id, "project_id": project_id,
        "repository_id": repository_id,
    }, sort_keys=True, separators=(",", ":"))


def _parse_owner(value: str, *, submission_id: str, project_id: str,
                 repository_id: str) -> dict[str, object]:
    try:
        owner = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise RecoveryFenceError("PA_E3_OWNER_EVIDENCE_INVALID") from None
    if (not isinstance(owner, dict) or owner.get("version") != 1
            or owner.get("submission_id") != submission_id
            or owner.get("project_id") != project_id
            or owner.get("repository_id") != repository_id
            or not isinstance(owner.get("host"), str)
            or not isinstance(owner.get("pid"), int)
            or not isinstance(owner.get("group"), int)
            or not isinstance(owner.get("birth"), str)
            or not isinstance(owner.get("executable"), str)):
        raise RecoveryFenceError("PA_E3_OWNER_EVIDENCE_INVALID")
    return owner


def _active(connection: sqlite3.Connection, run_id: str) -> list[tuple[str, str]]:
    return [(str(row[0]), str(row[1])) for row in connection.execute(
        "SELECT lease_id,holder_id FROM ep_execution_leases WHERE run_id=? "
        "AND lease_id LIKE ? AND released_at IS NULL",
        (run_id, _ATTEMPT + run_id + ":%"),
    )]


def _entered_id(attempt_id: str) -> str:
    return _ENTERED + attempt_id.removeprefix(_ATTEMPT)


def _returned_id(attempt_id: str) -> str:
    return _RETURNED + attempt_id.removeprefix(_ATTEMPT)


def _unreturned_entries(connection: sqlite3.Connection, run_id: str) -> list[str]:
    return [str(row[0]) for row in connection.execute(
        "SELECT entered.lease_id FROM ep_execution_leases entered "
        "WHERE entered.run_id=? AND entered.lease_id LIKE ? "
        "AND NOT EXISTS (SELECT 1 FROM ep_execution_leases returned "
        "WHERE returned.lease_id=? || substr(entered.lease_id, length(?) + 1))",
        (run_id, _ENTERED + run_id + ":%", _RETURNED, _ENTERED),
    )]


def _prove_prior_process_stopped(owner: dict[str, object]) -> None:
    if owner["host"] != socket.gethostname():
        raise RecoveryFenceError("PA_E3_OWNER_HOST_UNAVAILABLE")
    identity = ProcessIdentity(int(owner["pid"]), int(owner["group"]),
                               str(owner["birth"]), str(owner["executable"]))
    if verify_process_identity(identity) == "MATCH":
        raise RecoveryFenceError("PA_E3_DISPATCH_ACTIVE")
    # A missing ps observation is not proof that a process disappeared.
    try:
        os.kill(identity.pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        raise RecoveryFenceError("PA_E3_OWNER_LIVENESS_UNAVAILABLE") from None
    observed = capture_process_identity(identity.pid)
    if observed is None:
        raise RecoveryFenceError("PA_E3_OWNER_LIVENESS_UNAVAILABLE")
    if (observed.process_group == identity.process_group
            and observed.start_fingerprint == identity.start_fingerprint
            and observed.executable_identity == identity.executable_identity):
        raise RecoveryFenceError("PA_E3_DISPATCH_ACTIVE")


def status(connection: sqlite3.Connection, run_id: str) -> str:
    if connection.execute(
        "SELECT 1 FROM ep_execution_leases WHERE lease_id=? AND released_at IS NULL",
        (_UNCERTAIN + run_id,),
    ).fetchone():
        return "PROVIDER_EFFECT_UNCERTAIN"
    cancel = connection.execute(
        "SELECT released_at FROM ep_execution_leases WHERE lease_id=?",
        (_CANCEL + run_id,),
    ).fetchone()
    if cancel is not None:
        if cancel[0] is None:
            return "CANCEL_REQUESTED"
        if connection.execute(
            "SELECT 1 FROM ep_execution_leases WHERE lease_id=?",
            (_CANCEL_OUTCOME + run_id,),
        ).fetchone():
            return "CANCEL_TOO_LATE"
        return "CANCEL_ACKNOWLEDGED"
    return "NONE"


def acquire(data_root: Path, *, run_id: str, submission_id: str,
            project_id: str, repository_id: str) -> str:
    """Claim one dispatch process, replacing only a proved pre-runner corpse."""
    database = central_database.path(data_root)
    current_owner = _owner(submission_id, project_id, repository_id)
    for _ in range(3):
        with sqlite_connection(database) as connection:
            observed = _active(connection, run_id)
        if len(observed) > 1:
            raise RecoveryFenceError("PA_E3_DISPATCH_FENCE_CONFLICT")
        prior = observed[0] if observed else None
        if prior is not None:
            owner = _parse_owner(prior[1], submission_id=submission_id,
                                 project_id=project_id, repository_id=repository_id)
            _prove_prior_process_stopped(owner)
        outcome = "RETRY"
        attempt_id = _ATTEMPT + run_id + ":" + uuid4().hex
        with sqlite_connection(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            locked = _active(connection, run_id)
            if locked != observed:
                continue
            dispatch = connection.execute(
                "SELECT submission_id,project_id,repository_id,state "
                "FROM ep_parity_lifecycle_dispatches WHERE run_id=?", (run_id,),
            ).fetchone()
            if (dispatch is None or tuple(dispatch[:3])
                    != (submission_id, project_id, repository_id)
                    or dispatch[3] not in {"CLAIMED", "RUNNING"}):
                outcome = "PA_E3_DISPATCH_TARGET_INVALID"
            elif status(connection, run_id) == "PROVIDER_EFFECT_UNCERTAIN":
                outcome = "PA_E3_PROVIDER_EFFECT_UNCERTAIN"
            elif _unreturned_entries(connection, run_id):
                connection.execute(
                    "INSERT OR IGNORE INTO ep_execution_leases "
                    "(lease_id,run_id,holder_id,acquired_at,expires_at) VALUES(?,?,?,?,?)",
                    (_UNCERTAIN + run_id, run_id, "PROVIDER_EFFECT_UNCERTAIN", _now(), _FOREVER),
                )
                outcome = "PA_E3_PROVIDER_EFFECT_UNCERTAIN"
            else:
                if prior is not None:
                    connection.execute(
                        "UPDATE ep_execution_leases SET released_at=? "
                        "WHERE lease_id=? AND holder_id=? AND released_at IS NULL",
                        (_now(), prior[0], prior[1]),
                    )
                connection.execute(
                    "INSERT INTO ep_execution_leases "
                    "(lease_id,run_id,holder_id,acquired_at,expires_at) VALUES(?,?,?,?,?)",
                    (attempt_id, run_id, current_owner, _now(), _FOREVER),
                )
                outcome = "CLAIMED"
        if outcome == "CLAIMED":
            return attempt_id
        if outcome != "RETRY":
            raise RecoveryFenceError(outcome)
    raise RecoveryFenceError("PA_E3_DISPATCH_FENCE_CONFLICT")


def runner_entry(data_root: Path, *, run_id: str, attempt_id: str) -> str:
    """Persist the last point at which restart may safely re-enter a run."""
    current = capture_process_identity(os.getpid())
    if current is None:
        raise RecoveryFenceError("PA_E3_PROCESS_IDENTITY_UNAVAILABLE")
    with sqlite_connection(central_database.path(data_root)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = _active(connection, run_id)
        if len(rows) != 1 or rows[0][0] != attempt_id:
            raise RecoveryFenceError("PA_E3_DISPATCH_FENCE_CONFLICT")
        dispatch = connection.execute(
            "SELECT submission_id,project_id,repository_id FROM ep_parity_lifecycle_dispatches "
            "WHERE run_id=?", (run_id,),
        ).fetchone()
        if dispatch is None:
            raise RecoveryFenceError("PA_E3_DISPATCH_TARGET_INVALID")
        owner = _parse_owner(rows[0][1], submission_id=str(dispatch[0]),
                             project_id=str(dispatch[1]), repository_id=str(dispatch[2]))
        if (owner["pid"] != current.pid
                or owner["group"] != current.process_group
                or owner["birth"] != current.start_fingerprint
                or owner["executable"] != current.executable_identity):
            raise RecoveryFenceError("PA_E3_DISPATCH_FENCE_CONFLICT")
        if status(connection, run_id) == "PROVIDER_EFFECT_UNCERTAIN":
            raise RecoveryFenceError("PA_E3_PROVIDER_EFFECT_UNCERTAIN")
        if status(connection, run_id) == "CANCEL_REQUESTED":
            return "CANCEL_REQUESTED"
        if connection.execute(
            "SELECT 1 FROM ep_execution_leases WHERE lease_id=?",
            (_entered_id(attempt_id),),
        ).fetchone():
            raise RecoveryFenceError("PA_E3_RUNNER_ALREADY_ENTERED")
        connection.execute(
            "INSERT INTO ep_execution_leases "
            "(lease_id,run_id,holder_id,acquired_at,expires_at) VALUES(?,?,?,?,?)",
            (_entered_id(attempt_id), run_id, "RUNNER_ENTERED", _now(), _FOREVER),
        )
    return "RUNNER_ENTERED"


def release_attempt(data_root: Path, *, run_id: str, attempt_id: str) -> None:
    with sqlite_connection(central_database.path(data_root)) as connection:
        connection.execute(
            "UPDATE ep_execution_leases SET released_at=? WHERE lease_id=? "
            "AND run_id=? AND released_at IS NULL",
            (_now(), attempt_id, run_id),
        )


def complete_attempt(data_root: Path, *, run_id: str, attempt_id: str,
                     checkpoint_phase: str) -> None:
    """Record a synchronous, child-free runner return before permitting resume."""
    if not checkpoint_phase or checkpoint_phase in {"COMPLETE", "BLOCKED", "FAILED"}:
        raise RecoveryFenceError("PA_E3_CHECKPOINT_INVALID")
    with sqlite_connection(central_database.path(data_root)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = _active(connection, run_id)
        if len(rows) != 1 or rows[0][0] != attempt_id:
            raise RecoveryFenceError("PA_E3_DISPATCH_FENCE_CONFLICT")
        if not connection.execute(
            "SELECT 1 FROM ep_execution_leases WHERE lease_id=?",
            (_entered_id(attempt_id),),
        ).fetchone():
            raise RecoveryFenceError("PA_E3_RUNNER_ENTRY_MISSING")
        now = _now()
        connection.execute(
            "INSERT INTO ep_execution_leases "
            "(lease_id,run_id,holder_id,acquired_at,expires_at) VALUES(?,?,?,?,?)",
            (_returned_id(attempt_id), run_id, checkpoint_phase, now, _FOREVER),
        )
        connection.execute(
            "UPDATE ep_execution_leases SET released_at=? WHERE lease_id=? AND run_id=? "
            "AND released_at IS NULL", (now, attempt_id, run_id),
        )
        # Release the slot in the same commit as the returned checkpoint.
        # A new process can acquire this run immediately after this commit;
        # a later run-wide release would otherwise erase its fresh slot.
        parallel_action_delivery.release_capacity(connection, run_id)


def mark_uncertain(data_root: Path, *, run_id: str) -> None:
    """Retain provider and repository capacity after an unacknowledged runner exit."""
    with sqlite_connection(central_database.path(data_root)) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO ep_execution_leases "
            "(lease_id,run_id,holder_id,acquired_at,expires_at) VALUES(?,?,?,?,?)",
            (_UNCERTAIN + run_id, run_id, "PROVIDER_EFFECT_UNCERTAIN", _now(), _FOREVER),
        )


def require_resolved(connection: sqlite3.Connection, run_id: str) -> None:
    state = status(connection, run_id)
    if state == "PROVIDER_EFFECT_UNCERTAIN":
        raise RecoveryFenceError("PA_E3_PROVIDER_EFFECT_UNCERTAIN")
    if state == "CANCEL_REQUESTED":
        raise RecoveryFenceError("PA_E3_CANCEL_ACK_PENDING")


def request_cancel(data_root: Path, *, project_id: str, run_id: str,
                   audit_actor: str | None = None) -> dict[str, str]:
    """Persist one exact per-Action request; it is not an effect acknowledgement."""
    with sqlite_connection(central_database.path(data_root)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT d.submission_id,d.repository_id,d.state,i.policy_digest "
            "FROM ep_parity_lifecycle_dispatches d "
            "JOIN ep_parallel_action_submission_links l ON l.submission_id=d.submission_id "
            "JOIN ep_parallel_action_intakes i ON i.intake_id=l.intake_id "
            "WHERE d.project_id=? AND d.run_id=?",
            (project_id, run_id),
        ).fetchone()
        if (row is None or row[2] not in {"CLAIMED", "RUNNING"}
                or row[3] != parallel_action_delivery._pa_e2_digest()):
            raise RecoveryFenceError("PA_E3_CANCEL_TARGET_UNAVAILABLE")
        if status(connection, run_id) == "PROVIDER_EFFECT_UNCERTAIN":
            raise RecoveryFenceError("PA_E3_PROVIDER_EFFECT_UNCERTAIN")
        unreturned = _unreturned_entries(connection, run_id)
        if unreturned:
            attempts = _active(connection, run_id)
            if len(unreturned) != 1 or len(attempts) != 1 or unreturned[0] != _entered_id(attempts[0][0]):
                raise RecoveryFenceError("PA_E3_CANCEL_PRIOR_EFFECT_UNQUALIFIED")
        existing = connection.execute(
            "SELECT released_at FROM ep_execution_leases WHERE lease_id=?",
            (_CANCEL + run_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO ep_execution_leases "
                "(lease_id,run_id,holder_id,acquired_at,expires_at) VALUES(?,?,?,?,?)",
                (_CANCEL + run_id, run_id, "REQUESTED", _now(), _FOREVER),
            )
        elif existing[0] is not None:
            raise RecoveryFenceError("PA_E3_CANCEL_ALREADY_RESOLVED")
        if audit_actor is not None:
            # The accepted intent and its Console audit fact are one CENTRAL
            # transaction. A log sink failure must not leave an unaudited
            # cancellation request behind.
            now = _now()
            payload = json.dumps({
                "timestamp": now, "level": "INFO", "component": "operations_console",
                "run_id": run_id, "event": "dashboard_action_completed",
                "project_id": project_id, "audit_action": "execution_cancel_requested",
                "audit_actor": audit_actor, "audit_outcome": "COMPLETED",
                "user_action": "execution_cancel_requested",
            }, separators=(",", ":"))
            connection.execute(
                "INSERT INTO engineering_component_logs(component,payload,created_at) "
                "VALUES('operations_console',?,?)", (payload, now),
            )
    return {"run_id": run_id, "project_id": project_id,
            "repository_id": str(row[1]), "state": "CANCEL_REQUESTED"}


def acknowledge_pre_runner_cancel(data_root: Path, *, run_id: str,
                                  submission_id: str, attempt_id: str) -> None:
    """A cancelled pre-provider run has no external effect or resource hold."""
    with sqlite_connection(central_database.path(data_root)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if status(connection, run_id) != "CANCEL_REQUESTED":
            raise RecoveryFenceError("PA_E3_CANCEL_NOT_REQUESTED")
        if _unreturned_entries(connection, run_id):
            raise RecoveryFenceError("PA_E3_PROVIDER_EFFECT_UNCERTAIN")
        attempts = _active(connection, run_id)
        if len(attempts) != 1 or attempts[0][0] != attempt_id:
            raise RecoveryFenceError("PA_E3_DISPATCH_FENCE_CONFLICT")
        now = _now()
        earlier_effects = connection.execute(
            "SELECT 1 FROM ep_execution_leases WHERE run_id=? AND lease_id LIKE ?",
            (run_id, _ENTERED + run_id + ":%"),
        ).fetchone() is not None
        resolution = "OPEN" if earlier_effects else "DISMISSED"
        changed = connection.execute(
            "UPDATE ep_parity_lifecycle_dispatches SET state='FAILED',"
            "operator_resolution=?,updated_at=? "
            "WHERE run_id=? AND submission_id=? AND state IN ('CLAIMED','RUNNING')",
            (resolution, now, run_id, submission_id),
        ).rowcount
        if changed != 1:
            raise RecoveryFenceError("PA_E3_CANCEL_TARGET_UNAVAILABLE")
        connection.execute("UPDATE ep_execution_runs SET state='FAILED',updated_at=? WHERE run_id=?",
                           (now, run_id))
        parallel_action_delivery.release_capacity(connection, run_id)
        if not earlier_effects:
            parallel_action_delivery.release_resources(connection, run_id)
        connection.execute(
            "UPDATE ep_execution_leases SET released_at=? WHERE lease_id IN (?,?) "
            "AND run_id=? AND released_at IS NULL",
            (now, attempt_id, _CANCEL + run_id, run_id),
        )


def cancel_requested(data_root: Path, run_id: str) -> bool:
    with sqlite_connection(central_database.path(data_root)) as connection:
        return status(connection, run_id) == "CANCEL_REQUESTED"


def acknowledge_stopped_provider_in_transaction(connection: sqlite3.Connection, *,
                                                run_id: str, submission_id: str,
                                                attempt_id: str) -> None:
    """Acknowledge a stopped provider inside the caller's terminal commit."""
    if status(connection, run_id) != "CANCEL_REQUESTED":
        raise RecoveryFenceError("PA_E3_CANCEL_NOT_REQUESTED")
    if not connection.execute(
        "SELECT 1 FROM ep_execution_leases WHERE lease_id=?",
        (_entered_id(attempt_id),),
    ).fetchone():
        raise RecoveryFenceError("PA_E3_PROVIDER_EFFECT_UNCERTAIN")
    attempts = _active(connection, run_id)
    if len(attempts) != 1 or attempts[0][0] != attempt_id:
        raise RecoveryFenceError("PA_E3_DISPATCH_FENCE_CONFLICT")
    if _unreturned_entries(connection, run_id) != [_entered_id(attempt_id)]:
        raise RecoveryFenceError("PA_E3_CANCEL_PRIOR_EFFECT_UNQUALIFIED")
    eligible = connection.execute(
        "SELECT 1 FROM ep_parity_lifecycle_dispatches "
        "WHERE submission_id=? AND run_id=? AND state IN ('BLOCKED','FAILED') "
        "AND operator_resolution='OPEN'",
        (submission_id, run_id),
    ).fetchone()
    if eligible is None:
        raise RecoveryFenceError("PA_E3_CANCEL_TARGET_UNAVAILABLE")
    parallel_action_delivery.release_capacity(connection, run_id)
    # The provider stopped, but its earlier workspace/PR effects remain
    # for operator review. Keep this repository exclusive until dismissal.
    connection.execute(
        "UPDATE ep_execution_leases SET released_at=? WHERE lease_id IN (?,?) "
        "AND run_id=? AND released_at IS NULL",
        (_now(), attempt_id, _CANCEL + run_id, run_id),
    )


def acknowledge_stopped_provider(data_root: Path, *, run_id: str,
                                 submission_id: str, attempt_id: str) -> None:
    """Acknowledge only after this invocation observed its provider group exit."""
    with sqlite_connection(central_database.path(data_root)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        acknowledge_stopped_provider_in_transaction(
            connection, run_id=run_id, submission_id=submission_id,
            attempt_id=attempt_id,
        )


def close_terminal_cancel_in_transaction(connection: sqlite3.Connection, *, run_id: str) -> None:
    """Close a late cancellation within the same terminal state transaction."""
    if status(connection, run_id) != "CANCEL_REQUESTED":
        return
    row = connection.execute(
        "SELECT state FROM ep_parity_lifecycle_dispatches WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None or row[0] not in {"COMPLETE", "BLOCKED", "FAILED"}:
        return
    connection.execute(
        "INSERT OR IGNORE INTO ep_execution_leases "
        "(lease_id,run_id,holder_id,acquired_at,expires_at) VALUES(?,?,?,?,?)",
        (_CANCEL_OUTCOME + run_id, run_id, "COMPLETED_BEFORE_CANCELLATION", _now(), _FOREVER),
    )
    connection.execute(
        "UPDATE ep_execution_leases SET released_at=? WHERE lease_id=? AND released_at IS NULL",
        (_now(), _CANCEL + run_id),
    )


def close_terminal_cancel(data_root: Path, *, run_id: str) -> None:
    """Record that an already-ended run could not be cancelled retroactively."""
    with sqlite_connection(central_database.path(data_root)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        close_terminal_cancel_in_transaction(connection, run_id=run_id)
