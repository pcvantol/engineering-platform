"""Durable, one-shot provider interruption recovery evidence.

This module deliberately owns storage transitions only.  The Execution Host
owns lifecycle decisions and the watcher only decides whether a host may be
resumed for an already existing run.
"""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from .provider_process_identity import ProcessIdentity, capture_process_identity, verify_process_identity
from .storage import EngineeringStorageError, open_storage, record_artifact, verify_artifact_integrity
from .execution_lease import liveness as lease_liveness
from .agent_state import PHASES, StateError, StateStore, redact_diagnostic


RECOVERY_STATES = frozenset({
    "RECOVERY_AVAILABLE", "RECOVERY_STARTING", "RECOVERY_IN_PROGRESS",
    "RECOVERED", "EXHAUSTED", "PRECHECK_FAILED", "AMBIGUOUS",
})
ACTIVE_RECOVERY_STATES = frozenset({"RECOVERY_AVAILABLE", "RECOVERY_STARTING", "RECOVERY_IN_PROGRESS"})
TERMINAL_RECOVERY_STATES = RECOVERY_STATES - ACTIVE_RECOVERY_STATES
CONTROLLED_INTERRUPTION_PHASES = frozenset({"QUALITY_CONTROL_AGENT"})
CONTROL_DIRECTORY = Path(".engineering/artifacts/provider-recovery-fault-injection")


def _connection(root: Path, central_database: Path | None = None) -> sqlite3.Connection:
    """Open recovery authority from an explicit CENTRAL binding when supplied."""
    if central_database is None:
        return open_storage(root)
    database = central_database.resolve()
    if not database.is_file():
        raise EngineeringStorageError("CENTRAL recovery database is unavailable")
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


