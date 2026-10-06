"""Append-only FME receipts within the existing transaction checkpoint."""
from __future__ import annotations

import re
from datetime import datetime

from .effect_contract import EffectContractError, digest, parse, safe_path


_KEYS = {"version", "identity", "contract", "manifest", "validation_bindings", "attempts"}
_ATTEMPT = {"ordinal", "invocation_id", "started_at", "result", "controls", "reviews", "candidate_sha"}
_CONTROL = {"validation_id", "authority", "command_id", "started_at", "completed_at", "exit_code", "profile_digest", "status"}
_REVIEW = {"reviewer", "status", "subject", "profile_digest", "invocation_id", "contract_version",
           "started_at", "completed_at", "findings", "coverage", "finding_dispositions"}


def _receipt(value: object, keys: set[str]) -> None:
    if (not isinstance(value, dict) or set(value) != keys or value["status"] not in {"PASS", "FAIL"}
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(value["profile_digest"])) is None):
        raise EffectContractError("INVALID_EFFECT_OBSERVATION")
    try:
        started, completed = (datetime.fromisoformat(value[key]) for key in ("started_at", "completed_at"))
        if started.tzinfo is None or completed.tzinfo is None or completed < started:
            raise ValueError("timestamp")
    except (ValueError, TypeError) as error:
        raise EffectContractError("INVALID_EFFECT_OBSERVATION_TIME") from error


def validate(value: object) -> None:
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != _KEYS or value["version"] != "1.0"
            or not isinstance(value["identity"], dict)
            or not isinstance(value["manifest"], dict)
            or not isinstance(value["validation_bindings"], list)
            or not isinstance(value["attempts"], list) or len(value["attempts"]) > 4):
        raise EffectContractError("INVALID_EFFECT_CHECKPOINT")
    parse({"effect_contract": value["contract"]})
    identity_keys = {"run_id", "submission_id", "project_id", "repository_id", "producer_id", "producer_type",
                     "producer_version", "correlation_id", "mission_id", "engineering_action_id", "repository",
                     "source_revision", "accepted_request_digest"}
    if set(value["identity"]) != identity_keys or not value["manifest"]:
        raise EffectContractError("INVALID_EFFECT_IDENTITY")
    if (value["identity"]["source_revision"] != value["contract"]["source_revision"]
            or not all(isinstance(value["identity"][key], str) and value["identity"][key]
                       for key in identity_keys - {"producer_version", "correlation_id", "mission_id", "engineering_action_id"})):
        raise EffectContractError("INVALID_EFFECT_IDENTITY")
    binding_ids = set()
    for binding in value["validation_bindings"]:
        if (not isinstance(binding, dict) or set(binding) != {"validation_id", "category", "command"}
                or not isinstance(binding["validation_id"], str) or binding["validation_id"] in binding_ids
                or not isinstance(binding["category"], str) or not isinstance(binding["command"], list)
                or not binding["command"] or not all(isinstance(arg, str) and arg for arg in binding["command"])):
            raise EffectContractError("INVALID_EFFECT_VALIDATION_BINDING")
        binding_ids.add(binding["validation_id"])
    for path, fingerprint in value["manifest"].items():
        safe_path(path)
        if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise EffectContractError("INVALID_EFFECT_MANIFEST")
    for ordinal, attempt in enumerate(value["attempts"]):
        if (not isinstance(attempt, dict) or set(attempt) != _ATTEMPT
                or attempt["ordinal"] != ordinal or not isinstance(attempt["invocation_id"], str)
                or not isinstance(attempt["started_at"], str)
                or not isinstance(attempt["controls"], list) or not isinstance(attempt["reviews"], list)
                or len(attempt["reviews"]) > 2
                or (attempt["candidate_sha"] is not None
                    and re.fullmatch(r"[0-9a-f]{40}", str(attempt["candidate_sha"])) is None)):
            raise EffectContractError("INVALID_EFFECT_ATTEMPT")
        if attempt["result"] is not None:
            result = attempt["result"]
            if (not isinstance(result, dict) or set(result) != {"artifact_id", "sha256"}
                    or not isinstance(result["artifact_id"], str)
                    or result["artifact_id"] != f"effect-result:{value['identity']['run_id']}:{ordinal}"
                    or re.fullmatch(r"[0-9a-f]{64}", str(result["sha256"])) is None):
                raise EffectContractError("INVALID_EFFECT_RECEIPT")
        for receipt in attempt["controls"]:
            _receipt(receipt, _CONTROL)
            if (type(receipt["exit_code"]) is not int
                    or (receipt["exit_code"] == 0) != (receipt["status"] == "PASS")
                    or not all(isinstance(receipt[key], str) and receipt[key]
                               for key in ("validation_id", "authority", "command_id"))):
                raise EffectContractError("INVALID_EFFECT_CONTROL")
        for index, receipt in enumerate(attempt["reviews"]):
            _receipt(receipt, _REVIEW)
            if (receipt["reviewer"] != ("quality", "security")[index] or receipt["contract_version"] != "3.0"
                    or not isinstance(receipt["subject"], dict) or not isinstance(receipt["invocation_id"], str)
                    or any(not isinstance(receipt[key], list) or any(not isinstance(item, dict) for item in receipt[key])
                           for key in ("findings", "coverage", "finding_dispositions"))):
                raise EffectContractError("INVALID_EFFECT_REVIEW")


def transition(previous: object, current: object) -> None:
    validate(current)
    if previous is None:
        return
    validate(previous)
    if current is None or any(previous[key] != current[key] for key in _KEYS - {"attempts"}):
        raise EffectContractError("EFFECT_IDENTITY_CHANGED")
    old, new = previous["attempts"], current["attempts"]
    if len(new) < len(old) or len(new) > len(old) + 1:
        raise EffectContractError("EFFECT_ATTEMPT_ROLLBACK")
    for index, prior in enumerate(old):
        following = new[index]
        for key in ("ordinal", "invocation_id", "started_at", "result", "candidate_sha"):
            if prior[key] is not None and prior[key] != following[key]:
                raise EffectContractError("EFFECT_RECEIPT_REPLACED")
        for key in ("controls", "reviews"):
            if following[key][:len(prior[key])] != prior[key]:
                raise EffectContractError("EFFECT_OBSERVATION_REPLACED")
        if index < len(old) - 1 and prior != following:
            raise EffectContractError("EFFECT_HISTORY_CHANGED")


def subject(checkpoint: dict[str, object], attempt: dict[str, object]) -> dict[str, object]:
    receipt = attempt["result"]
    if not isinstance(receipt, dict):
        raise EffectContractError("EFFECT_RESULT_UNAVAILABLE")
    return {
        "subject_kind": "REPORT_ARTIFACT", "subject_id": receipt["artifact_id"],
        "subject_digest": "sha256:" + receipt["sha256"],
        "source_revision": checkpoint["contract"]["source_revision"],
        "source_snapshot_digest": "sha256:" + digest(checkpoint["manifest"]),
        "effect_contract_digest": "sha256:" + digest(checkpoint["contract"]),
        "criteria_digest": "sha256:" + digest(checkpoint["contract"]["criteria"]),
        "binding_digest": "sha256:" + digest(checkpoint["identity"]),
        "repair_ordinal": attempt["ordinal"],
        "candidate_revision": attempt["candidate_sha"],
    }
