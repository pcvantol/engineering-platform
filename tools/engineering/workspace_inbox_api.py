"""Bounded trusted-producer API for publishing one canonical Inbox envelope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import os
import uuid

from .platform_api import execution_host_configuration
from .producer import ProducerSubmissionError, parse_producer_submission
from .storage import EngineeringStorageError, record_submission


class WorkspaceInboxSubmissionError(ValueError):
    """Raised when a Forge/Workspace submission cannot enter the Inbox safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class WorkspaceInboxReceipt:
    submission_id: str
    filename: str
    inbox: Path
    received_at: str


def publish(root: Path, envelope: str) -> WorkspaceInboxReceipt:
    """Validate and atomically publish a trusted producer envelope into the Inbox.

    This is deliberately a local API, not a new execution path: the normal
    Inbox watcher remains the sole claimant and lifecycle owner.
    """
    try:
        submission = parse_producer_submission(envelope)
    except ProducerSubmissionError as error:
        raise WorkspaceInboxSubmissionError(
            "invalid_producer_envelope", "Producer submission does not contain a valid producer envelope."
        ) from error
    if (
        submission.is_legacy
        or submission.producer.producer_type not in {"FORGE", "HUMAN"}
        or not submission.submission_id
    ):
        raise WorkspaceInboxSubmissionError(
            "producer_envelope_required", "This API accepts only a complete trusted producer envelope."
        )
    transport = execution_host_configuration(root).resolve_runtime_prompt_transport()
    inbox = transport.inbox
    if not inbox.is_dir() or not os.access(inbox, os.W_OK):
        raise WorkspaceInboxSubmissionError(
            "inbox_unavailable", "The configured Engineering Inbox is not writable."
        )
    received_at = datetime.now(timezone.utc).isoformat()
    try:
        record_submission(
            root,
            submission_id=submission.submission_id,
            producer_id=submission.producer.producer_id,
            producer_type=submission.producer.producer_type,
            producer_version=submission.producer.producer_version,
            contract_version=submission.contract_version,
            prompt_content=submission.prompt,
            prompt_metadata=dict(submission.envelope.get("prompt", {}).get("metadata", {})),
            target_identity={"transport": transport.provider, "inbox": str(inbox)},
            original_envelope=submission.envelope,
            correlation_id=submission.producer.correlation_id,
            mission_id=submission.producer.mission_id,
            engineering_action_id=submission.producer.engineering_action_id,
            execution_context=submission.execution_context,
            forge_governance_handoff=submission.forge_governance_handoff,
            received_at=received_at,
        )
    except EngineeringStorageError as error:
        raise WorkspaceInboxSubmissionError(
            "submission_audit_unavailable", "Inbox submission audit evidence could not be stored safely."
        ) from error
    filename = f"producer-{submission.submission_id}-{uuid.uuid4().hex[:12]}.json"
    target = inbox / filename
    partial = inbox / f".{filename}.partial"
    try:
        partial.write_text(envelope, encoding="utf-8")
        partial.chmod(0o600)
        partial.replace(target)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise WorkspaceInboxSubmissionError(
            "inbox_publication_failed", "Forge submission could not be published atomically to the Inbox."
        ) from error
    return WorkspaceInboxReceipt(submission.submission_id, filename, inbox, received_at)
