"""Producer Contract parsing for producer-neutral Execution Host metadata."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from .recommendation_handoff import ForgeGovernanceHandoffError, validate_forge_governance_handoff


_FIELD_LIMIT = 160
_PRODUCER_TYPES = frozenset({"HUMAN", "FORGE", "EXTERNAL", "UNKNOWN"})
_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$")
ENVELOPE_CONTRACT_NAME = "djconnect.producer_submission"
ENVELOPE_CONTRACT_VERSION = "1.0"


@dataclass(frozen=True)
class ProducerMetadata:
    """Immutable provenance supplied by a Producer, never execution input."""

    producer_id: str = "legacy"
    producer_type: str = "HUMAN"
    producer_version: str | None = None
    correlation_id: str | None = None
    mission_id: str | None = None
    engineering_action_id: str | None = None
    execution_constraint_version: str | None = None


class ProducerSubmissionError(ValueError):
    """Raised when a JSON Producer Submission Envelope is not safe to claim."""


@dataclass(frozen=True)
class ProducerSubmission:
    """A validated, producer-owned submission with no derived runtime semantics."""

    prompt: str
    producer: ProducerMetadata
    submission_id: str | None
    contract_version: str | None
    execution_context: dict[str, object] | None
    forge_governance_handoff: dict[str, object] | None
    envelope: dict[str, object]
    is_legacy: bool


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProducerSubmissionError(f"Producer Submission Envelope {label} must be an object.")
    return value


def _required_token(value: object, label: str) -> str:
    normalized = _value(value)
    if normalized is None:
        raise ProducerSubmissionError(f"Producer Submission Envelope {label} is invalid.")
    return normalized


def _optional_token(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_token(value, label)


def _optional_object(value: object, label: str) -> None:
    if value is not None and not isinstance(value, dict):
        raise ProducerSubmissionError(f"Producer Submission Envelope {label} must be an object when supplied.")


def parse_producer_submission(content: str) -> ProducerSubmission:
    """Validate one atomic Producer Submission Envelope or map a legacy prompt.

    JSON-looking submissions are always envelopes and fail closed.  Plain text
    remains the canonical Human Producer compatibility path.  This function
    never interprets prompt text or derives Execution Context semantics.
    """
    if not isinstance(content, str) or not content.strip():
        raise ProducerSubmissionError("Producer submission must contain a prompt.")
    if not content.lstrip().startswith("{"):
        return ProducerSubmission(
            prompt=content,
            producer=parse_producer_metadata(content),
            submission_id=None,
            contract_version=None,
            execution_context=None,
            forge_governance_handoff=None,
            envelope={"kind": "legacy_prompt", "prompt": content},
            is_legacy=True,
        )
    try:
        envelope = json.loads(content)
    except json.JSONDecodeError as error:
        raise ProducerSubmissionError("Producer Submission Envelope is not valid JSON.") from error
    envelope = _object(envelope, "root")
    contract = _object(envelope.get("contract"), "contract")
    if contract.get("name") != ENVELOPE_CONTRACT_NAME:
        raise ProducerSubmissionError("Producer Submission Envelope contract name is unsupported.")
    contract_version = contract.get("version")
    if contract_version != ENVELOPE_CONTRACT_VERSION:
        raise ProducerSubmissionError("Producer Submission Envelope contract version is unsupported.")
    submission = _object(envelope.get("submission"), "submission")
    submission_id = _required_token(submission.get("id"), "submission.id")
    _optional_object(submission.get("metadata"), "submission.metadata")
    if submission.get("submitted_at") is not None and not isinstance(submission.get("submitted_at"), str):
        raise ProducerSubmissionError("Producer Submission Envelope submission.submitted_at must be a string.")
    producer_payload = _object(envelope.get("producer"), "producer")
    producer_id = _required_token(producer_payload.get("id"), "producer.id")
    producer_type = _required_token(producer_payload.get("type"), "producer.type").upper()
    for name in ("version", "correlation_id", "mission_id", "engineering_action_id", "execution_constraint_version"):
        _optional_token(producer_payload.get(name), f"producer.{name}")
    _optional_object(producer_payload.get("metadata"), "producer.metadata")
    prompt_payload = _object(envelope.get("prompt"), "prompt")
    prompt = prompt_payload.get("text")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProducerSubmissionError("Producer Submission Envelope prompt.text must be a non-empty string.")
    _optional_object(prompt_payload.get("metadata"), "prompt.metadata")
    context = envelope.get("execution_context")
    if context is not None:
        context = _object(context, "execution_context")
        _required_token(context.get("context_version"), "execution_context.context_version")
        intent = context.get("action_intent")
        if intent is not None and intent not in {"MUTATING_DELIVERY", "VALIDATION_ONLY"}:
            raise ProducerSubmissionError("Producer Submission Envelope execution_context.action_intent is invalid.")
        profile = context.get("validation_profile")
        if profile is not None:
            profile = _object(profile, "execution_context.validation_profile")
            _required_token(profile.get("tier"), "execution_context.validation_profile.tier")
            _required_token(profile.get("version"), "execution_context.validation_profile.version")
            controls = profile.get("required_controls")
            if not isinstance(controls, list) or not controls or any(_optional_token(item, "execution_context.validation_profile.required_controls") is None for item in controls):
                raise ProducerSubmissionError("Producer Submission Envelope execution_context.validation_profile.required_controls is invalid.")
    handoff = envelope.get("forge_governance_handoff")
    if handoff is not None:
        try:
            handoff = validate_forge_governance_handoff(handoff)
        except ForgeGovernanceHandoffError as error:
            raise ProducerSubmissionError(str(error)) from error
    return ProducerSubmission(
        prompt=prompt,
        producer=ProducerMetadata(
            producer_id=producer_id,
            producer_type=producer_type,
            producer_version=_optional_token(producer_payload.get("version"), "producer.version"),
            correlation_id=_optional_token(producer_payload.get("correlation_id"), "producer.correlation_id"),
            mission_id=_optional_token(producer_payload.get("mission_id"), "producer.mission_id"),
            engineering_action_id=_optional_token(producer_payload.get("engineering_action_id"), "producer.engineering_action_id"),
            execution_constraint_version=_optional_token(producer_payload.get("execution_constraint_version"), "producer.execution_constraint_version"),
        ),
        submission_id=submission_id,
        contract_version=contract_version,
        execution_context=context,
        forge_governance_handoff=handoff,
        envelope=envelope,
        is_legacy=False,
    )


def _value(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    normalized = raw.strip()
    return normalized if _FIELD_PATTERN.fullmatch(normalized) else None


def _field(prompt: str, label: str) -> str | None:
    match = re.search(rf"(?mi)^\s*{re.escape(label)}\s*:\s*([^\r\n]+?)\s*$", prompt)
    return _value(match.group(1)) if match else None


def parse_producer_metadata(prompt: str) -> ProducerMetadata:
    """Consume only declared Producer Contract metadata with legacy defaults.

    The returned value is provenance.  No caller may use it for admission,
    scheduling, lifecycle, reviewer, or execution decisions.
    """
    producer_id = _field(prompt, "Producer ID") or "legacy"
    raw_type = _field(prompt, "Producer Type")
    producer_type = raw_type.upper() if raw_type else "HUMAN"
    # The contract's known values are explicit, while a valid future producer
    # token remains observable without forcing an Engineering Platform release.
    if not _FIELD_PATTERN.fullmatch(producer_type):
        producer_type = "UNKNOWN"
    return ProducerMetadata(
        producer_id=producer_id,
        producer_type=producer_type if raw_type else "HUMAN",
        producer_version=_field(prompt, "Producer Version"),
        correlation_id=_field(prompt, "Producer Correlation ID") or _field(prompt, "Correlation ID"),
        mission_id=_field(prompt, "Mission ID"),
        engineering_action_id=_field(prompt, "Engineering Action ID"),
        execution_constraint_version=_field(prompt, "Execution Constraint Version"),
    )
