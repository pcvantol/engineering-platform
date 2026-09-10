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

from . import installation_update_activation, operational_installation_record
from .installation_update_operation import (
    InstallationUpdateOperationError,
    InstallationUpdateSession,
    execution_admission,
    prepared_record_provenance,
    recover_prepared_candidate,
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
    """Bind the whole closed record as well as its stable provenance fields."""
    try:
        canonical = operational_installation_record.validate_record(record)
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateAdmissionError("registered installation provenance is incomplete") from error
    required = ("installation_id", "version", "artifact_digest", "source_revision", "interpreter", "roles")
    if any(key not in canonical for key in required):
        raise InstallationUpdateAdmissionError("registered installation provenance is incomplete")
    return {
        "installation_id": canonical["installation_id"],
        "version": canonical["version"],
        "artifact_digest": canonical["artifact_digest"],
        "source_revision": canonical["source_revision"],
        "interpreter": canonical["interpreter"],
        "roles": canonical["roles"],
        "record_digest": "sha256:" + hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "record": canonical,
    }


def _legacy_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    """Project v2 evidence onto the immutable v1 provenance wire shape."""
    fields = ("installation_id", "version", "artifact_digest", "source_revision", "interpreter", "roles", "record_digest")
    if any(field not in snapshot for field in fields):
        raise InstallationUpdateAdmissionError("registered installation provenance is incomplete")
    return {field: snapshot[field] for field in fields}


def _bound_pre_activation_record(admission: ExecutionAdmission) -> dict[str, object] | None:
    """Return the exact v2 source record; v1 has no safe target recovery."""
    if admission.schema_version == 1:
        return None
    if admission.schema_version != 2:
        raise InstallationUpdateAdmissionError("execution admission is invalid")
    snapshot = dict(admission.registered_installation)
    record = snapshot.get("record")
    if not isinstance(record, Mapping):
        raise InstallationUpdateAdmissionError("execution admission does not bind the complete registered installation")
    try:
        canonical = operational_installation_record.validate_record(record)
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateAdmissionError("execution admission does not bind the complete registered installation") from error
    if snapshot != _record_snapshot(canonical):
        raise InstallationUpdateAdmissionError("execution admission does not bind the complete registered installation")
    return canonical


def _same_launcher(left: str | Path, right: str | Path) -> bool:
    first, second = Path(left).expanduser(), Path(right).expanduser()
    return first.is_absolute() and second.is_absolute() and first.absolute() == second.absolute()


def _verify_record(plan: InstallationUpdatePlan, candidate: PreparedUpdateCandidate) -> dict[str, object]:
    try:
        record = operational_installation_record.load(Path(plan.data_root))
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateAdmissionError("registered operational installation is unavailable") from error
    return _verify_pre_activation_record(plan, candidate, record)


def _verify_pre_activation_record(
    plan: InstallationUpdatePlan,
    candidate: PreparedUpdateCandidate,
    record: Mapping[str, object],
) -> dict[str, object]:
    """Require the exact old record while the journal is pre-activation."""
    if (record.get("installation_id") != plan.installation_id
            or record.get("version") != plan.current_version
            or record.get("artifact_digest") != plan.current_digest):
        raise InstallationUpdateAdmissionError("registered installation changed before execution admission")
    # The candidate is deliberately only operation-owned until an explicit,
    # future activation selects it.  A record that already points at it is a
    # second-runtime/cutover attempt, not a pre-cleanup admission.
    if _same_launcher(str(record["interpreter"]), candidate.interpreter):
        raise InstallationUpdateAdmissionError("prepared candidate is already selected as the operational runtime")
    return _record_snapshot(record)


def _matches_admission_snapshot(admission: ExecutionAdmission, snapshot: Mapping[str, object]) -> bool:
    registered = dict(admission.registered_installation)
    if admission.schema_version == 1:
        return registered == _legacy_snapshot(snapshot)
    if admission.schema_version == 2:
        return registered == dict(snapshot)
    return False


def _require_pre_activation_provenance(
    plan: InstallationUpdatePlan,
    admission: ExecutionAdmission,
    snapshot: Mapping[str, object],
) -> None:
    try:
        provenance = prepared_record_provenance(plan)
    except InstallationUpdateOperationError as error:
        raise InstallationUpdateAdmissionError("execution admission operation is invalid") from error
    if provenance != _legacy_snapshot(snapshot) or not _matches_admission_snapshot(admission, snapshot):
        raise InstallationUpdateAdmissionError("registered installation changed after execution admission")


def _expected_replacement(
    plan: InstallationUpdatePlan,
    admission: ExecutionAdmission,
    candidate: PreparedUpdateCandidate,
) -> dict[str, object] | None:
    """Derive the sole target record that may exist before ``ACTIVATED``.

    Schema v1 captured only a provenance projection.  It can safely resume
    while the old record remains selected, but cannot prove a target record
    retained every channel, role, desired-state, verification, and cleanup
    fact.  Schema v2 binds the full record for this crash-recovery boundary.
    """
    original = _bound_pre_activation_record(admission)
    if original is None:
        return None
    snapshot = _verify_pre_activation_record(plan, candidate, original)
    _require_pre_activation_provenance(plan, admission, snapshot)
    try:
        return installation_update_activation.replacement_record(
            plan, current=original, interpreter=candidate.interpreter,
        )
    except installation_update_activation.InstallationUpdateActivationError as error:
        raise InstallationUpdateAdmissionError("execution admission target record is invalid") from error


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
        schema_version=2,
        operation_id=plan.operation_id,
        plan_digest=digest,
        installation_id=plan.installation_id,
        registered_installation=dict(record),
        prepared_candidate=candidate.payload(),
    )


