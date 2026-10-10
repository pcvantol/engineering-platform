"""Typed presentation of stored run evidence; no execution authority or effects."""
from __future__ import annotations

import json
import re
from datetime import datetime

from .capability_review import specialist_readback, validate_specialist_records
from .managed_publication import validate_intent


def projection(checkpoint: dict[str, object], run_id: str, *, invocations=None) -> dict[str, object]:
    """Keep missing evidence distinct from a measured empty result.

    The caller supplies its canonical read-only checkpoint. Journal validation
    and publication identity checks are reused rather than interpreting reports.
    Reservations/results are not evidence that a native model process started.
    """
    specialists: dict[str, object] = {
        "available": False, "state": "NOT_RECORDED", "decisions": [],
        "dispatch_skips": [], "findings": [], "history": [],
        "reserved_invocation_count": None, "completed_invocation_count": None,
        "uncertain_invocation_count": None, "duplicate_observations": None,
        "actual_model_invocation_count": None,
        "invocations": [], "invocation_evidence_state": "NOT_RECORDED",
    }
    records = checkpoint.get("specialist_records")
    if isinstance(records, (tuple, list)) and records:
        try:
            bound = tuple(records)
            validate_specialist_records(bound, run_id=run_id, repository=checkpoint.get("repository"))
            specialists.update(specialist_readback(bound))
            specialists.update(available=True, state="RECORDED", history=[
                event for event in bound
                if event["kind"] in {"DISPOSITION", "APPLICATION", "VERIFICATION"}
            ])
            dispatches = {event["invocation_id"]: event for event in bound if event["kind"] == "DISPATCH"}
            if not dispatches:
                specialists.update(actual_model_invocation_count=0, invocation_evidence_state="NOT_STARTED")
            elif invocations is not None:
                observations = []
                for row in invocations:
                    if not isinstance(row["phase"], str):
                        raise ValueError("invalid invocation phase")
                    if row["phase"] not in {"CAPABILITY_REVIEW", "CAPABILITY_REVIEW_DISPATCH"}:
                        continue
                    if not isinstance(row["invocation_id"], str):
                        raise ValueError("invalid invocation identity")
                    identifier = row["invocation_id"].removesuffix(":dispatch")
                    event = dispatches.get(identifier)
                    if event is None:
                        continue
                    binding = json.loads(row["churn"])
                    if (not isinstance(binding, dict) or row["run_id"] != run_id
                            or row["role"] != "reviewer:" + event["reviewer"]
                            or binding.get("canonical_invocation_id") != identifier
                            or binding.get("assurance_profile_digest") != event["profile_digest"]
                            or any(binding.get(key) != event[key] for key in
                                   ("request_id", "source_digest", "consumer", "repository", "candidate_sha"))):
                        raise ValueError("invocation evidence conflicts with its stored request")
                    started = datetime.fromisoformat(row["started_at"])
                    completed = datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
                    if started.tzinfo is None or (completed is not None and (completed.tzinfo is None or completed < started)):
                        raise ValueError("invocation evidence has invalid observation times")
                    observations.append({"invocation_id": identifier, "request_id": event["request_id"],
                                         "reviewer": event["reviewer"], "started_at": row["started_at"],
                                         "completed_at": row["completed_at"],
                                         "state": "COMPLETED" if row["completed_at"] else "UNKNOWN"})
                if len({item["invocation_id"] for item in observations}) != len(observations):
                    raise ValueError("conflicting canonical invocation observations")
                if observations:
                    specialists.update(invocations=observations, actual_model_invocation_count=len(observations),
                                       invocation_evidence_state="RECORDED")
        except (ValueError, TypeError, KeyError):
            specialists.update(state="UNAVAILABLE", actual_model_invocation_count=None,
                               invocation_evidence_state="UNAVAILABLE", invocations=[])
    elif records is not None and not isinstance(records, (tuple, list)):
        specialists["state"] = "UNAVAILABLE"

    publication: dict[str, object] = {"available": False, "status": "NOT_RECORDED"}
    intent = checkpoint.get("publication_intent")
    if intent is not None:
        try:
            validate_intent(intent)
            if intent["run_id"] != run_id or intent["repository"] != checkpoint.get("repository"):
                raise ValueError("publication identity mismatch")
            publication = {**intent, "available": True}
        except (ValueError, TypeError, KeyError):
            publication["status"] = "UNAVAILABLE"
    return {
        "contract_version": "ep-console-run-evidence/v1", "run_id": run_id,
        "specialists": specialists, "publication": publication,
        "recorded_candidate_sha": checkpoint.get("commit_sha") if isinstance(checkpoint.get("commit_sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", checkpoint["commit_sha"]) else None,
        "recorded_execution_authorized": checkpoint.get("owner_authorized")
            if isinstance(checkpoint.get("owner_authorized"), bool) else None,
        "observation": "STORED_EVIDENCE_ONLY", "execution_authority": False,
    }
