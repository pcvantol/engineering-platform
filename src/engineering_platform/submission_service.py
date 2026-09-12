"""Canonical, transport-neutral CENTRAL submission intake.

Adapters only turn their input into :class:`SubmissionRequest`; this module is
the single owner of project resolution, durable acceptance, idempotency and
the initial admission/queue projection.  It intentionally does *not* execute
a provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import unicodedata
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from . import central_database
from .platform_version import CURRENT_PLATFORM_VERSION


MAX_PROMPT_BYTES = 65536
MAX_FIELD_LENGTH = 128
MAX_CONSTRAINT_BYTES = 8192
VALID_TRANSPORTS = frozenset({"HTTP", "CLI", "FILE_INBOX", "DEPENDABOT", "LEGACY_FILE"})
VALID_EXECUTION_MODES = frozenset({"MANAGED", "GENESIS"})
OPERATOR_QUEUE_STATES = frozenset({"DEFERRED", "QUARANTINED", "DECLINED"})
OPERATOR_QUEUE_TRANSITIONS = {
    "QUEUED": frozenset({"DEFERRED", "QUARANTINED", "DECLINED"}),
    "DEFERRED": frozenset({"QUEUED"}),
    "QUARANTINED": frozenset({"QUEUED", "DECLINED"}),
}

# This is the complete B8D lifecycle.  The final value deliberately says what
# CENTRAL has *not* done: admission makes a submission eligible for a later
# execution protocol, but it never invokes a provider or a local Codex binary.
SUBMISSION_LIFECYCLE_VERSION = "1"
SUBMISSION_ACCEPTED = "ACCEPTED"
ADMISSION_GRANTED = "ADMITTED"
EXECUTION_NOT_DISPATCHED = "NOT_DISPATCHED"
_LIFECYCLE_EVENT_KINDS = (
    "SUBMISSION_ACCEPTED",
    "ADMISSION_GRANTED",
    "EXECUTION_NOT_DISPATCHED",
)
FORGE_PROVENANCE_CONTRACT_VERSION = "1.1"
EP_SUBMISSION_RECEIPT_CONTRACT_VERSION = "1.0"


class SubmissionError(ValueError):
    """A stable, safe submission rejection."""
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code, self.status = code, status


def operator_queue_disposition(connection: sqlite3.Connection, *, project_id: str,
                               submission_id: str, disposition: str, reason: str,
                               expected_state: str | None = None,
                               expected_revision: int | None = None,
                               operation_id: str | None = None,
                               actor_reference: str = "LEGACY_NOT_RECORDED") -> dict[str, object]:
    """Apply one auditable CENTRAL queue disposition; never delete intent."""
    if (not isinstance(submission_id, str) or not isinstance(disposition, str)
            or not isinstance(reason, str) or not isinstance(actor_reference, str)):
        raise SubmissionError("INVALID_QUEUE_DISPOSITION")
    normalized_reason = unicodedata.normalize("NFC", reason).strip()
    if (disposition not in OPERATOR_QUEUE_STATES | {"QUEUED"} or not normalized_reason
            or len(normalized_reason) > 500 or any(ord(char) < 32 and char not in "\t" for char in normalized_reason)):
        raise SubmissionError("INVALID_QUEUE_DISPOSITION")
    command_digest = hashlib.sha256(json.dumps(
        [project_id, submission_id, expected_state, expected_revision, disposition, normalized_reason],
        separators=(",", ":"),
    ).encode()).hexdigest()
    if operation_id is not None:
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
            raise SubmissionError("INVALID_QUEUE_DISPOSITION")
        prior = connection.execute(
            "SELECT command_digest,to_state,resulting_revision,recorded_at FROM ep_queue_disposition_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if prior is not None:
            if str(prior[0]) != command_digest:
                raise SubmissionError("OPERATION_ID_CONFLICT", 409)
            return {"submission_id": submission_id, "state": str(prior[1]), "reason": normalized_reason,
                    "recorded_at": str(prior[3]), "resulting_revision": int(prior[2]), "operation_id": operation_id,
                    "replayed": True}
    row = connection.execute(
        "SELECT state,admission,disposition_revision FROM ep_submissions WHERE project_id=? AND submission_id=?", (project_id, submission_id)
    ).fetchone()
    if row is None:
        raise SubmissionError("SUBMISSION_NOT_FOUND", 404)
    current, admission, revision = str(row[0]), str(row[1]), int(row[2])
    if admission != "ADMITTED" or (expected_state is not None and expected_state != current) or (expected_revision is not None and expected_revision != revision):
        raise SubmissionError("QUEUE_DISPOSITION_CONFLICT", 409)
    if connection.execute("SELECT 1 FROM ep_parity_lifecycle_dispatches WHERE submission_id=?", (submission_id,)).fetchone() is not None:
        raise SubmissionError("QUEUE_DISPOSITION_CONFLICT", 409)
    if disposition not in OPERATOR_QUEUE_TRANSITIONS.get(current, frozenset()):
        raise SubmissionError("QUEUE_DISPOSITION_CONFLICT", 409)
    now = _now()
    changed = connection.execute("UPDATE ep_submissions SET state=?,disposition_revision=disposition_revision+1 WHERE project_id=? AND submission_id=? AND state=? AND disposition_revision=?", (disposition, project_id, submission_id, current, revision)).rowcount
    if changed != 1:
        raise SubmissionError("QUEUE_DISPOSITION_CONFLICT", 409)
    event = connection.execute(
        "INSERT INTO ep_submission_events(submission_id,event_kind,payload,recorded_at) VALUES(?,?,?,?)",
        (submission_id, "OPERATOR_QUEUE_" + disposition, json.dumps({"state": disposition, "reason": normalized_reason, "actor_reference": actor_reference, "from_state": current, "previous_revision": revision, "resulting_revision": revision + 1, "operation_id": operation_id}, sort_keys=True), now),
    )
    if operation_id is not None:
        connection.execute("INSERT INTO ep_queue_disposition_operations(operation_id,project_id,submission_id,actor_reference,command_digest,from_state,to_state,previous_revision,resulting_revision,event_id,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (operation_id, project_id, submission_id, actor_reference, command_digest, current, disposition, revision, revision + 1, event.lastrowid, now))
    return {"submission_id": submission_id, "state": disposition, "reason": normalized_reason, "recorded_at": now, "previous_revision": revision, "resulting_revision": revision + 1, "operation_id": operation_id}


def _lifecycle_payload(*, transport: str, producer_id: str) -> dict[str, str]:
    """Return the one durable lifecycle meaning shared by every adapter."""
    return {
        "version": SUBMISSION_LIFECYCLE_VERSION,
        "submission": SUBMISSION_ACCEPTED,
        "admission": ADMISSION_GRANTED,
        "execution": EXECUTION_NOT_DISPATCHED,
        "transport": transport,
        "producer_id": producer_id,
    }


@dataclass(frozen=True)
class SubmissionRequest:
    project_id: str
    repository_id: str
    producer_id: str
    producer_type: str
    producer_version: str | None
    prompt: str
    transport: str
    idempotency_key: str | None = None
    correlation_id: str | None = None
    mission_id: str | None = None
    engineering_action_id: str | None = None
    constraints: Mapping[str, object] | None = None
    transport_receipt_id: str | None = None
    transport_received_at: str | None = None


@dataclass(frozen=True)
class SubmissionResult:
    submission_id: str
    project_id: str
    repository_id: str
    state: str
    created_at: str
    admission: str
    transport: str
    producer_id: str
    duplicate: bool = False
    receipt: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"submission_id": self.submission_id, "project_id": self.project_id,
                "repository_id": self.repository_id, "state": self.state,
                "created_at": self.created_at, "admission": self.admission,
                "transport": self.transport, "duplicate": self.duplicate,
                "lifecycle": _lifecycle_payload(
                    transport=self.transport,
                    producer_id=self.producer_id,
                )}
        if self.receipt is not None:
            result["receipt"] = dict(self.receipt)
        return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_FIELD_LENGTH or "\x00" in value:
        raise SubmissionError(f"INVALID_{field.upper()}")
    return value


def _json_object(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SubmissionError("INVALID_CONSTRAINTS")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise SubmissionError("INVALID_CONSTRAINTS") from None
    if len(encoded) > MAX_CONSTRAINT_BYTES or b"\0" in encoded:
        raise SubmissionError("INVALID_CONSTRAINTS")
    return dict(value)


def _transport(value: object) -> str:
    if not isinstance(value, str) or value not in VALID_TRANSPORTS:
        raise SubmissionError("INVALID_TRANSPORT")
    return value


def _validate_execution_mode(request: SubmissionRequest) -> None:
    """Reject invalid mode declarations before CENTRAL writes admission state."""
    declarations = {
        line.split(":", 1)[1].strip().upper()
        for line in request.prompt.splitlines()
        if line.strip().lower().startswith("execution mode:")
    }
    if declarations and (not declarations <= VALID_EXECUTION_MODES or len(declarations) != 1):
        raise SubmissionError("INVALID_EXECUTION_MODE")
    if declarations == {"GENESIS"}:
        targets = [line.split(":", 1)[1].strip() for line in request.prompt.splitlines() if line.strip().lower().startswith("target repository:")]
        if len(set(targets)) != 1 or not targets[0] or not Path(targets[0]).is_absolute():
            raise SubmissionError("INVALID_GENESIS_TARGET")


def _forge_provenance(request: SubmissionRequest) -> None:
    """Validate the optional Forge envelope without imposing it on other producers."""
    if request.producer_type != "FORGE":
        return
    raw = (request.constraints or {}).get("forge_execution")
    if not isinstance(raw, Mapping):
        raise SubmissionError("FORGE_PROVENANCE_REQUIRED")
    expected = {"contract_version", "host_id", "repository_id", "correlation_id", "mission_id",
                "mission_revision", "intent_id", "intent_revision", "action_id", "runtime_prompt",
                "retry_of_correlation_id"}
    contract_version = raw.get("contract_version")
    if contract_version == "1.0":
        permitted = expected
    elif contract_version == FORGE_PROVENANCE_CONTRACT_VERSION:
        permitted = expected | {"producer_contract_version", "forge_application_version"}
    else:
        permitted = set()
    if set(raw) != permitted:
        raise SubmissionError("INVALID_FORGE_PROVENANCE")
    prompt = raw.get("runtime_prompt")
    if not isinstance(prompt, Mapping) or set(prompt) != {"id", "content_digest"}:
        raise SubmissionError("INVALID_FORGE_PROVENANCE")
    digest = prompt.get("content_digest")
    values = (raw.get("host_id"), raw.get("repository_id"), raw.get("correlation_id"), raw.get("mission_id"),
              raw.get("mission_revision"), raw.get("intent_id"), raw.get("intent_revision"), raw.get("action_id"), prompt.get("id"))
    if (not all(isinstance(value, str) and value and len(value) <= MAX_FIELD_LENGTH for value in values)
            or not isinstance(digest, str) or not __import__("re").fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or raw.get("repository_id") != request.repository_id
            or raw.get("correlation_id") != request.correlation_id
            or raw.get("mission_id") != request.mission_id
            or raw.get("action_id") != request.engineering_action_id
            or (raw.get("retry_of_correlation_id") is not None and not isinstance(raw.get("retry_of_correlation_id"), str))):
        raise SubmissionError("INVALID_FORGE_PROVENANCE")
    if contract_version == FORGE_PROVENANCE_CONTRACT_VERSION and (
            raw.get("producer_contract_version") != "1.0"
            or raw.get("forge_application_version") != request.producer_version
            or not isinstance(request.producer_version, str)):
        raise SubmissionError("INVALID_FORGE_PROVENANCE")


def request_from_mapping(project_id: str, payload: object, *, transport: str) -> SubmissionRequest:
    if not isinstance(payload, Mapping):
        raise SubmissionError("MALFORMED_REQUEST")
    allowed = {"repository_id", "producer", "prompt", "idempotency_key", "correlation_id", "mission_id", "engineering_action_id", "constraints", "transport_receipt_id", "transport_received_at"}
    if set(payload) - allowed:
        raise SubmissionError("UNKNOWN_FIELD")
    producer = payload.get("producer")
    if not isinstance(producer, Mapping) or set(producer) - {"id", "type", "version"}:
        raise SubmissionError("INVALID_PRODUCER")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise SubmissionError("INVALID_PROMPT")
    request = SubmissionRequest(
        project_id=_token(project_id, "project_id") or "",
        repository_id=_token(payload.get("repository_id"), "repository_id") or "",
        producer_id=_token(producer.get("id"), "producer_id") or "",
        producer_type=_token(producer.get("type"), "producer_type") or "",
        producer_version=_token(producer.get("version"), "producer_version", optional=True),
        prompt=prompt.replace("\r\n", "\n").replace("\r", "\n"), transport=_transport(transport),
        idempotency_key=_token(payload.get("idempotency_key"), "idempotency_key", optional=True),
        correlation_id=_token(payload.get("correlation_id"), "correlation_id", optional=True),
        mission_id=_token(payload.get("mission_id"), "mission_id", optional=True),
        engineering_action_id=_token(payload.get("engineering_action_id"), "engineering_action_id", optional=True),
        constraints=_json_object(payload.get("constraints")),
        transport_receipt_id=_token(payload.get("transport_receipt_id"), "transport_receipt_id", optional=True),
        transport_received_at=_token(payload.get("transport_received_at"), "transport_received_at", optional=True),
    )
    _forge_provenance(request)
    return request


def _same_idempotent_request(row: tuple[object, ...], request: SubmissionRequest) -> bool:
    """Ensure an idempotency key cannot silently alias another submission."""
    stored = {
        "repository_id": row[0], "producer_id": row[1], "producer_type": row[2],
        "producer_version": row[3], "prompt": row[4], "constraints": row[5],
        "correlation_id": row[6], "mission_id": row[7],
        "engineering_action_id": row[8], "transport_receipt_id": row[9],
    }
    requested = {
        "repository_id": request.repository_id, "producer_id": request.producer_id,
        "producer_type": request.producer_type, "producer_version": request.producer_version,
        "prompt": request.prompt,
        "constraints": json.dumps(request.constraints or {}, sort_keys=True),
        "correlation_id": request.correlation_id, "mission_id": request.mission_id,
        "engineering_action_id": request.engineering_action_id,
        "transport_receipt_id": request.transport_receipt_id,
    }
    return stored == requested


def _persist_lifecycle_events(
    connection: sqlite3.Connection,
    *, submission_id: str,
    transport: str,
    producer_id: str,
    recorded_at: str,
) -> None:
    """Persist all three lifecycle boundaries in their canonical order."""
    payload = json.dumps(
        _lifecycle_payload(transport=transport, producer_id=producer_id), sort_keys=True,
    )
    for event_kind in _LIFECYCLE_EVENT_KINDS:
        connection.execute(
            "INSERT INTO ep_submission_events(submission_id,event_kind,payload,recorded_at) VALUES(?,?,?,?)",
            (submission_id, event_kind, payload, recorded_at),
        )


def lifecycle(connection: sqlite3.Connection, submission_id: str) -> dict[str, str]:
    """Read and validate the durable submission/admission/execution boundary.

    This is an observation helper.  It never performs dispatch and rejects a
    partial or reordered event history rather than inventing lifecycle state.
    """
    row = connection.execute(
        "SELECT producer_id,transport,state,admission FROM ep_submissions WHERE submission_id=?",
        (submission_id,),
    ).fetchone()
    if row is None:
        raise SubmissionError("UNKNOWN_SUBMISSION", 404)
    events = [
        str(event[0]) for event in connection.execute(
            "SELECT event_kind FROM ep_submission_events WHERE submission_id=? ORDER BY event_id",
            (submission_id,),
        )
    ]
    if tuple(events) != _LIFECYCLE_EVENT_KINDS or row[2:] != ("QUEUED", ADMISSION_GRANTED):
        raise SubmissionError("SUBMISSION_LIFECYCLE_INCOMPLETE", 500)
    return _lifecycle_payload(transport=str(row[1]), producer_id=str(row[0]))


def submit(connection: sqlite3.Connection, request: SubmissionRequest, *, audit_forge_exchange: bool = True) -> SubmissionResult:
    """Persist and admit one request; no provider or Agent is selected here."""
    _transport(request.transport)
    _validate_execution_mode(request)
    _forge_provenance(request)
    project = connection.execute("SELECT status FROM ep_project_registrations WHERE project_id=?", (request.project_id,)).fetchone()
    if project is None:
        raise SubmissionError("UNKNOWN_PROJECT", 404)
    if project[0] != "ACTIVE":
        raise SubmissionError("PROJECT_NOT_ACTIVE", 409)
    repository = connection.execute("SELECT project_id FROM ep_repository_registrations WHERE repository_id=?", (request.repository_id,)).fetchone()
    if repository is None:
        raise SubmissionError("UNKNOWN_REPOSITORY", 404)
    if repository[0] != request.project_id:
        raise SubmissionError("REPOSITORY_PROJECT_CONFLICT", 409)
    if request.idempotency_key:
        duplicate = connection.execute(
            "SELECT submission_id,created_at,state,admission,repository_id,producer_id,producer_type,producer_version,prompt,constraints,correlation_id,mission_id,engineering_action_id,transport_receipt_id,transport "
            "FROM ep_submissions WHERE project_id=? AND idempotency_key=?",
            (request.project_id, request.idempotency_key),
        ).fetchone()
        if duplicate is not None:
            if not _same_idempotent_request(tuple(duplicate[4:14]), request):
                raise SubmissionError("IDEMPOTENCY_CONFLICT", 409)
            lifecycle(connection, str(duplicate[0]))
            receipt = _forge_submission_receipt(
                connection, project_id=request.project_id, submission_id=str(duplicate[0]),
            ) if audit_forge_exchange else None
            return SubmissionResult(
                str(duplicate[0]), request.project_id, str(duplicate[4]), str(duplicate[2]),
                str(duplicate[1]), str(duplicate[3]), str(duplicate[14]), str(duplicate[5]), True,
                receipt,
            )
    submission_id, created_at = "sub-" + secrets.token_hex(16), _now()
    # Admission intentionally validates CENTRAL topology only at submission
    # time. Agent selection, leases and provider execution remain downstream.
    admission, state = "ADMITTED", "QUEUED"
    prompt_digest = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
    connection.execute("""INSERT INTO ep_submissions(submission_id,project_id,repository_id,producer_id,producer_type,producer_version,transport,prompt,prompt_digest,constraints,idempotency_key,correlation_id,mission_id,engineering_action_id,transport_receipt_id,transport_received_at,state,admission,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (submission_id, request.project_id, request.repository_id, request.producer_id, request.producer_type, request.producer_version, request.transport, request.prompt, prompt_digest, json.dumps(request.constraints or {}, sort_keys=True), request.idempotency_key, request.correlation_id, request.mission_id, request.engineering_action_id, request.transport_receipt_id, request.transport_received_at, state, admission, created_at))
    _persist_lifecycle_events(
        connection, submission_id=submission_id, transport=request.transport,
        producer_id=request.producer_id, recorded_at=created_at,
    )
    connection.execute("INSERT INTO ep_submission_prompt_history(submission_id,prompt_digest,recorded_at) VALUES(?,?,?)", (submission_id, prompt_digest, created_at))
    receipt = _record_forge_submission_acceptance(
        connection, request=request, submission_id=submission_id, created_at=created_at,
    ) if audit_forge_exchange else None
    lifecycle(connection, submission_id)
    return SubmissionResult(
        submission_id, request.project_id, request.repository_id, state, created_at,
        admission, request.transport, request.producer_id, receipt=receipt,
    )