def admitted_candidate(
    plan: InstallationUpdatePlan,
    admission: ExecutionAdmission,
    *,
    runner: object = subprocess.run,
) -> PreparedUpdateCandidate:
    """Reopen the one candidate already admitted for a resumable update.

    This is the consumption boundary for OI-4c evidence.  It performs no
    preparation, service action, migration, record replacement, or locking;
    a mutating caller must already own ``InstallationUpdateSession``.  The
    candidate launcher comes only from the durable operation binding, never
    from a caller-provided interpreter path.

    Before activation the registered installation still has to equal the
    admission snapshot.  At ``MIGRATED``, schema-v2 admission additionally
    permits exactly the deterministic target record that a crash could leave
    behind after the record CAS and before the ``ACTIVATED`` journal write.
    Schema-v1 evidence has no complete source record and therefore fails
    closed once a target record is selected.
    """
    if (
        not isinstance(plan, InstallationUpdatePlan)
        or not isinstance(admission, ExecutionAdmission)
        or not isinstance(admission.registered_installation, Mapping)
        or not isinstance(admission.prepared_candidate, Mapping)
    ):
        raise InstallationUpdateAdmissionError("execution admission is invalid")
    try:
        observed = status(Path(plan.data_root), plan.operation_id)
        persisted = execution_admission(plan)
        candidate = recover_prepared_candidate(plan, runner=runner)
    except InstallationUpdateOperationError as error:
        raise InstallationUpdateAdmissionError("execution admission operation is invalid") from error
    if not isinstance(candidate, PreparedUpdateCandidate):
        raise InstallationUpdateAdmissionError("prepared candidate identity is invalid")
    if persisted is None or persisted != admission.payload():
        raise InstallationUpdateAdmissionError("execution admission does not bind the exact prepared operation")
    if (
        type(admission.schema_version) is not int
        or admission.schema_version not in {1, 2}
        or admission.operation_id != plan.operation_id
        or admission.installation_id != plan.installation_id
        or admission.plan_digest != observed.get("plan_digest")
        or dict(admission.prepared_candidate) != candidate.payload()
    ):
        raise InstallationUpdateAdmissionError("execution admission does not bind the exact prepared candidate")
    try:
        record = operational_installation_record.load(Path(plan.data_root))
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateAdmissionError("registered operational installation is unavailable") from error
    state = observed.get("state")
    if state in {"PREPARED", "INVENTORIED", "QUIESCED", "BACKED_UP"}:
        _require_pre_activation_provenance(
            plan, admission, _verify_pre_activation_record(plan, candidate, record),
        )
    elif state == "MIGRATED":
        expected = _expected_replacement(plan, admission, candidate)
        if expected is not None and record == expected:
            # This is the only accepted record-after-CAS/journal-before-write
            # crash state.  ``_expected_replacement`` has revalidated the
            # candidate, full v2 admission and durable old provenance.
            pass
        else:
            _require_pre_activation_provenance(
                plan, admission, _verify_pre_activation_record(plan, candidate, record),
            )
    elif state in {"ACTIVATED", "VERIFIED", "CLEANUP_PENDING", "COMPLETE"}:
        expected = _expected_replacement(plan, admission, candidate)
        if expected is None or record != expected:
            raise InstallationUpdateAdmissionError("activated installation does not bind the admitted candidate")
    else:
        raise InstallationUpdateAdmissionError("execution admission operation is invalid")
    return candidate


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
            if bound_provenance != _legacy_snapshot(snapshot):
                raise InstallationUpdateAdmissionError("registered installation changed after prepared candidate binding")
            expected = _evidence(plan, candidate, snapshot)
            # ``bind_execution_admission`` is idempotent for v2 and performs
            # the sole safe v1-to-v2 evidence extension while PREPARED.  A
            # mutating executor only receives the resulting v2 admission.
            session.bind_execution_admission(expected.payload())
            return expected
    except (InstallationUpdateOperationError, OperationalInstallationLockError) as error:
        raise InstallationUpdateAdmissionError("execution admission operation is invalid") from error
