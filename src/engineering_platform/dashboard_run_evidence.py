"""Typed presentation of stored run evidence; no execution authority or effects."""
from __future__ import annotations

import json
import re
from datetime import datetime

from .agent_state import TransactionState
from .capability_review import specialist_readback, validate_specialist_records
from .managed_publication import validate_intent


_HOST_PATH = re.compile(r'''(?<![\w/.\-])(?!(?<=:)/{2})(?!(?<=<)/(?:script|style|div|span|p|b|i|em|strong|a|code|pre|li|ul|ol|h[1-6])>)(?:file://|~?/|[A-Za-z]:[\\/]|\\\\)[^\s<>"']+''', re.IGNORECASE)


def _presentation(value, redacted):
    """Remove host paths from the detached view, never from stored evidence."""
    if isinstance(value, str):
        safe, count = _HOST_PATH.subn("[REDACTED]", value)
        redacted[0] = redacted[0] or bool(count)
        return safe
    if isinstance(value, dict):
        return {key: _presentation(item, redacted) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_presentation(item, redacted) for item in value)
    return value


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
        "failed_invocation_count": None, "successful_invocation_count": None,
        "duplicate_finding_count": None,
        "actual_model_invocation_count": None,
        "invocations": [], "outcomes": [], "invocation_evidence_state": "NOT_RECORDED",
    }
    records = checkpoint.get("specialist_records")
    if isinstance(records, (tuple, list)) and records:
        try:
            bound = tuple(records)
            validate_specialist_records(bound, run_id=run_id, repository=checkpoint.get("repository"))
            specialists.update(specialist_readback(bound))
            terminals = {event["invocation_id"]: event for event in bound
                         if event["kind"] in {"RESULT", "UNCERTAIN"}}
            specialists.update(
                outcomes=[{"request_id": event["request_id"], "invocation_id": event["invocation_id"],
                           "state": "UNCERTAIN" if event["kind"] == "UNCERTAIN" else event["payload"]["status"]}
                          for event in terminals.values()],
                failed_invocation_count=sum(event["kind"] == "RESULT" and event["payload"]["status"] == "FAILED" for event in terminals.values()),
                successful_invocation_count=sum(event["kind"] == "RESULT" and event["payload"]["status"] == "COMPLETE" for event in terminals.values()),
                duplicate_finding_count=sum(item["disposition"] == "DUPLICATE" for item in specialists["findings"]),
            )
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
                    terminal = terminals.get(identifier)
                    outcome = ("UNCERTAIN" if terminal["kind"] == "UNCERTAIN" else terminal["payload"]["status"]) if terminal else "UNKNOWN"
                    observations.append({"invocation_id": identifier, "request_id": event["request_id"],
                                         "reviewer": event["reviewer"], "started_at": row["started_at"],
                                         "completed_at": row["completed_at"],
                                         "state": outcome})
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
    # TransactionState has no generic commit_sha field. Read the validated
    # stored assurance candidate, never the current target Git HEAD.
    recorded_candidate = None
    try:
        state = TransactionState.from_dict(checkpoint)
        if state.run_id == run_id and state.assurance_profile is not None:
            recorded_candidate = state.assurance_profile["candidate_sha"]
    except (ValueError, TypeError, KeyError):
        pass
    redacted = [False]
    specialists = _presentation(specialists, redacted)
    publication = _presentation(publication, redacted)
    return {
        "contract_version": "ep-console-run-evidence/v1", "run_id": run_id,
        "specialists": specialists, "publication": publication,
        "presentation_redacted": redacted[0],
        "recorded_candidate_sha": recorded_candidate,
        "recorded_execution_authorized": checkpoint.get("owner_authorized")
            if isinstance(checkpoint.get("owner_authorized"), bool) else None,
        "observation": "STORED_EVIDENCE_ONLY", "execution_authority": False,
    }
