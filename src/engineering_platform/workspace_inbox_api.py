"""Bounded trusted-producer API for publishing one canonical Inbox envelope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import os
import sys
import uuid

from .platform_api import execution_host_configuration
from .producer import (
    ENVELOPE_CONTRACT_NAME,
    ENVELOPE_CONTRACT_VERSION,
    ProducerSubmissionError,
    _value,
    parse_producer_submission,
)
from .storage import EngineeringStorageError, record_submission
from .validation_profile import ValidationProfileResolutionError, producer_profile_payload


class WorkspaceInboxSubmissionError(ValueError):
    """Raised when a Forge/Workspace submission cannot enter the Inbox safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


HUMAN_ACTION_INTENTS = frozenset({"MUTATING_DELIVERY", "VALIDATION_ONLY"})
HUMAN_PRODUCER_ID_PREFIX = "human:"
HUMAN_EXECUTION_CONTEXT_VERSION = "1.0"
HUMAN_TITLE_LIMIT = 240


@dataclass(frozen=True)
class WorkspaceInboxReceipt:
    submission_id: str
    filename: str
    inbox: Path
    received_at: str


def canonical_human_producer_id(identity: str) -> str:
    """Return the canonical non-legacy ``human:<identity>`` producer ID."""
    normalized = _value(identity)
    if normalized is None or normalized == "legacy":
        raise WorkspaceInboxSubmissionError("invalid_human_producer", "Human producer identity is invalid.")
    producer_id = normalized if normalized.startswith(HUMAN_PRODUCER_ID_PREFIX) else f"{HUMAN_PRODUCER_ID_PREFIX}{normalized}"
    if _value(producer_id) is None or producer_id == "legacy":
        raise WorkspaceInboxSubmissionError("invalid_human_producer", "Human producer identity is invalid.")
    return producer_id


def canonical_human_title(title: object) -> str:
    """Return one bounded, single-line operator-visible submission title."""
    if not isinstance(title, str):
        raise WorkspaceInboxSubmissionError("invalid_human_title", "Human submission title is required.")
    normalized = title.strip()
    if not normalized or "\n" in normalized or len(normalized) > HUMAN_TITLE_LIMIT:
        raise WorkspaceInboxSubmissionError("invalid_human_title", "Human submission title is invalid.")
    return normalized