class ControlledInterruptionControlError(ValueError):
    """A bounded operator control request is invalid or unsafe."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _control_paths(root: Path, run_id: str, phase: str) -> tuple[Path, Path]:
    directory = root / CONTROL_DIRECTORY
    return directory / "armed" / f"{run_id}-{phase}.json", directory / f"{run_id}-{phase}.json"


def controlled_interruption_status(root: Path, *, run_id: str, phase: str) -> str:
    armed, consumed = _control_paths(root, run_id, phase)
    if consumed.is_file():
        return "CONSUMED"
    return "ARMED" if armed.is_file() else "NOT_ARMED"


def _validate_control_target(root: Path, *, run_id: str, phase: str) -> object:
    if phase not in CONTROLLED_INTERRUPTION_PHASES:
        raise ControlledInterruptionControlError("phase is not supported for controlled interruption")
    try:
        state = StateStore(root / ".engineering" / "engineering-runs").load(run_id)
    except StateError as error:
        raise ControlledInterruptionControlError(str(error)) from error
    if state.terminal:
        raise ControlledInterruptionControlError("run is terminal")
    # The hook is deliberately offered only before its lifecycle boundary.  A
    # phase already entered may have an active provider that cannot be raced.
    if state.phase not in PHASES or state.phase in {"QUALITY_CONTROL_AGENT", "REPAIR_AGENT", "FINALIZE_AGENT", "RECONCILE_AGENT", "WAIT_FOR_TERMINAL_EVIDENCE", "WAIT_FOR_OPERATOR_MERGE", "REPOSITORY_CLEANUP"}:
        raise ControlledInterruptionControlError("target phase is already active or has passed")
    return state


def arm_controlled_interruption(root: Path, *, run_id: str, phase: str, armed_by: str | None = None, reason: str | None = None) -> dict[str, object]:
    _validate_control_target(root, run_id=run_id, phase=phase)
    armed, consumed = _control_paths(root, run_id, phase)
    if consumed.is_file():
        raise ControlledInterruptionControlError("controlled interruption is already consumed")
    payload = {"version": 1, "state": "ARMED", "run_id": run_id, "phase": phase, "armed_at": _now(), "armed_by": redact_diagnostic(armed_by or os.environ.get("USER", "operator"), limit=120)}
    if reason:
        payload["reason"] = redact_diagnostic(reason, limit=240)
    armed.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(armed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise ControlledInterruptionControlError("controlled interruption is already armed") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return payload


def disarm_controlled_interruption(root: Path, *, run_id: str, phase: str) -> str:
    armed, consumed = _control_paths(root, run_id, phase)
    if consumed.is_file():
        raise ControlledInterruptionControlError("controlled interruption is already consumed and cannot be disarmed")
    try:
        armed.unlink()
    except FileNotFoundError:
        return "NOT_ARMED"
    return "DISARMED"


def consume_controlled_interruption_hook(root: Path, *, run_id: str, phase: str) -> bool:
    """Durably consume the explicit qualification-only interruption hook.

    The marker is a run-bound artifact rather than a provider recovery row:
    the first invocation has not yet produced the canonical interruption
    evidence needed to create that row.  ``O_EXCL`` makes consumption survive
    a host crash in that small interval and prevents an unchanged environment
    setting from firing in a later host.  It contains no prompt or provider
    command data.
    """
    requested = os.environ.get("DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE")
    armed, path = _control_paths(root, run_id, phase)
    durable_armed = armed.is_file()
    if (requested != f"{run_id}:{phase}" and not durable_armed) or load_recovery_state(root, run_id) is not None:
        return False
    artifact_id = f"provider-recovery-fault-injection:{run_id}:{phase}"
    directory = root / CONTROL_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "kind": "CONTROLLED_PROVIDER_INTERRUPTION",
        "run_id": run_id,
        "phase": phase,
        "consumed_at": _now(),
    }
    try:
        if durable_armed:
            # Claim the ARMED record by atomically turning it into the existing
            # consumed marker. A concurrent disarm therefore cannot erase an
            # already-claimed proof, and a crash cannot permit a refire.
            os.replace(armed, path)
            descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
        else:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileNotFoundError:
        return False
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        record_artifact(
            root, path, artifact_id=artifact_id,
            artifact_type="CONTROLLED_PROVIDER_INTERRUPTION", content_type="application/json",
            created_at=str(payload["consumed_at"]), run_id=run_id,
        )
    except Exception:
        # The exclusive marker remains intentionally: after an uncertain
        # qualification-hook write, failing closed is safer than firing twice.
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage run-scoped controlled provider interruption proof controls")
    parser.add_argument("command", choices=("arm-controlled-interruption", "controlled-interruption-status", "disarm-controlled-interruption"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    root = args.repo.resolve()
    try:
        if args.command == "arm-controlled-interruption":
            payload = arm_controlled_interruption(root, run_id=args.run_id, phase=args.phase, reason=args.reason)
            print(json.dumps({"status": "ARMED", **payload}, sort_keys=True))
        elif args.command == "controlled-interruption-status":
            print(json.dumps({"status": controlled_interruption_status(root, run_id=args.run_id, phase=args.phase), "run_id": args.run_id, "phase": args.phase}, sort_keys=True))
        else:
            print(json.dumps({"status": disarm_controlled_interruption(root, run_id=args.run_id, phase=args.phase), "run_id": args.run_id, "phase": args.phase}, sort_keys=True))
    except ControlledInterruptionControlError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def load_recovery_state(root: Path, run_id: str, *, central_database: Path | None = None) -> dict[str, object] | None:
    connection = _connection(root, central_database)
    try:
        row = connection.execute(
            "SELECT run_id,recovery_ordinal,maximum_attempts,triggering_invocation_id,replacement_invocation_id,"
            "lifecycle_phase,state,requested_at,launch_claimed_at,process_receipt_id,process_pid,process_group,"
            "provider_session_id,provider_confirmed_active_at,completed_at,result,result_evidence_ref,branch,worktree_identity,lease_id,"
            "fault_injection_consumed_at,diagnostic_code FROM provider_recovery_attempts WHERE run_id=?",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    keys = ("run_id", "recovery_ordinal", "maximum_attempts", "triggering_invocation_id", "replacement_invocation_id",
            "lifecycle_phase", "state", "requested_at", "launch_claimed_at", "process_receipt_id", "process_pid",
            "process_group", "provider_session_id", "provider_confirmed_active_at", "completed_at", "result", "result_evidence_ref", "branch",
            "worktree_identity", "lease_id", "fault_injection_consumed_at", "diagnostic_code")
    return dict(zip(keys, row, strict=True))


def watcher_resume_action(root: Path, run_id: str) -> str | None:
    """Return the sole watcher action derived from durable recovery evidence.

    The watcher never allocates an invocation or interprets reports.  A host
    resume is allowed only to let the host own the corresponding controller
    transition or consume a durable result.
    """
    recovery = load_recovery_state(root, run_id)
    if recovery is None:
        return None
    if recovery.get("state") == "RECOVERY_IN_PROGRESS":
        # A verified session-bound provider is still owned work, even if its
        # original host has disappeared. The watcher must not start a second
        # host merely to rediscover it.
        if reconcile_recovery(root, run_id=run_id) == "SAME_PROVIDER_STILL_ACTIVE":
            return None
        recovery = load_recovery_state(root, run_id)
        if recovery is None:
            return None
    # A live canonical run lease proves another host still owns this run. The
    # watcher must never race it by launching a second resumed host.
    if lease_liveness(root, run_id).get("state") == "LIVE":
        return None
    state = recovery.get("state")
    if state == "RECOVERED":
        connection = open_storage(root)
        try:
            transaction = connection.execute(
                "SELECT phase FROM engineering_transactions WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        # Result consumption persists the next lifecycle phase before the
        # host returns. A later watcher scan must not consume it again.
        if transaction and transaction[0] != recovery.get("lifecycle_phase"):
            return None
    if state == "RECOVERY_AVAILABLE":
        return "RESUME_AVAILABLE"
    if state == "RECOVERY_STARTING":
        return "RECONCILE_STARTING"
    if state == "RECOVERY_IN_PROGRESS":
        return "RECONCILE_IN_PROGRESS"
    if state == "RECOVERED":
        return "CONSUME_RECOVERED"
    return None


def _receipts(root: Path, run_id: str, invocation_id: str) -> list[dict[str, object]]:
    connection = open_storage(root)
    try:
        rows = connection.execute(
            "SELECT receipt_id,run_id,invocation_id,launch_state,provider_session_id,process_pid,process_group,"
            "process_start_fingerprint,process_executable_identity,started_at,completed_at,outcome,result_evidence_ref FROM provider_invocation_receipts "
            "WHERE run_id=? AND invocation_id=? ORDER BY started_at,receipt_id",
            (run_id, invocation_id),
        ).fetchall()
    finally:
        connection.close()
    keys = (
        "receipt_id", "run_id", "invocation_id", "launch_state", "provider_session_id", "process_pid", "process_group",
        "process_start_fingerprint", "process_executable_identity", "started_at", "completed_at", "outcome", "result_evidence_ref",
    )
    return [dict(zip(keys, row, strict=True)) for row in rows]


def record_pre_execution_launch_failure(root: Path, *, run_id: str, diagnostic_code: str) -> bool:
    """Record the only proof that a claimed launch did not reach a provider.

    The real provider adapter invokes its process callback at the spawn
    boundary. Therefore this may only be written by the host while the row is
    still STARTING and no PROCESS_STARTED receipt exists.
    """
    connection = open_storage(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT replacement_invocation_id,state,provider_session_id FROM provider_recovery_attempts WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or row[1] != "RECOVERY_STARTING":
            connection.execute("ROLLBACK")
            return False
        started = connection.execute(
            "SELECT 1 FROM provider_invocation_receipts WHERE run_id=? AND invocation_id=? "
            "AND launch_state='PROCESS_STARTED'",
            (run_id, row[0]),
        ).fetchone()
        if started:
            connection.execute("ROLLBACK")
            return False
        changed = connection.execute(
            "UPDATE provider_recovery_attempts SET diagnostic_code=? WHERE run_id=? AND state='RECOVERY_STARTING'",
            (f"LAUNCH_NOT_STARTED:{diagnostic_code[:120]}", run_id),
        ).rowcount
        connection.execute("COMMIT")
        return changed == 1
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def reconcile_recovery(root: Path, *, run_id: str, verifier=verify_process_identity) -> str:
    """Classify a restart solely from immutable receipts and recovery state.

    A live provider is accepted only when its persisted session, birth
    fingerprint and executable identity verify as one invocation-bound unit.
    """
    recovery = load_recovery_state(root, run_id)
    if recovery is None:
        return "NOT_APPLICABLE"
    state = recovery.get("state")
    invocation_id = recovery.get("replacement_invocation_id")
    if not isinstance(invocation_id, str):
        mark_ambiguous(root, run_id=run_id, expected=str(state), diagnostic_code="missing_replacement_invocation")
        return "AMBIGUOUS"
    receipts = _receipts(root, run_id, invocation_id)
    claims = [receipt for receipt in receipts if receipt["launch_state"] == "CLAIMED"]
    starts = [receipt for receipt in receipts if receipt["launch_state"] == "PROCESS_STARTED"]
    terminals = [receipt for receipt in receipts if receipt["launch_state"] == "TERMINAL"]
    outcomes = {str(receipt["outcome"]) for receipt in terminals if receipt["outcome"] is not None}
    if len(claims) > 1 or len(starts) > 1 or len(terminals) > 1 or len(outcomes) > 1:
        if state in {"RECOVERY_STARTING", "RECOVERY_IN_PROGRESS"}:
            mark_ambiguous(root, run_id=run_id, expected=str(state), diagnostic_code="contradictory_receipts")
        return "AMBIGUOUS"
    if state == "RECOVERY_STARTING":
        if starts or terminals:
            # Receipt ordering/state mismatch is never repaired by a launch.
            mark_ambiguous(root, run_id=run_id, expected="RECOVERY_STARTING", diagnostic_code="starting_receipt_conflict")
            return "AMBIGUOUS"
        if not claims:
            return "LAUNCH_UNCLAIMED"
        diagnostic = str(recovery.get("diagnostic_code") or "")
        if diagnostic.startswith("LAUNCH_NOT_STARTED:"):
            return "LAUNCH_CLAIMED_PREEXEC_FAILURE"
        mark_ambiguous(root, run_id=run_id, expected="RECOVERY_STARTING", diagnostic_code="claimed_launch_unresolved")
        return "AMBIGUOUS"
    if state == "RECOVERY_IN_PROGRESS":
        if len(starts) != 1:
            mark_ambiguous(root, run_id=run_id, expected="RECOVERY_IN_PROGRESS", diagnostic_code="missing_process_started_receipt")
            return "AMBIGUOUS"
        if not terminals:
            start = starts[0]
            session_id = recovery.get("provider_session_id")
            if (
                not isinstance(session_id, str)
                or start.get("provider_session_id") != session_id
                or not isinstance(start.get("process_pid"), int)
                or not isinstance(start.get("process_group"), int)
                or not isinstance(start.get("process_start_fingerprint"), str)
                or not isinstance(start.get("process_executable_identity"), str)
            ):
                mark_ambiguous(root, run_id=run_id, expected="RECOVERY_IN_PROGRESS", diagnostic_code="provider_session_identity_invalid")
                return "AMBIGUOUS"
            verification = verifier(ProcessIdentity(
                pid=int(start["process_pid"]), process_group=int(start["process_group"]),
                start_fingerprint=str(start["process_start_fingerprint"]),
                executable_identity=str(start["process_executable_identity"]),
            ))
            if verification == "MATCH":
                return "SAME_PROVIDER_STILL_ACTIVE"
            mark_ambiguous(
                root, run_id=run_id, expected="RECOVERY_IN_PROGRESS",
                diagnostic_code="provider_process_not_active" if verification == "NOT_ACTIVE" else "provider_process_identity_mismatch",
            )
            return "AMBIGUOUS"
        outcome = next(iter(outcomes), "")
        if outcome == "SUCCESS" and isinstance(terminals[0].get("result_evidence_ref"), str):
            transition_recovery_state(
                root, run_id=run_id, expected="RECOVERY_IN_PROGRESS", target="RECOVERED",
                result="SUCCESS", result_evidence_ref=str(terminals[0]["result_evidence_ref"]),
            )
            return "RECOVERED"
        if outcome == "INTERRUPTED":
            transition_recovery_state(root, run_id=run_id, expected="RECOVERY_IN_PROGRESS", target="EXHAUSTED", result="INTERRUPTED")
            return "EXHAUSTED"
        mark_ambiguous(root, run_id=run_id, expected="RECOVERY_IN_PROGRESS", diagnostic_code="terminal_failure_or_invalid_result")
        return "AMBIGUOUS"
    return str(state)


def create_recovery_available(
    root: Path, *, run_id: str, triggering_invocation_id: str, lifecycle_phase: str,
    branch: str | None, worktree_identity: str, lease_id: str | None,
    central_database: Path | None = None,
) -> dict[str, object]:
    """Create the only automatic-recovery budget atomically and idempotently."""
    replacement = f"provider-recovery-{run_id}-{uuid4().hex[:12]}"
    now = _now()
    connection = _connection(root, central_database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO provider_recovery_attempts("
            "run_id,recovery_ordinal,maximum_attempts,triggering_invocation_id,replacement_invocation_id,"
            "lifecycle_phase,state,requested_at,branch,worktree_identity,lease_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, 1, 1, triggering_invocation_id, replacement, lifecycle_phase,
             "RECOVERY_AVAILABLE", now, branch, worktree_identity, lease_id),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    state = load_recovery_state(root, run_id, central_database=central_database)
    if state is None:
        raise EngineeringStorageError("Provider recovery evidence could not be persisted.")
    return state


def transition_recovery_state(
    root: Path, *, run_id: str, expected: str, target: str, diagnostic_code: str | None = None,
    result: str | None = None, result_evidence_ref: str | None = None,
    central_database: Path | None = None,
) -> bool:
    """Compare-and-swap one durable recovery transition."""
    if expected not in RECOVERY_STATES or target not in RECOVERY_STATES:
        raise ValueError("invalid provider recovery transition")
    completed = _now() if target in TERMINAL_RECOVERY_STATES else None
    connection = _connection(root, central_database)
    try:
        changed = connection.execute(
            "UPDATE provider_recovery_attempts SET state=?,diagnostic_code=COALESCE(?,diagnostic_code),"
            "result=COALESCE(?,result),result_evidence_ref=COALESCE(?,result_evidence_ref),"
            "completed_at=COALESCE(?,completed_at) WHERE run_id=? AND state=?",
            (target, diagnostic_code, result, result_evidence_ref, completed, run_id, expected),
        ).rowcount
    finally:
        connection.close()
    return changed == 1


def claim_replacement_launch(root: Path, *, run_id: str, central_database: Path | None = None) -> dict[str, object] | None:
    """Claim the exact persisted replacement intent once, before process spawn."""
    now = _now()
    connection = _connection(root, central_database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT replacement_invocation_id,state,provider_session_id FROM provider_recovery_attempts WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or row[1] != "RECOVERY_STARTING":
            connection.execute("ROLLBACK")
            return None
        provider_session_id = row[2] or f"provider-session-{uuid4().hex}"
        receipt_id = f"provider-launch-{uuid4().hex}"
        connection.execute(
            "INSERT INTO provider_invocation_receipts(receipt_id,run_id,invocation_id,launch_state,provider_session_id,started_at) VALUES(?,?,?,?,?,?)",
            (receipt_id, run_id, row[0], "CLAIMED", provider_session_id, now),
        )
        changed = connection.execute(
            "UPDATE provider_recovery_attempts SET provider_session_id=?,launch_claimed_at=?,process_receipt_id=? "
            "WHERE run_id=? AND state='RECOVERY_STARTING'",
            (provider_session_id, now, receipt_id, run_id),
        ).rowcount
        if changed != 1:
            connection.execute("ROLLBACK")
            return None
        connection.execute("COMMIT")
        return {"receipt_id": receipt_id, "invocation_id": row[0], "provider_session_id": provider_session_id, "claimed_at": now}
    except sqlite3.IntegrityError:
        connection.execute("ROLLBACK")
        return None
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def record_provider_started(
    root: Path, *, run_id: str, receipt_id: str, pid: int, process_group: int,
    identity: ProcessIdentity | None = None, central_database: Path | None = None,
) -> bool:
    """Append immutable process-start evidence and enter IN_PROGRESS."""
    now = _now()
    connection = _connection(root, central_database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT replacement_invocation_id,state,provider_session_id FROM provider_recovery_attempts WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or row[1] != "RECOVERY_STARTING" or not isinstance(row[2], str):
            connection.execute("ROLLBACK")
            return False
        identity = identity or capture_process_identity(pid, process_group)
        if identity is None:
            connection.execute("ROLLBACK")
            return False
        connection.execute(
            "INSERT INTO provider_invocation_receipts(receipt_id,run_id,invocation_id,launch_state,provider_session_id,process_pid,process_group,process_start_fingerprint,process_executable_identity,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"provider-start-{uuid4().hex}", run_id, row[0], "PROCESS_STARTED", row[2], pid, process_group,
             identity.start_fingerprint, identity.executable_identity, now),
        )
        changed = connection.execute(
            "UPDATE provider_recovery_attempts SET state='RECOVERY_IN_PROGRESS',process_receipt_id=?,process_pid=?,process_group=?,provider_confirmed_active_at=? WHERE run_id=? AND state='RECOVERY_STARTING'",
            (receipt_id, pid, process_group, now, run_id),
        ).rowcount
        connection.execute("COMMIT")
        return changed == 1
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def record_replacement_terminal(
    root: Path, *, run_id: str, outcome: str, result_evidence_ref: str | None = None,
) -> bool:
    """Append terminal provider evidence once and advance the recovery state.

    ``SUCCESS`` requires a separately persisted structured-result artifact.
    Interrupted attempt two is exhaustion; any unknown outcome is deliberately
    ambiguous instead of becoming another launch opportunity.
    """
    target = {
        "SUCCESS": "RECOVERED",
        "INTERRUPTED": "EXHAUSTED",
        "FAILED": "AMBIGUOUS",
    }.get(outcome)
    if target is None:
        raise ValueError("invalid provider recovery terminal outcome")
    now = _now()
    connection = open_storage(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT replacement_invocation_id,state,provider_session_id FROM provider_recovery_attempts WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or row[1] != "RECOVERY_IN_PROGRESS":
            connection.execute("ROLLBACK")
            return False
        connection.execute(
            "INSERT INTO provider_invocation_receipts(receipt_id,run_id,invocation_id,launch_state,provider_session_id,started_at,completed_at,outcome,result_evidence_ref) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"provider-terminal-{uuid4().hex}", run_id, row[0], "TERMINAL", row[2], now, now, outcome, result_evidence_ref),
        )
        changed = connection.execute(
            "UPDATE provider_recovery_attempts SET state=?,completed_at=?,result=?,result_evidence_ref=? WHERE run_id=? AND state='RECOVERY_IN_PROGRESS'",
            (target, now, outcome, result_evidence_ref, run_id),
        ).rowcount
        connection.execute("COMMIT")
        return changed == 1
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def mark_precheck_failed(root: Path, *, run_id: str, diagnostic_code: str) -> bool:
    return transition_recovery_state(
        root, run_id=run_id, expected="RECOVERY_AVAILABLE", target="PRECHECK_FAILED",
        diagnostic_code=diagnostic_code,
    )


def mark_ambiguous(root: Path, *, run_id: str, expected: str, diagnostic_code: str) -> bool:
    return transition_recovery_state(
        root, run_id=run_id, expected=expected, target="AMBIGUOUS", diagnostic_code=diagnostic_code,
    )


def persist_recovery_agent_result(root: Path, *, run_id: str, invocation_id: str, result: object) -> str:
    """Persist the bounded structured AgentResult once for post-crash consumption."""
    fields = ("terminal_state", "branch", "pull_request", "terminal_condition", "diagnostic", "repository_path", "commit_sha", "validation_evidence", "quality_evidence", "validation_disposition")
    payload = {field: getattr(result, field) for field in fields}
    artifact_id = f"provider-recovery-result:{run_id}:{invocation_id}"
    directory = root / ".engineering" / "artifacts" / "provider-recovery-results"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{invocation_id}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{invocation_id}.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    record_artifact(
        root, path, artifact_id=artifact_id, artifact_type="PROVIDER_RECOVERY_AGENT_RESULT",
        content_type="application/json", created_at=_now(), run_id=run_id, execution_id=invocation_id,
    )
    return f"artifact:{artifact_id}"


def load_recovery_agent_result(
    root: Path, reference: str, *, run_id: str, invocation_id: str,
) -> dict[str, object] | None:
    if not reference.startswith("artifact:"):
        return None
    artifact_id = reference.removeprefix("artifact:")
    if not verify_artifact_integrity(root, artifact_id):
        return None
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT storage_location,artifact_type FROM execution_artifact_records "
            "WHERE artifact_id=? AND execution_id=? AND (run_id=? OR run_id IS NULL)",
            (artifact_id, invocation_id, run_id),
        ).fetchone()
    finally:
        connection.close()
    if not row or row[1] != "PROVIDER_RECOVERY_AGENT_RESULT":
        return None
    try:
        payload = json.loads((root / ".engineering" / str(row[0])).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