def register_consumer(connection: sqlite3.Connection, *, consumer_id: str, project_id: str) -> None:
    """Explicitly grant a consumer one project scope in CENTRAL."""
    consumer_id = _token(consumer_id, "consumer_id") or ""
    project_id = _token(project_id, "project_id") or ""
    if connection.execute("SELECT 1 FROM ep_project_registrations WHERE project_id=? AND status='ACTIVE'", (project_id,)).fetchone() is None:
        raise SubmissionError("UNKNOWN_PROJECT", 404)
    now = _now()
    connection.execute("""INSERT INTO ep_consumer_registrations(consumer_id,project_id,status,created_at,updated_at,audit_metadata)
        VALUES(?,?,'ACTIVE',?,?,?) ON CONFLICT(consumer_id,project_id) DO UPDATE SET status='ACTIVE',updated_at=excluded.updated_at""", (consumer_id, project_id, now, now, json.dumps({"action": "SUBMISSION_CONSUMER_REGISTER"}, sort_keys=True)))


def issue_consumer_credential(connection: sqlite3.Connection, *, consumer_id: str, project_id: str) -> dict[str, str]:
    """Issue a scoped bearer token once; only its verifier is retained."""
    register_consumer(connection, consumer_id=consumer_id, project_id=project_id)
    from .ep_consumer_credentials import fingerprint, verifier
    token, credential_id, now = secrets.token_urlsafe(32), "production-" + secrets.token_hex(16), _now()
    connection.execute("INSERT INTO ep_consumer_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)", (credential_id, consumer_id, project_id, verifier(token), fingerprint(token), now))
    return {"credential_id": credential_id, "consumer_id": consumer_id, "project_id": project_id, "credential": token}


