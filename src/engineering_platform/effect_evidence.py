"""Canonical FME evidence indexing and independent receipt verification."""
from __future__ import annotations

from pathlib import Path

from .capability_review import mandatory_coverage_surfaces
from .effect_contract import EffectContractError, digest
from .effect_state import subject, validate
from .effect_validation import control_ids
from .effect_workspace import read_json
from .storage import sqlite_connection


def required_controls(checkpoint: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return (tuple((name, "host_control") for name in control_ids(checkpoint["contract"]))
            + tuple((item["validation_id"], item["category"]) for item in checkpoint["validation_bindings"]))


def profile_digest(checkpoint: dict[str, object], attempt: dict[str, object]) -> str:
    return "sha256:" + digest({"version": "effect-validation@1.0", "subject": subject(checkpoint, attempt),
                              "controls": required_controls(checkpoint),
                              "validation_bindings": checkpoint["validation_bindings"]})


def register(database: Path, checkpoint: dict[str, object], path: Path, *, artifact_id: str,
             artifact_type: str, fingerprint: str, created_at: str) -> None:
    """Use the existing artifact catalog with immutable insert-and-compare semantics."""
    read_json(path, fingerprint)
    identity = checkpoint["identity"]
    location = str(path.relative_to(database.resolve().parent / "artifacts"))
    record = (artifact_type, "sha256", fingerprint, "application/json", identity["mission_id"],
              identity["producer_id"], identity["run_id"], identity["submission_id"], created_at,
              "VERIFIED", location, "AVAILABLE")
    columns = ("artifact_type,digest_algorithm,digest,content_type,mission_id,producer_id,ep_run_id,"
               "ep_submission_id,created_at,integrity_status,storage_location,projection_status")
    with sqlite_connection(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT OR IGNORE INTO execution_artifact_records(artifact_id," + columns + ") "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (artifact_id, *record))
        stored = connection.execute("SELECT " + columns + " FROM execution_artifact_records WHERE artifact_id=?",
                                    (artifact_id,)).fetchone()
        if stored is None or tuple(stored) != record:
            raise EffectContractError("EFFECT_ARTIFACT_IDENTITY_CONFLICT")
        connection.commit()


def _artifact(connection, checkpoint, directory, filename, artifact_id, fingerprint):
    identity = checkpoint["identity"]
    row = connection.execute(
        "SELECT digest,producer_id,ep_run_id,ep_submission_id,storage_location,integrity_status "
        "FROM execution_artifact_records WHERE artifact_id=? AND digest_algorithm='sha256' "
        "AND content_type='application/json' AND projection_status='AVAILABLE'", (artifact_id,),
    ).fetchone()
    expected = (fingerprint, identity["producer_id"], identity["run_id"], identity["submission_id"],
                str(Path("effects") / identity["run_id"] / filename), "VERIFIED")
    if row is None or tuple(row) != expected:
        raise EffectContractError("EFFECT_ARTIFACT_CATALOG_MISMATCH")
    return read_json(directory / filename, fingerprint)


def _provider(connection, run_id, invocation_id, role, ordinal, started, completed=None):
    row = connection.execute(
        "SELECT run_id,role,ordinal,started_at,completed_at FROM provider_invocations "
        "WHERE invocation_id=? AND provider='codex_cli'", (invocation_id,),
    ).fetchone()
    if (row is None or tuple(row[:4]) != (run_id, role, ordinal, started)
            or not row[4] or (completed is not None and row[4] != completed)):
        raise EffectContractError("EFFECT_PROVIDER_RECEIPT_MISMATCH")


def verify(database: Path, checkpoint: dict[str, object], directory: Path) -> None:
    """A checkpoint PASS is insufficient without immutable command/provider observations."""
    validate(checkpoint)
    attempt = checkpoint["attempts"][-1]
    ordinal = attempt["ordinal"]
    run_id = checkpoint["identity"]["run_id"]
    selected = required_controls(checkpoint)
    expected_profile = profile_digest(checkpoint, attempt)
    expected_subject = subject(checkpoint, attempt)
    with sqlite_connection(database) as connection:
        _artifact(connection, checkpoint, directory, f"result-{ordinal}.json",
                  attempt["result"]["artifact_id"], attempt["result"]["sha256"])
        _provider(connection, run_id, attempt["invocation_id"], "implementation", ordinal * 3 + 1,
                  attempt["started_at"])
        for index, receipt in enumerate(attempt["controls"]):
            if (index >= len(selected) or (receipt["validation_id"], receipt["authority"]) != selected[index]
                    or receipt["profile_digest"] != expected_profile
                    or receipt["command_id"] != f"{run_id}:effect:{ordinal}:{index}"):
                raise EffectContractError("EFFECT_CONTROL_IDENTITY_CHANGED")
            start = connection.execute(
                "SELECT validation_id,category,control_identity,required_for_profile,started_at,currentness "
                "FROM execution_validation_command_invocations WHERE run_id=? AND command_id=?",
                (run_id, receipt["command_id"]),
            ).fetchone()
            terminal = connection.execute(
                "SELECT completed_at,exit_code,result FROM execution_validation_command_terminals "
                "WHERE run_id=? AND command_id=?", (run_id, receipt["command_id"]),
            ).fetchone()
            result = connection.execute(
                "SELECT result,execution_status,category,required_for_profile,observed_at "
                "FROM execution_validation_control_results WHERE run_id=? AND validation_id=? AND currentness=?",
                (run_id, receipt["validation_id"], ordinal),
            ).fetchone()
            name, authority = selected[index]
            if (start is None or tuple(start) != (name, authority, name, 1, receipt["started_at"], ordinal)
                    or terminal is None or tuple(terminal) != (receipt["completed_at"], receipt["exit_code"], receipt["status"])
                    or result is None or tuple(result) != (receipt["status"], "EXECUTED", authority, 1, receipt["completed_at"])):
                raise EffectContractError("EFFECT_CONTROL_RECEIPT_MISMATCH")
        for index, review in enumerate(attempt["reviews"]):
            role = ("quality", "security")[index]
            if (review["reviewer"] != role or review["subject"] != expected_subject
                    or review["profile_digest"] != expected_profile
                    or review["invocation_id"] != f"{run_id}:{role}:effect:{ordinal}"):
                raise EffectContractError("EFFECT_REVIEW_IDENTITY_CHANGED")
            _provider(connection, run_id, review["invocation_id"], role, ordinal * 3 + index + 2,
                      review["started_at"], review["completed_at"])
            _artifact(connection, checkpoint, directory, f"review-{ordinal}-{role}.json",
                      f"effect-review:{run_id}:{ordinal}:{role}", digest(review))
            if review["status"] == "PASS":
                prior_ids = {finding["id"] for previous in checkpoint["attempts"][:ordinal]
                             for prior in previous["reviews"] if prior["reviewer"] == role
                             for finding in prior["findings"] if finding["blocking"]}
                surfaces = mandatory_coverage_surfaces(role, "EFFECT_RESULT")
                if (review["findings"] or len(review["coverage"]) != len(surfaces)
                        or {item.get("surface") for item in review["coverage"]} != set(surfaces)
                        or any(item.get("status") != "REVIEWED" or not str(item.get("evidence_ref", "")).startswith(
                            expected_subject["subject_digest"]) for item in review["coverage"])
                        or len(review["finding_dispositions"]) != len(prior_ids)
                        or {item.get("finding_id") for item in review["finding_dispositions"]} != prior_ids
                        or any(item.get("disposition") != "RESOLVED" for item in review["finding_dispositions"])):
                    raise EffectContractError("EFFECT_REVIEW_COVERAGE_INCOMPLETE")


def qualified(checkpoint: dict[str, object]) -> bool:
    attempt = checkpoint["attempts"][-1]
    return (len(attempt["controls"]) == len(required_controls(checkpoint)) and len(attempt["reviews"]) == 2
            and all(item["status"] == "PASS" for item in (*attempt["controls"], *attempt["reviews"])))
