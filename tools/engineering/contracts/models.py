"""JSON-compatible, versioned public contract value objects.

These objects describe evidence and policy decisions only.  They contain no
filesystem locations, executable commands, provider output, or prompt text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


CONTRACT_MAJOR_VERSION = 1
CONTRACT_VERSION = "1.0"


class ContractVersionError(ValueError):
    """Raised when an incompatible external contract version is requested."""


def require_compatible_version(version: object) -> None:
    """Fail closed for unknown major contract versions."""
    if not isinstance(version, str) or version.split(".", 1)[0] != str(CONTRACT_MAJOR_VERSION):
        raise ContractVersionError("Engineering contract version is incompatible.")


@dataclass(frozen=True)
class _ContractValue:
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        require_compatible_version(self.contract_version)
        return asdict(self)


@dataclass(frozen=True)
class EvidenceReference(_ContractValue):
    """A bounded pointer to canonical evidence, never the evidence payload."""

    id: str = ""
    source_type: str = "UNAVAILABLE"
    authority: str = "UNAVAILABLE"
    observed_at: str = "UNAVAILABLE"
    freshness: str = "UNKNOWN"
    subject: str = "UNAVAILABLE"
    snapshot_identity: str = "UNAVAILABLE"
    safe_summary_code: str = "UNAVAILABLE"


@dataclass(frozen=True)
class AllowedAction(_ContractValue):
    """A capability descriptor; it intentionally has no executable payload."""

    action_id: str = ""
    action_namespace: str = ""
    action_version: str = CONTRACT_VERSION
    run_id: str = ""
    target_identity: str = "RUN"
    classification: str = "READ_ONLY"
    mutation_level: str = "NONE"
    authority_required: str = "EP_POLICY"
    confirmation_required: bool = False
    allowed: bool = False
    reason_code: str = "UNAVAILABLE"
    evidence_version: str = "UNAVAILABLE"
    expires_at: str = "BOUNDARY_SENSITIVE"
    scope: str = "RUN_SCOPED"
    expected_effect_code: str = "READ_CANONICAL_EVIDENCE"
    blocked_reason_code: str | None = None


@dataclass(frozen=True)
class ActionPolicyDecision(_ContractValue):
    action_id: str = ""
    run_id: str = ""
    decision: str = "UNAVAILABLE"
    reason_code: str = "UNAVAILABLE"
    policy_version: str = CONTRACT_VERSION
    evaluated_at: str = "UNAVAILABLE"
    evidence_version: str = "UNAVAILABLE"
    authority: str = "EP_POLICY"
    confirmation_required: bool = False


@dataclass(frozen=True)
class ActionAuditRecord(_ContractValue):
    """Append-only audit shape for a future governed action gateway.

    No persistence or execution behaviour is introduced by this declaration.
    """

    audit_id: str = ""
    run_id: str = ""
    action_id: str = ""
    action_version: str = CONTRACT_VERSION
    policy_version: str = CONTRACT_VERSION
    evidence_version: str = "UNAVAILABLE"
    actor_type: str = "UNAVAILABLE"
    authority: str = "UNAVAILABLE"
    requested_at: str = "UNAVAILABLE"
    confirmed_at: str | None = None
    executed_at: str | None = None
    result: str = "UNAVAILABLE"
    resulting_state_code: str = "UNAVAILABLE"
    validation_evidence_reference: str | None = None
