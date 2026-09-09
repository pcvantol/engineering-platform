"""OI-4c admission of one already-prepared EP update candidate.

This is deliberately not an installer, CLI, service controller, or activation
engine.  It proves the narrow boundary immediately before a future EP-owned
executor may use an OI-4b candidate.  The future executor must retain exactly
one selected operational runtime or remove this operation-owned candidate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Mapping

from . import operational_installation_record
from .installation_update_operation import (
    InstallationUpdateOperationError,
    InstallationUpdateSession,
    status,
)
from .installation_update_plan import InstallationUpdatePlan
from .installation_update_preparation import PreparedUpdateCandidate
from .operational_installation_lock import OperationalInstallationLockError


class InstallationUpdateAdmissionError(ValueError):
    """The exact prepared candidate cannot enter EP pre-cleanup execution."""


@dataclass(frozen=True)
class ExecutionAdmission:
    """Durable, token-free proof for exactly one non-operational candidate."""

    schema_version: int
    operation_id: str
    plan_digest: str
    installation_id: str
    registered_installation: Mapping[str, object]
    prepared_candidate: Mapping[str, object]

    def payload(self) -> dict[str, object]:
        return asdict(self)


def _record_snapshot(record: Mapping[str, object]) -> dict[str, object]:
    """Bind all current registered provenance, including the service launcher."""
    required = ("installation_id", "version", "artifact_digest", "source_revision", "interpreter", "roles")
    if any(key not in record for key in required):
        raise InstallationUpdateAdmissionError("registered installation provenance is incomplete")
    return {
        "installation_id": record["installation_id"],
        "version": record["version"],
        "artifact_digest": record["artifact_digest"],
        "source_revision": record["source_revision"],
        "interpreter": record["interpreter"],
        "roles": record["roles"],
        "record_digest": "sha256:" + hashlib.sha256(
            json.dumps(dict(record), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _same_launcher(left: str | Path, right: str | Path) -> bool:
    first, second = Path(left).expanduser(), Path(right).expanduser()
    return first.is_absolute() and second.is_absolute() and first.absolute() == second.absolute()


def _verify_record(plan: InstallationUpdatePlan, candidate: PreparedUpdateCandidate) -> dict[str, object]:
    try:
        record = operational_installation_record.load(Path(plan.data_root))
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateAdmissionError("registered operational installation is unavailable") from error
    if (record["installation_id"] != plan.installation_id
            or record["version"] != plan.current_version
            or record["artifact_digest"] != plan.current_digest):
        raise InstallationUpdateAdmissionError("registered installation changed before execution admission")
    # The candidate is deliberately only operation-owned until an explicit,
    # future activation selects it.  A record that already points at it is a
    # second-runtime/cutover attempt, not a pre-cleanup admission.
    if _same_launcher(str(record["interpreter"]), candidate.interpreter):
        raise InstallationUpdateAdmissionError("prepared candidate is already selected as the operational runtime")
    return _record_snapshot(record)


def _evidence(plan: InstallationUpdatePlan, candidate: PreparedUpdateCandidate,
              record: Mapping[str, object]) -> ExecutionAdmission:
    observed = status(Path(plan.data_root), plan.operation_id)
    if observed["state"] != "PREPARED":
        raise InstallationUpdateAdmissionError("execution admission requires the prepared pre-cleanup state")
    if Path(plan.artifact).expanduser().absolute() != Path(candidate.staged_artifact).expanduser().absolute():
        raise InstallationUpdateAdmissionError("execution admission requires the bound staged candidate")
    binding = observed.get("prepared_candidate")
    if binding != candidate.payload():
        raise InstallationUpdateAdmissionError("prepared candidate binding changed before execution admission")
    digest = observed.get("plan_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise InstallationUpdateAdmissionError("execution admission plan identity is invalid")
    return ExecutionAdmission(
        schema_version=1,
        operation_id=plan.operation_id,
        plan_digest=digest,
        installation_id=plan.installation_id,
        registered_installation=dict(record),
        prepared_candidate=candidate.payload(),
    )


def admit(plan: InstallationUpdatePlan, *, runner: object = subprocess.run) -> ExecutionAdmission:
    """Durably admit the bound candidate under the existing installation lock.

    The only candidate read is ``recover_prepared_candidate``: it verifies the
    operation-owned stage, marker, venv launcher and package identity.  This
    function never calls source-wheel preparation or selects a service path.
    """
    try:
        with InstallationUpdateSession(plan) as session:
            candidate = session.recover_prepared_candidate(runner=runner)
            if not isinstance(candidate, PreparedUpdateCandidate):
                raise InstallationUpdateAdmissionError("prepared candidate identity is invalid")
            snapshot = _verify_record(plan, candidate)
            bound_provenance = session.prepared_record_provenance()
            if bound_provenance is None:
                raise InstallationUpdateAdmissionError("prepared candidate lacks immutable record provenance")
            if bound_provenance != snapshot:
                raise InstallationUpdateAdmissionError("registered installation changed after prepared candidate binding")
            expected = _evidence(plan, candidate, snapshot)
            existing = session.execution_admission()
            if existing is not None:
                if existing != expected.payload():
                    raise InstallationUpdateAdmissionError("execution admission provenance changed during recovery")
                return expected
            session.bind_execution_admission(expected.payload())
            return expected
    except (InstallationUpdateOperationError, OperationalInstallationLockError) as error:
        raise InstallationUpdateAdmissionError("execution admission operation is invalid") from error