PRODUCER_READBACK_CONTRACT_VERSION = "1.2"
TERMINAL_EVIDENCE_CONTRACT_VERSION = "1.2"
_TERMINAL_OUTCOMES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})


def _canonical_json_bytes(value: object) -> bytes:
    """The one serialization covered by producer evidence digests."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _accepted_request_digest(*, repository_id: str, producer_id: str,
                             producer_type: str, producer_version: object,
                             prompt_digest: str, constraints: object,
                             correlation_id: object, mission_id: object,
                             engineering_action_id: object) -> str:
    """Bind the exact accepted request, excluding replay transport metadata."""
    return _sha256(_canonical_json_bytes({
        "repository_id": repository_id, "producer": {
            "id": producer_id, "type": producer_type, "version": producer_version,
        },
        "prompt_digest": prompt_digest, "constraints": constraints,
        "correlation_id": correlation_id, "mission_id": mission_id,
        "engineering_action_id": engineering_action_id,
    }))


def _forge_submission_receipt(
    connection: sqlite3.Connection, *, project_id: str, submission_id: str,
) -> dict[str, object] | None:
    """Return the immutable EP acceptance receipt for one Forge envelope.

    Historic Forge v1.0 envelopes intentionally have no synthetic receipt:
    their application and envelope versions were never supplied.  New v1.1
    envelopes receive this receipt atomically with CENTRAL acceptance.
    """
    row = connection.execute(
        """SELECT receipt_id,producer_contract_version,forge_provenance_contract_version,
                  forge_application_version,ep_application_version,
                  producer_readback_contract_version,accepted_request_digest,recorded_at
             FROM ep_forge_exchange_audit
            WHERE project_id=? AND submission_id=? AND event_kind='FORGE_SUBMISSION_ACCEPTED'""",
        (project_id, submission_id),
    ).fetchone()
    if row is None:
        return None
    instance = connection.execute(
        "SELECT value FROM engineering_metadata WHERE key='installation.instance_id'"
    ).fetchone()
    if instance is None:
        raise SubmissionError("FORGE_RECEIPT_UNAVAILABLE", 500)
    return {
        "contract_version": EP_SUBMISSION_RECEIPT_CONTRACT_VERSION,
        "id": str(row[0]),
        "event": "FORGE_SUBMISSION_ACCEPTED",
        "issued_at": str(row[7]),
        "submission_id": submission_id,
        "ep_instance_id": str(instance[0]),
        "ep_application_version": str(row[4]),
        "producer_contract_version": str(row[1]),
        "forge_provenance_contract_version": str(row[2]),
        "forge_application_version": str(row[3]),
        "producer_readback_contract_version": str(row[5]),
        "accepted_request_digest": str(row[6]),
    }


def _record_forge_submission_acceptance(
    connection: sqlite3.Connection, *, request: SubmissionRequest, submission_id: str, created_at: str,
) -> dict[str, object] | None:
    """Durably attest the versioned Forge→EP envelope without its prompt.

    This is a distinct immutable audit record, rather than a component log:
    component logs have operator-configured retention, while a producer
    receipt is part of the accountable inter-product exchange.
    """
    if request.transport != "HTTP" or request.producer_type != "FORGE":
        return None
    raw = (request.constraints or {}).get("forge_execution")
    if not isinstance(raw, Mapping) or raw.get("contract_version") != FORGE_PROVENANCE_CONTRACT_VERSION:
        return None
    request_digest = _accepted_request_digest(
        repository_id=request.repository_id, producer_id=request.producer_id,
        producer_type=request.producer_type, producer_version=request.producer_version,
        prompt_digest=hashlib.sha256(request.prompt.encode("utf-8")).hexdigest(),
        constraints=request.constraints or {}, correlation_id=request.correlation_id,
        mission_id=request.mission_id, engineering_action_id=request.engineering_action_id,
    )
    receipt_id = "ep-submission-receipt:" + submission_id
    connection.execute(
        """INSERT INTO ep_forge_exchange_audit(
               audit_id,submission_id,project_id,direction,event_kind,receipt_id,
               producer_contract_version,forge_provenance_contract_version,
               forge_application_version,ep_application_version,
               producer_readback_contract_version,accepted_request_digest,recorded_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "forge-exchange:" + submission_id, submission_id, request.project_id,
            "FORGE_TO_EP", "FORGE_SUBMISSION_ACCEPTED", receipt_id,
            str(raw["producer_contract_version"]), str(raw["contract_version"]),
            str(raw["forge_application_version"]), CURRENT_PLATFORM_VERSION,
            PRODUCER_READBACK_CONTRACT_VERSION, request_digest, created_at,
        ),
    )
    return _forge_submission_receipt(
        connection, project_id=request.project_id, submission_id=submission_id,
    )


