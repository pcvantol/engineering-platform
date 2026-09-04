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
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Mapping


MAX_PROMPT_BYTES = 65536
MAX_FIELD_LENGTH = 128
MAX_CONSTRAINT_BYTES = 8192
VALID_TRANSPORTS = frozenset({"HTTP", "CLI", "FILE_INBOX", "LEGACY_FILE"})
VALID_EXECUTION_MODES = frozenset({"MANAGED", "GENESIS"})

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


class SubmissionError(ValueError):
    """A stable, safe submission rejection."""
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code, self.status = code, status


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

    def to_dict(self) -> dict[str, object]:
        return {"submission_id": self.submission_id, "project_id": self.project_id,
                "repository_id": self.repository_id, "state": self.state,
                "created_at": self.created_at, "admission": self.admission,
                "transport": self.transport, "duplicate": self.duplicate,
                "lifecycle": _lifecycle_payload(
                    transport=self.transport,
                    producer_id=self.producer_id,
                )}


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
    return SubmissionRequest(
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


def submit(connection: sqlite3.Connection, request: SubmissionRequest) -> SubmissionResult:
    """Persist and admit one request; no provider or Agent is selected here."""
    _transport(request.transport)
    _validate_execution_mode(request)
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
            return SubmissionResult(
                str(duplicate[0]), request.project_id, str(duplicate[4]), str(duplicate[2]),
                str(duplicate[1]), str(duplicate[3]), str(duplicate[14]), str(duplicate[5]), True,
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
    lifecycle(connection, submission_id)
    return SubmissionResult(
        submission_id, request.project_id, request.repository_id, state, created_at,
        admission, request.transport, request.producer_id,
    )


def register_consumer(connection: sqlite3.Connection, *, consumer_id: str, project_id: str) -> None:
    """Explicitly grant a consumer one project scope in CENTRAL."""
    consumer_id = _token(consumer_id, "consumer_id") or ""
    project_id = _token(project_id, "project_id") or ""
    if connection.execute("SELECT 1 FROM ep_project_registrations WHERE project_id=? AND status='ACTIVE'", (project_id,)).fetchone() is None:
        raise SubmissionError("UNKNOWN_PROJECT", 404)
    now = _now()
    connection.execute("""INSERT INTO local_api_consumer_registrations(consumer_id,project_id,status,created_at,updated_at,audit_metadata)
        VALUES(?,?,'ACTIVE',?,?,?) ON CONFLICT(consumer_id,project_id) DO UPDATE SET status='ACTIVE',updated_at=excluded.updated_at""", (consumer_id, project_id, now, now, json.dumps({"action": "SUBMISSION_CONSUMER_REGISTER"}, sort_keys=True)))


def issue_consumer_credential(connection: sqlite3.Connection, *, consumer_id: str, project_id: str) -> dict[str, str]:
    """Issue a scoped bearer token once; only its verifier is retained."""
    register_consumer(connection, consumer_id=consumer_id, project_id=project_id)
    from .local_api_credentials import fingerprint, verifier
    token, credential_id, now = secrets.token_urlsafe(32), "production-" + secrets.token_hex(16), _now()
    connection.execute("INSERT INTO local_api_credentials(credential_id,consumer_id,project_id,verifier,fingerprint,issued_at) VALUES(?,?,?,?,?,?)", (credential_id, consumer_id, project_id, verifier(token), fingerprint(token), now))
    return {"credential_id": credential_id, "consumer_id": consumer_id, "project_id": project_id, "credential": token}


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
