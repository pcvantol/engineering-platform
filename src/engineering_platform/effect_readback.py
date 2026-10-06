"""Producer-isolated readback of the exact qualified FME artifact bytes."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .agent_state import TransactionState
from .effect_contract import EffectContractError, parse
from . import effect_evidence
from .effect_execution import _load_result
from .effect_state import subject


CONTRACT_VERSION = "1.1"
TERMINAL_CONTRACT_VERSION = "1.6"


def projection(database: Path, state: TransactionState) -> dict[str, object]:
    checkpoint = state.effect_execution
    if not isinstance(checkpoint, dict) or not checkpoint["attempts"]:
        raise EffectContractError("EFFECT_RESULT_NOT_RECORDED")
    directory = database.resolve().parent / "artifacts/effects" / state.run_id
    envelope = _load_result(directory, state)
    effect_evidence.verify(database, checkpoint, directory)
    attempt = checkpoint["attempts"][-1]
    identity = subject(checkpoint, attempt)
    controls, reviews = attempt["controls"], attempt["reviews"]
    qualified = (state.terminal and state.phase == "COMPLETE" and effect_evidence.qualified(checkpoint)
                 and (checkpoint["contract"]["delivery"] == "EVIDENCE_ONLY"
                      or state.implementation_merge_commit is not None))
    if state.phase == "COMPLETE" and not qualified:
        raise EffectContractError("EFFECT_QUALIFICATION_INCOMPLETE")
    return {"contract_version": CONTRACT_VERSION, "outcome": state.phase,
            "terminal": state.terminal, "effect_qualified": qualified,
            "subject": identity,
            "validation_profile": effect_evidence.profile_inputs(checkpoint, attempt),
            "artifact": {"id": attempt["result"]["artifact_id"], "digest_algorithm": "sha256",
                         "digest": "sha256:" + attempt["result"]["sha256"],
                         "content_type": "application/json", "content": envelope},
            "validation_controls": controls, "assurance_reviews": reviews,
            "repair_rounds": {"used": state.repair_iterations, "maximum": 3},
            "delivery": {"kind": checkpoint["contract"]["delivery"],
                         "revision": state.implementation_merge_commit, "pull_request": state.pull_request}}


def read(connection: sqlite3.Connection, *, project_id: str, submission_id: str,
         consumer_id: str) -> dict[str, object] | None:
    from .submission_service import _accepted_request_digest
    row = connection.execute(
        "SELECT s.repository_id,s.producer_type,s.producer_version,s.prompt_digest,s.constraints,"
        "s.correlation_id,s.mission_id,s.engineering_action_id,t.payload "
        "FROM ep_submissions s LEFT JOIN ep_parity_lifecycle_dispatches d ON d.submission_id=s.submission_id "
        "LEFT JOIN engineering_transactions t ON t.run_id=d.run_id "
        "WHERE s.project_id=? AND s.submission_id=? AND s.producer_id=?",
        (project_id, submission_id, consumer_id),
    ).fetchone()
    if row is None:
        return None
    constraints = json.loads(row[4])
    effects = parse(constraints)
    if effects is None:
        raise EffectContractError("EFFECT_CONTRACT_NOT_ACCEPTED")
    if row[8] is None:
        return {"contract_version": CONTRACT_VERSION, "outcome": "NOT_STARTED", "effect_qualified": False}
    state = TransactionState.from_dict(json.loads(row[8]))
    expected = _accepted_request_digest(repository_id=row[0], producer_id=consumer_id, producer_type=row[1],
        producer_version=row[2], prompt_digest=row[3], constraints=constraints, correlation_id=row[5],
        mission_id=row[6], engineering_action_id=row[7])
    checkpoint = state.effect_execution
    if (checkpoint is None or checkpoint["contract"] != effects
            or checkpoint["identity"]["accepted_request_digest"] != expected
            or checkpoint["identity"]["submission_id"] != submission_id
            or checkpoint["identity"]["project_id"] != project_id
            or checkpoint["identity"]["producer_id"] != consumer_id):
        raise EffectContractError("EFFECT_READBACK_BINDING_MISMATCH")
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    return projection(database, state)