def _read_constraints(value: object) -> dict[str, object] | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _terminal_artifact_id(run_id: str) -> str:
    return f"terminal-evidence:{run_id}"


def _findings_artifact_id(run_id: str) -> str:
    return f"assurance-findings:{run_id}"


def _current_assurance(checkpoint: object) -> tuple[str, list[dict[str, object]], list[dict[str, object]]]:
    """Project final assurance from one complete current review set.

    Earlier review records are deliberately not overwritten: they remain
    historical observations in the findings artifact.  Only the most recent
    complete quality/security pair bound to the checkpoint's exact candidate
    and policy can qualify terminal delivery.
    """
    profile = getattr(checkpoint, "assurance_profile", None)
    reviews = list(getattr(checkpoint, "assurance_reviews", ()))
    if not isinstance(profile, dict):
        return "NOT_RECORDED", [], reviews
    candidate, digest = profile.get("candidate_sha"), profile.get("digest")
    if not isinstance(candidate, str) or not isinstance(digest, str):
        return "UNRESOLVED", [], reviews
    current = [
        review for review in reviews
        if isinstance(review, dict)
        and review.get("candidate_sha") == candidate
        and review.get("profile_digest") == digest
    ]
    latest = {
        role: next((review for review in reversed(current) if review.get("reviewer") == role), None)
        for role in ("quality", "security")
    }
    if any(review is None or review.get("status") == "UNRESOLVED" for review in latest.values()):
        return "UNRESOLVED", current, reviews
    if any(review.get("status") != "PASS" for review in latest.values() if isinstance(review, dict)):
        return "FAIL", current, reviews
    findings = [finding for review in latest.values() if isinstance(review, dict) for finding in review.get("findings", []) if isinstance(finding, dict)]
    current_open = any(finding.get("blocking") and finding.get("disposition") == "OPEN" for finding in findings)
    resolved = {
        item.get("finding_id") for item in getattr(checkpoint, "assurance_resolutions", ())
        if isinstance(item, dict) and item.get("disposition") in {"RESOLVED", "REJECTED", "RISK_ACCEPTED"}
    }
    historical_open = any(
        finding.get("blocking") and finding.get("disposition") == "OPEN" and finding.get("id") not in resolved
        for review in reviews if isinstance(review, dict)
        for finding in review.get("findings", []) if isinstance(finding, dict)
    )
    return ("FAIL" if current_open or historical_open else "PASS"), current, reviews


