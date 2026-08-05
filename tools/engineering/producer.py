"""Producer Contract parsing for producer-neutral Execution Host metadata."""

from __future__ import annotations

from dataclasses import dataclass
import re


_FIELD_LIMIT = 160
_PRODUCER_TYPES = frozenset({"HUMAN", "FORGE", "EXTERNAL", "UNKNOWN"})
_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$")


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
