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

from . import (
    installation_update_activation,
    legacy_installation_adoption,
    operational_installation,
    operational_installation_record,
    server_service,
)
from .installation_update_operation import (
    InstallationUpdateOperationError,
    InstallationUpdateSession,
    current_record_provenance,
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
    if admission.schema_version in {1, 3}:
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


def _verify_record(plan: InstallationUpdatePlan, candidate: PreparedUpdateCandidate, *,
                   runner: object = subprocess.run, service_home: Path | None = None,
                   allowed_target_interpreter: str | Path | None = None,
                   allow_target_configured_version: bool = False) -> dict[str, object]:
    if plan.legacy_adoption is not None:
        if _same_launcher(str(plan.legacy_adoption.get("interpreter", "")), candidate.interpreter):
            raise InstallationUpdateAdmissionError("prepared candidate is already selected as the legacy runtime")
        try:
            legacy_installation_adoption.revalidate_bound_baseline(
                Path(plan.data_root), operation_id=plan.operation_id,
                target_version=plan.target_version, target_digest=plan.target_digest,
                target_source_revision=plan.target_source_revision, runner=runner,
                service_home=service_home,
                allowed_target_interpreter=allowed_target_interpreter,
                allow_target_configured_version=allow_target_configured_version,
            )
            return current_record_provenance(plan)
        except (legacy_installation_adoption.LegacyInstallationAdoptionError,
                InstallationUpdateOperationError) as error:
            raise InstallationUpdateAdmissionError("authorized legacy installation changed before execution") from error
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
    if admission.schema_version == 3:
        return registered == dict(snapshot)
    return False


def _require_retained_candidate_package(
    candidate: PreparedUpdateCandidate,
    *,
    runner: object = subprocess.run,
) -> None:
    """Reprove the installed candidate after its staged wheel is removed."""
    try:
        identity = operational_installation.package_identity(candidate.interpreter, runner=runner)
    except (operational_installation.OperationalInstallationError, OSError, subprocess.SubprocessError) as error:
        raise InstallationUpdateAdmissionError(
            "activated candidate package identity is unavailable after verification"
        ) from error
    candidate_root = Path(candidate.candidate_venv)
    launcher = Path(candidate.interpreter)
    try:
        retained_paths = tuple(Path(str(identity[label])) for label in ("metadata", "package"))
        if (
            candidate_root.is_symlink()
            or not candidate_root.is_dir()
            or launcher.parent.is_symlink()
            or not launcher.parent.is_dir()
            or not launcher.is_file()
            or any(not path.is_absolute() or not path.is_dir() for path in retained_paths)
        ):
            raise ValueError
        retained_root = candidate_root.resolve(strict=True)
        for path in retained_paths:
            path.resolve(strict=True).relative_to(retained_root)
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise InstallationUpdateAdmissionError(
            "activated candidate package identity is unavailable after verification"
        ) from error
    if dict(identity) != dict(candidate.package):
        raise InstallationUpdateAdmissionError(
            "activated candidate package identity changed after verification"
        )


def _require_activated_service(
    plan: InstallationUpdatePlan,
    candidate: PreparedUpdateCandidate,
    *,
    service_home: Path | None = None,
) -> None:
    """Keep cleanup bound to the exact activated service and data root."""
    try:
        selected = server_service.configured_interpreter(Path(plan.data_root), home=service_home)
    except server_service.ServerServiceError as error:
        raise InstallationUpdateAdmissionError(
            "activated service does not bind the admitted candidate"
        ) from error
    if selected != Path(candidate.interpreter):
        raise InstallationUpdateAdmissionError(
            "activated service does not bind the admitted candidate"
        )


def _require_pre_activation_provenance(
    plan: InstallationUpdatePlan,
    admission: ExecutionAdmission,
    snapshot: Mapping[str, object],
) -> None:
    try:
        provenance = prepared_record_provenance(plan)
    except InstallationUpdateOperationError as error:
        raise InstallationUpdateAdmissionError("execution admission operation is invalid") from error
    expected_provenance = dict(snapshot) if admission.schema_version == 3 else _legacy_snapshot(snapshot)
    if provenance != expected_provenance or not _matches_admission_snapshot(admission, snapshot):
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
    fact.  Schema v2 binds the full record for this crash-recovery boundary;
    schema v3 instead binds the complete legacy decision without inventing a
    historical release record.
    """
    if admission.schema_version == 3:
        try:
            provenance = prepared_record_provenance(plan)
            if provenance != dict(admission.registered_installation):
                raise InstallationUpdateAdmissionError("legacy installation changed after execution admission")
            return installation_update_activation.legacy_replacement_record(
                plan, interpreter=candidate.interpreter,
            )
        except (InstallationUpdateOperationError,
                installation_update_activation.InstallationUpdateActivationError) as error:
            raise InstallationUpdateAdmissionError("execution admission target record is invalid") from error
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


def _validate_legacy_admission_identity(
    plan: InstallationUpdatePlan,
    admission: ExecutionAdmission,
) -> None:
    """Keep plan, journal, adoption decision and current owner exactly aligned."""
    try:
        decision = legacy_installation_adoption.bound_decision(
            Path(plan.data_root), operation_id=plan.operation_id,
            target_version=plan.target_version, target_digest=plan.target_digest,
            target_source_revision=plan.target_source_revision,
        )
        _canonical, observation, authorization = legacy_installation_adoption.validate_decision(decision)
        if (admission.registered_installation.get("decision") != decision
                or observation.payload() != plan.legacy_adoption):
            raise InstallationUpdateAdmissionError("legacy execution admission identity changed")
        legacy_installation_adoption.local_owner_authority(authorization, observation)
    except legacy_installation_adoption.LegacyInstallationAdoptionError as error:
        raise InstallationUpdateAdmissionError("legacy execution admission identity is invalid") from error


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
        schema_version=3 if plan.legacy_adoption is not None else 2,
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
    service_home: Path | None = None,
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
    closed once a target record is selected. Schema-v3 legacy evidence allows
    only the same admitted target and retains unknown historical provenance.
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
    except InstallationUpdateOperationError as error:
        raise InstallationUpdateAdmissionError("execution admission operation is invalid") from error
    state = observed.get("state")
    try:
        if state in {"VERIFIED", "CLEANUP_PENDING", "COMPLETE"}:
            # Cleanup deliberately removes the staged wheel.  Once exact live
            # verification is durable, reopen the digest-checked journal
            # binding and prove the target record/service below instead of
            # requiring an operation-owned input that no longer should exist.
            binding = observed.get("prepared_candidate")
            if not isinstance(binding, dict):
                raise TypeError
            candidate = PreparedUpdateCandidate(**binding)
        else:
            candidate = recover_prepared_candidate(plan, runner=runner)
    except (InstallationUpdateOperationError, TypeError, ValueError) as error:
        raise InstallationUpdateAdmissionError("execution admission operation is invalid") from error
    if not isinstance(candidate, PreparedUpdateCandidate):
        raise InstallationUpdateAdmissionError("prepared candidate identity is invalid")
    if persisted is None or persisted != admission.payload():
        raise InstallationUpdateAdmissionError("execution admission does not bind the exact prepared operation")
    if (
        type(admission.schema_version) is not int
        or admission.schema_version not in {1, 2, 3}
        or admission.operation_id != plan.operation_id
        or admission.installation_id != plan.installation_id
        or admission.plan_digest != observed.get("plan_digest")
        or dict(admission.prepared_candidate) != candidate.payload()
    ):
        raise InstallationUpdateAdmissionError("execution admission does not bind the exact prepared candidate")
    if state in {"VERIFIED", "CLEANUP_PENDING", "COMPLETE"}:
        _require_retained_candidate_package(candidate, runner=runner)
        _require_activated_service(plan, candidate, service_home=service_home)
    if admission.schema_version == 3:
        if plan.legacy_adoption is None:
            raise InstallationUpdateAdmissionError("execution admission legacy baseline is unavailable")
        _validate_legacy_admission_identity(plan, admission)
        expected = _expected_replacement(plan, admission, candidate)
        try:
            record = operational_installation_record.load(Path(plan.data_root))
        except operational_installation_record.OperationalInstallationRecordNotFound:
            record = None
        except operational_installation_record.OperationalInstallationRecordError as error:
            raise InstallationUpdateAdmissionError("operational installation registration is invalid") from error
        if state in {"PREPARED", "INVENTORIED", "QUIESCED", "BACKED_UP"}:
            if record is not None:
                raise InstallationUpdateAdmissionError("operational installation conflicts with admitted legacy baseline")
            snapshot = _verify_record(
                plan, candidate, runner=runner, service_home=service_home,
                allow_target_configured_version=state == "BACKED_UP",
            )
            _require_pre_activation_provenance(plan, admission, snapshot)
        elif state == "MIGRATED":
            if record is None:
                snapshot = _verify_record(
                    plan, candidate, runner=runner, service_home=service_home,
                    allowed_target_interpreter=candidate.interpreter,
                )
                _require_pre_activation_provenance(plan, admission, snapshot)
            elif record != expected:
                raise InstallationUpdateAdmissionError("activated installation does not bind the admitted legacy candidate")
            elif server_service.configured_interpreter(Path(plan.data_root), home=service_home) != Path(candidate.interpreter):
                raise InstallationUpdateAdmissionError("activated service does not bind the admitted legacy candidate")
        elif state in {"ACTIVATED", "VERIFIED", "CLEANUP_PENDING", "COMPLETE"}:
            if record != expected:
                raise InstallationUpdateAdmissionError("activated installation does not bind the admitted legacy candidate")
            if server_service.configured_interpreter(Path(plan.data_root), home=service_home) != Path(candidate.interpreter):
                raise InstallationUpdateAdmissionError("activated service does not bind the admitted legacy candidate")
        else:
            raise InstallationUpdateAdmissionError("execution admission operation is invalid")
        return candidate
    try:
        record = operational_installation_record.load(Path(plan.data_root))
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateAdmissionError("registered operational installation is unavailable") from error
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


def admit(plan: InstallationUpdatePlan, *, runner: object = subprocess.run,
          service_home: Path | None = None) -> ExecutionAdmission:
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
            snapshot = _verify_record(plan, candidate, runner=runner, service_home=service_home)
            bound_provenance = session.prepared_record_provenance()
            if bound_provenance is None:
                raise InstallationUpdateAdmissionError("prepared candidate lacks immutable record provenance")
            expected_provenance = dict(snapshot) if plan.legacy_adoption is not None else _legacy_snapshot(snapshot)
            if bound_provenance != expected_provenance:
                raise InstallationUpdateAdmissionError("registered installation changed after prepared candidate binding")
            expected = _evidence(plan, candidate, snapshot)
            # ``bind_execution_admission`` is idempotent for v2/v3 and performs
            # the sole safe v1-to-v2 evidence extension while PREPARED.  A
            # mutating executor only receives a complete admission.
            session.bind_execution_admission(expected.payload())
            return expected
    except (InstallationUpdateOperationError, OperationalInstallationLockError) as error:
        raise InstallationUpdateAdmissionError("execution admission operation is invalid") from error