def _write_immutable_artifact(target: Path, payload: bytes) -> None:
    """Create immutable evidence once; conflicting terminal rewrites fail closed."""
    if target.exists():
        try:
            if target.read_bytes() == payload:
                return
        except OSError:
            pass
        raise SubmissionError("TERMINAL_EVIDENCE_IMMUTABLE_CONFLICT", 500)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.chmod(0o600)
    temporary.replace(target)


def _repository_revision(state: object, outcome: str) -> tuple[str | None, bool]:
    """Return a run-bound delivery revision, never an ambient checkout HEAD."""
    if outcome != "COMPLETE":
        return None, True
    if getattr(state, "action_intent", None) == "VALIDATION_ONLY":
        return None, True
    revision = getattr(state, "finalization_merge_commit", None) or getattr(state, "implementation_merge_commit", None)
    evidence = getattr(state, "commit_evidence", ())
    if not isinstance(revision, str) or not __import__("re").fullmatch(r"[0-9a-f]{40}", revision):
        return None, False
    if not any(isinstance(item, dict) and item.get("commit_sha") == revision for item in evidence):
        return None, False
    return revision, True


def write_terminal_evidence(
    data_root: Path, *, repository_root: Path, run_id: str,
) -> str:
    """Persist one immutable, run-bound terminal evidence artifact.

    This is called only by the lifecycle finalizer.  It validates the durable
    checkpoint and its submission/dispatch binding before writing, so a reader
    can never manufacture evidence for an otherwise terminal-looking row.
    """
    from .agent_state import StateError, TransactionState
    from .storage import record_artifact

    database = central_database.path(data_root)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """SELECT d.submission_id,d.project_id,d.repository_id,d.state,
                      s.producer_id,s.producer_type,s.producer_version,s.prompt_digest,
                      s.constraints,s.correlation_id,s.mission_id,s.engineering_action_id,
                      t.payload,h.terminal_state
                 FROM ep_parity_lifecycle_dispatches d
                 JOIN ep_submissions s ON s.submission_id=d.submission_id
                 JOIN engineering_transactions t ON t.run_id=d.run_id
                 LEFT JOIN prompt_execution_history h ON h.run_id=d.run_id
                WHERE d.run_id=?""", (run_id,),
        ).fetchone()
    if row is None:
        raise SubmissionError("TERMINAL_EVIDENCE_BINDING_UNAVAILABLE", 500)
    try:
        checkpoint = TransactionState.from_dict(json.loads(str(row[12])))
    except (StateError, TypeError, ValueError, json.JSONDecodeError):
        raise SubmissionError("TERMINAL_CHECKPOINT_INVALID", 500) from None
    outcome = checkpoint.phase
    if checkpoint.run_id != run_id or not checkpoint.terminal or outcome not in _TERMINAL_OUTCOMES:
        raise SubmissionError("TERMINAL_CHECKPOINT_INVALID", 500)
    if str(row[3]) not in {"RUNNING", outcome} or row[13] != outcome:
        raise SubmissionError("TERMINAL_EVIDENCE_BINDING_UNAVAILABLE", 500)
    constraints = _read_constraints(row[8])
    if constraints is None:
        raise SubmissionError("SUBMISSION_PROVENANCE_INVALID", 500)
    request_digest = _accepted_request_digest(
        repository_id=str(row[2]), producer_id=str(row[4]), producer_type=str(row[5]),
        producer_version=row[6], prompt_digest=str(row[7]), constraints=constraints,
        correlation_id=row[9], mission_id=row[10], engineering_action_id=row[11],
    )
    revision, delivery_qualified = _repository_revision(checkpoint, outcome)
    artifact_id = _terminal_artifact_id(run_id)
    report_id = f"report:{run_id}"
    assurance_status, current_reviews, reviews = _current_assurance(checkpoint)
    findings = [finding for review in reviews for finding in review.get("findings", [])]
    findings_id = _findings_artifact_id(run_id) if checkpoint.assurance_profile is not None else None
    if findings_id is not None:
        findings_payload = {
            "artifact_type": "EP_ASSURANCE_FINDINGS", "contract_version": "1.0",
            "run_id": run_id, "project_id": str(row[1]), "repository_id": str(row[2]),
            "profile": checkpoint.assurance_profile, "reviews": reviews,
            "resolutions": list(checkpoint.assurance_resolutions),
        }
        findings_target = data_root / "artifacts" / "projects" / str(row[1]) / "runs" / run_id / "assurance-findings-v1.json"
        findings_bytes = _canonical_json_bytes(findings_payload)
        _write_immutable_artifact(findings_target, findings_bytes)
        record_artifact(repository_root, findings_target, artifact_id=findings_id, artifact_type="EP_ASSURANCE_FINDINGS",
                        content_type="application/json", created_at=_now(), run_id=run_id, submission_id=str(row[0]),
                        mission_id=str(row[10]) if row[10] is not None else None, producer_id=str(row[4]),
                        central_database=database, artifact_root=data_root / "artifacts")
    payload = {
        "artifact_type": "EP_TERMINAL_EVIDENCE", "contract_version": TERMINAL_EVIDENCE_CONTRACT_VERSION,
        "submission": {"id": str(row[0]), "project_id": str(row[1]), "repository_id": str(row[2]),
                       "accepted_request_digest": request_digest},
        "producer": {"id": str(row[4]), "type": str(row[5]), "version": row[6]},
        "correlation": {"correlation_id": row[9], "mission_id": row[10], "engineering_action_id": row[11]},
        "provenance": constraints.get("forge_execution"),
        "run": {"id": run_id, "outcome": outcome, "delivery_qualified": delivery_qualified},
        "repository": {"id": str(row[2]), "revision": revision,
                       "revision_required": checkpoint.action_intent != "VALIDATION_ONLY" and outcome == "COMPLETE"},
        "report": {"id": report_id, "terminal_state": outcome},
        "references": {
            "validation": list(checkpoint.validation_evidence), "quality": list(checkpoint.quality_evidence),
            "repair": list(checkpoint.repair_audit), "finalization": checkpoint.latest_repository_evidence,
        },
        "assurance": {
            "status": assurance_status,
            "profile": checkpoint.assurance_profile,
            "quality_review": next((review.get("status") for review in reversed(current_reviews) if review.get("reviewer") == "quality"), "NOT_RECORDED"),
            "security_review": next((review.get("status") for review in reversed(current_reviews) if review.get("reviewer") == "security"), "NOT_RECORDED"),
            "repair_rounds": {"used": checkpoint.repair_iterations, "maximum": 3},
            "findings": {"open_blocking": sum(1 for review in current_reviews for finding in review.get("findings", []) if finding.get("blocking") and finding.get("disposition") == "OPEN"), "open_non_blocking": sum(1 for review in current_reviews for finding in review.get("findings", []) if not finding.get("blocking") and finding.get("disposition") in {"OPEN", "NON_BLOCKING"}), "artifact": None if findings_id is None else {"id": findings_id, "digest_algorithm": "sha256", "digest": hashlib.sha256(findings_bytes).hexdigest()}},
        },
    }
    target = data_root / "artifacts" / "projects" / str(row[1]) / "runs" / run_id / "terminal-evidence-v1.json"
    _write_immutable_artifact(target, _canonical_json_bytes(payload))
    record_artifact(
        repository_root, target, artifact_id=artifact_id, artifact_type="EP_TERMINAL_EVIDENCE",
        content_type="application/json", created_at=_now(), run_id=run_id,
        submission_id=str(row[0]), mission_id=str(row[10]) if row[10] is not None else None,
        producer_id=str(row[4]), central_database=database, artifact_root=data_root / "artifacts",
    )
    return artifact_id