def build_human_envelope(
    *, prompt: str, title: str, producer_identity: str, action_intent: str, validation_profile: str | None = None,
    submission_id: str | None = None,
) -> dict[str, object]:
    """Build one explicit Managed HUMAN envelope using the existing v1 contract."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise WorkspaceInboxSubmissionError("invalid_human_prompt", "Human submission prompt is required.")
    normalized_title = canonical_human_title(title)
    if action_intent not in HUMAN_ACTION_INTENTS:
        raise WorkspaceInboxSubmissionError("invalid_human_action_intent", "Human submission action intent is invalid.")
    identifier = submission_id or f"human-{uuid.uuid4().hex}"
    if _value(identifier) is None:
        raise WorkspaceInboxSubmissionError("invalid_human_submission", "Human submission identity is invalid.")
    stripped = prompt.lstrip()
    if stripped.casefold().startswith("execution mode:"):
        if stripped.splitlines()[0].strip().casefold() != "execution mode: managed":
            raise WorkspaceInboxSubmissionError("invalid_human_execution_mode", "Structured Human submissions require Execution Mode: Managed.")
        objective = prompt
    else:
        # Execution mode remains the existing prompt-level Execution Host
        # contract. It is explicit here, never inferred from objective prose.
        objective = f"Execution Mode: Managed\n\n{prompt}"
    if action_intent == "VALIDATION_ONLY" and validation_profile is None:
        raise WorkspaceInboxSubmissionError(
            "validation_profile_required", "Human VALIDATION_ONLY submissions require a validation profile."
        )
    try:
        profile_payload = (
            producer_profile_payload(validation_profile)
            if validation_profile is not None
            else None
        )
    except ValidationProfileResolutionError as error:
        raise WorkspaceInboxSubmissionError(
            "invalid_validation_profile", "Human submission validation profile is invalid."
        ) from error
    envelope: dict[str, object] = {
        "contract": {"name": ENVELOPE_CONTRACT_NAME, "version": ENVELOPE_CONTRACT_VERSION},
        "submission": {"id": identifier},
        "producer": {"id": canonical_human_producer_id(producer_identity), "type": "HUMAN"},
        "prompt": {"text": objective, "metadata": {"title": normalized_title}},
        "execution_context": {
            "context_version": HUMAN_EXECUTION_CONTEXT_VERSION,
            "action_intent": action_intent,
            **({"validation_profile": profile_payload} if profile_payload is not None else {}),
        },
    }
    try:
        parsed = parse_producer_submission(json.dumps(envelope, sort_keys=True))
    except ProducerSubmissionError as error:
        raise WorkspaceInboxSubmissionError("invalid_human_envelope", "Human submission envelope is invalid.") from error
    if parsed.producer.producer_type != "HUMAN" or parsed.producer.producer_id == "legacy":
        raise WorkspaceInboxSubmissionError("invalid_human_producer", "Human submission producer provenance is invalid.")
    return envelope


def publish(root: Path, envelope: str) -> WorkspaceInboxReceipt:
    """Validate and atomically publish a trusted producer envelope into the Inbox.

    This is deliberately a local API, not a new execution path: the normal
    Inbox watcher remains the sole claimant and lifecycle owner.
    """
    submission, transport = _submission_and_transport(root, envelope)
    inbox = transport.inbox
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
    # A submission ID owns one immutable physical envelope. This makes a retry
    # after persistence or source-archive failure idempotent.
    filename = f"producer-{submission.submission_id}.json"
    target = inbox / filename
    partial = inbox / f".{filename}.partial"
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError as error:
            raise WorkspaceInboxSubmissionError(
                "inbox_publication_failed", "Forge submission could not be read safely from the Inbox."
            ) from error
        if existing != envelope:
            raise WorkspaceInboxSubmissionError(
                "submission_id_conflict", "Submission identity already belongs to a different envelope."
            )
        return WorkspaceInboxReceipt(submission.submission_id, filename, inbox, received_at)
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


def _submission_and_transport(root: Path, envelope: str) -> tuple[object, object]:
    """Validate an envelope and its configured Inbox without publishing it."""
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
    return submission, transport


def preview(root: Path, envelope: str) -> dict[str, object]:
    """Return a validated submission preview without storage or Inbox mutation."""
    submission, transport = _submission_and_transport(root, envelope)
    inbox = transport.inbox
    prompt = submission.envelope.get("prompt")
    metadata = prompt.get("metadata") if isinstance(prompt, dict) else None
    return {
        "dry_run": True,
        "submission_id": submission.submission_id,
        "inbox": str(inbox),
        "producer": {"id": submission.producer.producer_id, "type": submission.producer.producer_type},
        "execution_context": submission.execution_context,
        "prompt_metadata": metadata if isinstance(metadata, dict) else {},
        "envelope": submission.envelope,
    }


def submit_human(
    root: Path,
    *, prompt: str, title: str, producer_identity: str, action_intent: str, validation_profile: str | None = None,
    submission_id: str | None = None,
) -> WorkspaceInboxReceipt:
    """Persist then publish the exact structured Human envelope through ``publish``."""
    envelope = build_human_envelope(
        prompt=prompt,
        title=title,
        producer_identity=producer_identity,
        action_intent=action_intent,
        validation_profile=validation_profile,
        submission_id=submission_id,
    )
    return publish(root, json.dumps(envelope, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    """Offer the bounded operator-facing structured HUMAN ingress."""
    parser = argparse.ArgumentParser(description="Submit a structured HUMAN Engineering Inbox envelope.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--producer-id", required=True)
    parser.add_argument("--action-intent", required=True, choices=sorted(HUMAN_ACTION_INTENTS))
    parser.add_argument("--validation-profile", metavar="TIER")
    parser.add_argument("--submission-id")
    parser.add_argument("--dry-run", action="store_true", help="Validate and preview without storage or Inbox publication.")
    arguments = parser.parse_args(argv)
    try:
        if arguments.dry_run:
            envelope = build_human_envelope(
                prompt=arguments.prompt_file.read_text(encoding="utf-8"),
                title=arguments.title,
                producer_identity=arguments.producer_id,
                action_intent=arguments.action_intent,
                validation_profile=arguments.validation_profile,
                submission_id=arguments.submission_id,
            )
            print(json.dumps(preview(arguments.root, json.dumps(envelope, sort_keys=True)), sort_keys=True))
            return 0
        receipt = submit_human(
            arguments.root,
            prompt=arguments.prompt_file.read_text(encoding="utf-8"),
            title=arguments.title,
            producer_identity=arguments.producer_id,
            action_intent=arguments.action_intent,
            validation_profile=arguments.validation_profile,
            submission_id=arguments.submission_id,
        )
    except (OSError, WorkspaceInboxSubmissionError) as error:
        print(f"Human submission refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({
        "submission_id": receipt.submission_id, "filename": receipt.filename,
        "inbox": str(receipt.inbox), "received_at": receipt.received_at,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
