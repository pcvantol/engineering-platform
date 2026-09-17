"""Guarded reconciliation of one known terminal-evidence projection defect.

The route never changes a terminal run, accepted request, assurance record or
historical artifact bytes.  It may add one corrected v1.4 projection only when
the retained checkpoint and complete current assurance set prove that the
legacy merge writer projected the execution baseline as its candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from . import central_database, submission_service
from .agent_state import RUN_ID_PATTERN, StateError, TransactionState
from .storage import record_artifact, sqlite_connection


_OPERATION_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{7,127}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REASON = "MERGE_CANDIDATE_PROJECTION_V1"


class TerminalEvidenceReconciliationError(ValueError):
    """A stable, secret-free refusal of a terminal-evidence repair."""


def _result(row: tuple[object, ...]) -> dict[str, object]:
    return {
        "operation_id": str(row[0]),
        "run_id": str(row[1]),
        "project_id": str(row[2]),
        "source_artifact_id": str(row[3]),
        "source_digest": "sha256:" + str(row[4]),
        "replacement_artifact_id": str(row[5]),
        "replacement_digest": "sha256:" + str(row[6]),
        "reason_code": str(row[7]),
        "recorded_at": str(row[8]),
        "state": "SUCCEEDED",
    }


def _operation(connection: sqlite3.Connection, operation_id: str) -> tuple[object, ...] | None:
    row = connection.execute(
        """SELECT operation_id,run_id,project_id,source_artifact_id,source_digest,
                  replacement_artifact_id,replacement_digest,reason_code,recorded_at
             FROM ep_terminal_evidence_reconciliation_operations
            WHERE operation_id=?""",
        (operation_id,),
    ).fetchone()
    return tuple(row) if row is not None else None


def reconcile_terminal_evidence(
    data_root: Path, *, run_id: str, operation_id: str,
) -> dict[str, object]:
    """Add and select one corrected projection while retaining original bytes."""
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise TerminalEvidenceReconciliationError("RUN_ID_INVALID")
    if _OPERATION_ID.fullmatch(operation_id) is None:
        raise TerminalEvidenceReconciliationError("OPERATION_ID_INVALID")
    data_root = data_root.resolve()
    database = central_database.path(data_root)
    artifact_root = data_root / "artifacts"
    with sqlite_connection(database) as connection:
        existing = _operation(connection, operation_id)
        if existing is not None:
            if str(existing[1]) != run_id:
                raise TerminalEvidenceReconciliationError("OPERATION_ID_CONFLICT")
            return _result(existing)
        conflicting = connection.execute(
            "SELECT operation_id FROM ep_terminal_evidence_reconciliation_operations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if conflicting is not None:
            raise TerminalEvidenceReconciliationError("RUN_ALREADY_RECONCILED")
        active = submission_service._active_terminal_artifact(connection, run_id)
        row = connection.execute(
            """SELECT d.project_id,d.repository_id,d.submission_id,t.payload
                 FROM ep_parity_lifecycle_dispatches d
                 JOIN engineering_transactions t ON t.run_id=d.run_id
                WHERE d.run_id=? AND d.state='COMPLETE'""",
            (run_id,),
        ).fetchone()
    if active is None or row is None:
        raise TerminalEvidenceReconciliationError("TERMINAL_EVIDENCE_UNAVAILABLE")
    source_id, algorithm, source_digest, _content_type, _integrity, source_location = active
    if algorithm != "sha256" or not isinstance(source_digest, str):
        raise TerminalEvidenceReconciliationError("SOURCE_ARTIFACT_INVALID")
    try:
        source_path = (artifact_root / str(source_location)).resolve()
        source_path.relative_to(artifact_root.resolve())
        source_bytes = source_path.read_bytes()
        document = json.loads(source_bytes)
        checkpoint = TransactionState.from_dict(json.loads(str(row[3])))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, StateError):
        raise TerminalEvidenceReconciliationError("SOURCE_ARTIFACT_INVALID") from None
    if hashlib.sha256(source_bytes).hexdigest() != source_digest:
        raise TerminalEvidenceReconciliationError("SOURCE_ARTIFACT_INVALID")
    assurance_status, current_reviews, _reviews = submission_service._current_assurance(checkpoint)
    profile = checkpoint.assurance_profile
    candidate = profile.get("candidate_sha") if isinstance(profile, dict) else None
    repository = document.get("repository") if isinstance(document, dict) else None
    submission = document.get("submission") if isinstance(document, dict) else None
    producer = document.get("producer") if isinstance(document, dict) else None
    run = document.get("run") if isinstance(document, dict) else None
    assurance = document.get("assurance") if isinstance(document, dict) else None
    if (
        document.get("artifact_type") != "EP_TERMINAL_EVIDENCE"
        or document.get("contract_version") != "1.4"
        or not isinstance(repository, dict)
        or not isinstance(submission, dict)
        or not isinstance(producer, dict)
        or not isinstance(run, dict)
        or not isinstance(assurance, dict)
        or run.get("id") != run_id
        or run.get("outcome") != "COMPLETE"
        or submission.get("id") != row[2]
        or submission.get("project_id") != row[0]
        or repository.get("id") != row[1]
        or not isinstance(producer.get("id"), str)
        or checkpoint.run_id != run_id
        or not checkpoint.terminal
        or checkpoint.phase != "COMPLETE"
        or checkpoint.action_intent != "MUTATING_DELIVERY"
        or assurance_status != "PASS"
        or not isinstance(candidate, str)
        or _SHA.fullmatch(candidate) is None
        or assurance.get("profile") != profile
        or len(current_reviews) < 2
    ):
        raise TerminalEvidenceReconciliationError("RECONCILIATION_PRECONDITION_FAILED")
    projected_candidate = repository.get("candidate")
    if (
        projected_candidate != checkpoint.implementation_head_sha
        or projected_candidate != checkpoint.execution_baseline_sha
        or projected_candidate == candidate
    ):
        raise TerminalEvidenceReconciliationError("RECONCILIATION_DEFECT_NOT_PRESENT")

    corrected = dict(document)
    corrected["repository"] = {**repository, "candidate": candidate}
    replacement_bytes = submission_service._canonical_json_bytes(corrected)
    replacement_digest = hashlib.sha256(replacement_bytes).hexdigest()
    replacement_id = f"terminal-evidence-reconciled:{run_id}:{replacement_digest[:16]}"
    replacement_path = source_path.with_name(
        f"terminal-evidence-v1-reconciled-{replacement_digest[:16]}.json"
    )
    submission_service._write_immutable_artifact(replacement_path, replacement_bytes)
    record_artifact(
        data_root,
        replacement_path,
        artifact_id=replacement_id,
        artifact_type="EP_TERMINAL_EVIDENCE",
        content_type="application/json",
        created_at=datetime_now(),
        run_id=run_id,
        submission_id=str(row[2]),
        producer_id=str(producer["id"]),
        ep_run_id=run_id,
        ep_submission_id=str(row[2]),
        projection_status="CANDIDATE",
        central_database=database,
        artifact_root=artifact_root,
    )
    recorded_at = datetime_now()
    with sqlite_connection(database) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = _operation(connection, operation_id)
            if existing is not None:
                connection.execute("ROLLBACK")
                if str(existing[1]) != run_id:
                    raise TerminalEvidenceReconciliationError("OPERATION_ID_CONFLICT")
                return _result(existing)
            active_now = submission_service._active_terminal_artifact(connection, run_id)
            if active_now is None or str(active_now[0]) != str(source_id) or str(active_now[2]) != source_digest:
                raise TerminalEvidenceReconciliationError("SOURCE_ARTIFACT_CHANGED")
            connection.execute(
                """INSERT INTO ep_terminal_evidence_reconciliation_operations(
                    operation_id,run_id,project_id,source_artifact_id,source_digest,
                    replacement_artifact_id,replacement_digest,reason_code,recorded_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    operation_id, run_id, str(row[0]), str(source_id), source_digest,
                    replacement_id, replacement_digest, _REASON, recorded_at,
                ),
            )
            source_updated = connection.execute(
                """UPDATE execution_artifact_records SET projection_status='SUPERSEDED'
                    WHERE artifact_id=? AND digest=? AND projection_status='AVAILABLE'""",
                (str(source_id), source_digest),
            )
            replacement_updated = connection.execute(
                """UPDATE execution_artifact_records SET projection_status='AVAILABLE'
                    WHERE artifact_id=? AND digest=? AND projection_status='CANDIDATE'""",
                (replacement_id, replacement_digest),
            )
            if source_updated.rowcount != 1 or replacement_updated.rowcount != 1:
                raise TerminalEvidenceReconciliationError("PROJECTION_TRANSITION_CONFLICT")
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise
        result = _operation(connection, operation_id)
    if result is None:
        raise TerminalEvidenceReconciliationError("RECONCILIATION_RECEIPT_UNAVAILABLE")
    return _result(result)


def datetime_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="engineering-terminal-evidence-reconcile",
        description="Reconcile one exact historical terminal candidate projection.",
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = reconcile_terminal_evidence(
            args.data_root, run_id=args.run_id, operation_id=args.operation_id,
        )
    except TerminalEvidenceReconciliationError as error:
        print(json.dumps({"state": "BLOCKED", "reason_code": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