def producer_readback(
    connection: sqlite3.Connection, *, project_id: str, submission_id: str,
) -> dict[str, object] | None:
    """Project one canonical producer-visible submission/readback record.

    This is deliberately a read-only projection over existing CENTRAL rows.  It
    never consults a checkout, invokes a provider, or reconstructs identity
    from a prompt.  ``None`` covers both an unknown ID and an ID outside the
    authenticated project scope, so the HTTP adapter does not disclose another
    project's submission identities.
    """
    row = connection.execute(
        """SELECT s.repository_id,s.producer_id,s.producer_type,s.producer_version,
                  s.prompt_digest,s.constraints,s.correlation_id,s.mission_id,
                  s.engineering_action_id,s.state,s.admission,s.transport,s.created_at,s.disposition_revision,
                  d.run_id,d.state,d.operator_resolution,d.updated_at
             FROM ep_submissions AS s
             LEFT JOIN ep_parity_lifecycle_dispatches AS d
               ON d.submission_id=s.submission_id
            WHERE s.project_id=? AND s.submission_id=?""",
        (project_id, submission_id),
    ).fetchone()
    if row is None:
        return None
    (
        repository_id, producer_id, producer_type, producer_version, prompt_digest,
        raw_constraints, correlation_id, mission_id, engineering_action_id, submission_state,
        admission, transport, created_at, disposition_revision, run_id, dispatch_state,
        operator_resolution, updated_at,
    ) = row
    disposition_row = connection.execute(
        "SELECT o.operation_id,o.event_id,o.actor_reference,e.payload,o.recorded_at FROM ep_queue_disposition_operations o JOIN ep_submission_events e ON e.event_id=o.event_id WHERE o.project_id=? AND o.submission_id=? ORDER BY o.recorded_at DESC LIMIT 1",
        (project_id, submission_id),
    ).fetchone()
    disposition = {"state": str(submission_state), "terminal": str(submission_state) == "DECLINED", "execution_eligible": str(submission_state) == "QUEUED", "revision": int(disposition_revision), "operation_id": None, "event_reference": None, "reason": "NOT_RECORDED", "actor_reference": "NOT_RECORDED", "recorded_at": None, "resolution_submission_id": None, "retry_parent_run_id": None}
    if disposition_row is not None:
        try:
            reason = json.loads(str(disposition_row[3])).get("reason", "NOT_RECORDED")
        except json.JSONDecodeError:
            reason = "NOT_RECORDED"
        disposition.update({"operation_id": str(disposition_row[0]), "event_reference": "event:" + str(disposition_row[1]), "actor_reference": str(disposition_row[2]), "reason": reason, "recorded_at": str(disposition_row[4])})
    retry_resolution = connection.execute(
        """SELECT resolution_submission_id FROM ep_parity_lifecycle_dispatches
           WHERE project_id=? AND repository_id=? AND submission_id=?
             AND operator_resolution='RETRIED' AND resolution_submission_id IS NOT NULL""",
        (project_id, repository_id, submission_id),
    ).fetchone()
    if retry_resolution is not None:
        disposition["resolution_submission_id"] = str(retry_resolution[0])
    retry_parent = connection.execute(
        """SELECT run_id FROM ep_parity_lifecycle_dispatches
           WHERE project_id=? AND repository_id=? AND resolution_submission_id=?
             AND operator_resolution='RETRIED'""",
        (project_id, repository_id, submission_id),
    ).fetchone()
    if retry_parent is not None:
        disposition["retry_parent_run_id"] = str(retry_parent[0])
    constraints = _read_constraints(raw_constraints)
    if constraints is None:
        # Existing data is retained, but cannot be represented as qualified
        # provenance when its canonical constraints record is corrupt.
        constraints = {}
        provenance_status = "INVALID"
    else:
        provenance_status = "PERSISTED"
    request_digest = _accepted_request_digest(
        repository_id=str(repository_id), producer_id=str(producer_id), producer_type=str(producer_type),
        producer_version=producer_version, prompt_digest=str(prompt_digest), constraints=constraints,
        correlation_id=correlation_id, mission_id=mission_id, engineering_action_id=engineering_action_id,
    )
    result: dict[str, object] = {"outcome": "NOT_STARTED", "terminal": False, "delivery_qualified": False}
    evidence: dict[str, object] = {"status": "NOT_TERMINAL", "terminal_artifact": None,
                                    "repository": {"id": str(repository_id), "revision": None}}
    run: dict[str, object] | None = None
    if run_id is not None:
        transaction = connection.execute(
            "SELECT phase,payload,updated_at FROM engineering_transactions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        terminal_state: str | None = None
        checkpoint_valid = False
        if transaction is not None:
            try:
                from .agent_state import TransactionState
                checkpoint = TransactionState.from_dict(json.loads(str(transaction[1])))
                if checkpoint.run_id == run_id and checkpoint.terminal and checkpoint.phase in _TERMINAL_OUTCOMES:
                    terminal_state, checkpoint_valid = checkpoint.phase, True
            except (Exception,):  # corrupt/legacy checkpoint is evidence-incomplete, never terminal proof.
                checkpoint_valid = False
        state = terminal_state if checkpoint_valid else str(dispatch_state)
        terminal = checkpoint_valid and state in _TERMINAL_OUTCOMES
        run = {
            "id": str(run_id), "state": state, "terminal": terminal,
            "operator_resolution": str(operator_resolution), "updated_at": str(updated_at),
        }
        result = {"outcome": state, "terminal": terminal, "delivery_qualified": False}
        if not checkpoint_valid and str(dispatch_state) in _TERMINAL_OUTCOMES:
            evidence["status"] = "INCOMPLETE"
        elif terminal:
            artifact = connection.execute(
                """SELECT artifact_id,digest_algorithm,digest,content_type,integrity_status,storage_location
                     FROM execution_artifact_records WHERE artifact_id=? AND run_id=?""",
                (_terminal_artifact_id(str(run_id)), run_id),
            ).fetchone()
            if artifact is None:
                evidence["status"] = "MISSING"
            elif artifact[1] != "sha256" or not isinstance(artifact[2], str) or len(str(artifact[2])) != 64:
                evidence["status"] = "CORRUPT"
            else:
                try:
                    database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
                    artifact_root = database_path.parent / "artifacts"
                    payload_path = (artifact_root / str(artifact[5])).resolve()
                    payload_path.relative_to(artifact_root.resolve())
                    verified = hashlib.sha256(payload_path.read_bytes()).hexdigest() == str(artifact[2])
                except (OSError, ValueError, sqlite3.Error):
                    verified = False
                if not verified:
                    evidence["status"] = "CORRUPT"
                else:
                    evidence["status"] = "AVAILABLE"
                    evidence["terminal_artifact"] = {
                        "id": str(artifact[0]), "digest_algorithm": "sha256",
                        "digest": "sha256:" + str(artifact[2]), "content_type": str(artifact[3]),
                    }
                    try:
                        document = json.loads((artifact_root / str(artifact[5])).read_text(encoding="utf-8"))
                    except (OSError, ValueError, json.JSONDecodeError):
                        evidence["status"] = "CORRUPT"
                    else:
                        if (document.get("run", {}).get("id") != run_id or
                                document.get("submission", {}).get("id") != submission_id or
                                document.get("submission", {}).get("project_id") != project_id or
                                document.get("submission", {}).get("accepted_request_digest") != request_digest):
                            evidence["status"] = "CORRUPT"
                        else:
                            repository = document.get("repository")
                            if isinstance(repository, dict):
                                evidence["repository"] = {"id": repository.get("id"), "revision": repository.get("revision")}
                            qualified = bool(document.get("run", {}).get("delivery_qualified"))
                            result["delivery_qualified"] = qualified and evidence["status"] == "AVAILABLE"
    return {
        "contract_version": PRODUCER_READBACK_CONTRACT_VERSION,
        "submission": {
            "id": submission_id, "project_id": project_id,
            "repository_id": str(repository_id), "state": str(submission_state),
            "admission": str(admission), "transport": str(transport),
            "created_at": str(created_at), "accepted_request_digest": request_digest,
        },
        "producer": {
            "id": str(producer_id), "type": str(producer_type),
            "version": producer_version,
        },
        "correlation": {
            "correlation_id": correlation_id, "mission_id": mission_id,
            "engineering_action_id": engineering_action_id,
        },
        "provenance": {"status": provenance_status, "forge_execution": constraints.get("forge_execution")},
        "disposition": disposition, "run": run, "result": result, "evidence": evidence,
    }


def producer_evidence_artifact(
    connection: sqlite3.Connection, *, project_id: str, artifact_id: str,
) -> bytes | None:
    """Return a verified, project-scoped terminal or assurance artifact."""
    row = connection.execute(
        """SELECT a.digest_algorithm,a.digest,a.storage_location
             FROM execution_artifact_records a
             JOIN ep_parity_lifecycle_dispatches d ON d.run_id=a.run_id
            WHERE d.project_id=? AND a.artifact_id=? AND a.artifact_type IN ('EP_TERMINAL_EVIDENCE','EP_ASSURANCE_FINDINGS')""",
        (project_id, artifact_id),
    ).fetchone()
    if row is None or row[0] != "sha256" or not isinstance(row[1], str):
        return None
    try:
        database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        artifact_root = (database_path.parent / "artifacts").resolve()
        target = (artifact_root / str(row[2])).resolve()
        target.relative_to(artifact_root)
        payload = target.read_bytes()
    except (OSError, ValueError, sqlite3.Error):
        return None
    return payload if hashlib.sha256(payload).hexdigest() == row[1] else None


def submit_legacy_file(connection: sqlite3.Connection, path: object) -> SubmissionResult:
    """Compatibility adapter: a bounded JSON envelope with explicit project scope."""
    from pathlib import Path
    candidate = Path(path)
    raw = candidate.read_bytes()
    if len(raw) > 131072 or b"\0" in raw:
        raise SubmissionError("MALFORMED_LEGACY_FILE")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SubmissionError("MALFORMED_LEGACY_FILE") from None
    if not isinstance(envelope, Mapping) or set(envelope) != {"project_id", "submission"}:
        raise SubmissionError("MALFORMED_LEGACY_FILE")
    return submit(connection, request_from_mapping(str(envelope["project_id"]), envelope["submission"], transport="LEGACY_FILE"))
