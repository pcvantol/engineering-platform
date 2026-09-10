"""Compose one bounded EP-owned operational-installation update.

The generic executor owns the durable session and installation lock.  This
module binds that executor to EP's retained CENTRAL backup, exact-target
migration and one-service activation primitives.  It deliberately leaves
service quiescence and live health verification as explicit EP installer
adapters.  The target runtime is read only from durable EP execution-admission
evidence: no PATH lookup, caller-supplied interpreter, arbitrary command, or
cross-product installer input is introduced here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable, Mapping

from . import (
    installation_update_activation,
    installation_update_admission,
    installation_update_backup,
    installation_update_executor,
    installation_update_migration,
    operational_installation,
    operational_installation_record,
    server_service,
)
from .installation_update_admission import ExecutionAdmission
from .installation_update_executor import EvidenceAction, InstallationUpdateActions
from .installation_update_operation import InstallationUpdateOperationError, status
from .installation_update_plan import InstallationUpdatePlan


MigrationRunner = Callable[..., subprocess.CompletedProcess[str]]


class InstallationUpdateCompositionError(ValueError):
    """The EP-specific update composition lacks a safe exact runtime input."""


@dataclass(frozen=True)
class InstallationUpdateOperationalActions:
    """EP-owned actions whose service/health mechanics remain explicit.

    ``inventory`` may collect bounded service-reference and compatibility
    evidence. ``quiesce`` owns the controlled shutdown of the one registered
    EP service. ``verify`` must validate the activated runtime's identity and
    health response; HTTP reachability alone is not sufficient evidence.
    """

    inventory: EvidenceAction
    quiesce: EvidenceAction
    verify: EvidenceAction


def _evidence(value: Mapping[str, object], step: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise InstallationUpdateCompositionError(f"installation update {step} evidence is invalid")
    return dict(value)


def _same_launcher(left: str | Path, right: Path) -> bool:
    """Compare launcher spellings without collapsing distinct venv symlinks."""
    candidate = Path(left).expanduser()
    return candidate.is_absolute() and candidate.absolute() == right


def _admitted_target(
    plan: InstallationUpdatePlan,
    *,
    admission: ExecutionAdmission,
    runner: MigrationRunner,
) -> Path:
    """Resolve only the durable OI-4c candidate launcher for this operation."""
    try:
        candidate = installation_update_admission.admitted_candidate(
            plan, admission, runner=runner,
        )
    except installation_update_admission.InstallationUpdateAdmissionError as error:
        raise InstallationUpdateCompositionError("installation update execution admission is invalid") from error
    target = Path(candidate.interpreter).expanduser()
    # Keep the venv launcher spelling rather than resolving it to a shared
    # base interpreter.  ``admitted_candidate`` already proves its operation
    # binding and package identity; this is only a defensive shape check.
    if not target.is_absolute() or not target.is_file():
        raise InstallationUpdateCompositionError("admitted target interpreter is unavailable")
    return target.absolute()


def _admitted_pre_activation_record(admission: ExecutionAdmission) -> Mapping[str, object] | None:
    """Pass the v2-bound old record to activation; v1 has no target resume."""
    if admission.schema_version == 1:
        return None
    if admission.schema_version != 2:
        raise InstallationUpdateCompositionError("installation update execution admission is invalid")
    record = admission.registered_installation.get("record")
    if not isinstance(record, Mapping):
        raise InstallationUpdateCompositionError("installation update execution admission is invalid")
    try:
        return operational_installation_record.validate_record(record)
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateCompositionError("installation update execution admission is invalid") from error


def _inventory(plan: InstallationUpdatePlan, *, target: Path,
               action: EvidenceAction, runner: MigrationRunner) -> Mapping[str, object]:
    """Prove the planned current record and target package before quiescing."""
    if not target.is_file():
        raise InstallationUpdateCompositionError("target interpreter is unavailable")
    try:
        current = operational_installation_record.load(Path(plan.data_root))
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateCompositionError("registered operational installation is unavailable") from error
    if (current["installation_id"] != plan.installation_id
            or current["version"] != plan.current_version
            or current["artifact_digest"] != plan.current_digest):
        raise InstallationUpdateCompositionError("registered operational installation changed before update")
    try:
        target_identity = operational_installation.package_identity(target, runner=runner)
    except operational_installation.OperationalInstallationError as error:
        raise InstallationUpdateCompositionError("target EP interpreter is unavailable") from error
    if (target_identity["version"] != plan.target_version
            or not _same_launcher(target_identity["interpreter"], target)):
        raise InstallationUpdateCompositionError("target interpreter does not provide the planned EP package")
    return {
        "registered_installation": {
            "installation_id": current["installation_id"],
            "version": current["version"],
            "artifact_digest": current["artifact_digest"],
        },
        "target_package": {
            "interpreter": str(target),
            "version": target_identity["version"],
        },
        "inspection": _evidence(action(plan), "inventory"),
    }


def _bound_target(
    plan: InstallationUpdatePlan,
    *,
    admission: ExecutionAdmission,
    runner: MigrationRunner,
) -> Path:
    """Require a recovery attempt to keep the target proven at inventory."""
    target = _admitted_target(plan, admission=admission, runner=runner)
    try:
        events = status(Path(plan.data_root), plan.operation_id)["events"]
    except InstallationUpdateOperationError as error:
        raise InstallationUpdateCompositionError("installation update operation is unavailable") from error
    evidence = next((event.get("evidence") for event in events
                     if isinstance(event, dict) and event.get("state") == "INVENTORIED"), None)
    if not isinstance(evidence, dict):
        raise InstallationUpdateCompositionError("installation update lacks target inventory evidence")
    identity = evidence.get("target_package")
    if not isinstance(identity, dict) or not isinstance(identity.get("interpreter"), str):
        raise InstallationUpdateCompositionError("installation update target inventory evidence is invalid")
    if not _same_launcher(identity["interpreter"], target):
        raise InstallationUpdateCompositionError("installation update target interpreter changed during recovery")
    return target


def _verify(
    plan: InstallationUpdatePlan,
    *,
    admission: ExecutionAdmission,
    action: EvidenceAction,
    runner: MigrationRunner,
) -> Mapping[str, object]:
    """Keep verification bound to the runtime activated from the inventoried path."""
    expected = _bound_target(plan, admission=admission, runner=runner)
    try:
        record = operational_installation_record.load(Path(plan.data_root))
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateCompositionError("activated operational installation is unavailable") from error
    if not _same_launcher(str(record["interpreter"]), expected):
        raise InstallationUpdateCompositionError("activated operational interpreter differs from the planned target")
    return _evidence(action(plan), "verification")


def compose(plan: InstallationUpdatePlan, *, admission: ExecutionAdmission,
            actions: InstallationUpdateOperationalActions,
            migration_runner: MigrationRunner = subprocess.run,
            activation_home: Path | None = None,
            activation_runner: server_service.Runner | None = None) -> InstallationUpdateActions:
    """Bind EP primitives to a prepared plan; the executor acquires the lock.

    The target path is deliberately *not* supplied by a caller.  It is
    reopened from the immutable OI-4c admission and candidate binding.  The
    resulting callbacks are passed only to :func:`execute`, which enters the
    existing ``InstallationUpdateSession`` before inventory, quiescence,
    backup, migration, activation, verification and cleanup can progress.
    """
    # A legacy v1 admission can be read and safely extended by ``admit``
    # while PREPARED, but it never reaches a mutating executor.  Its
    # provenance projection cannot prove the crash window after the record
    # CAS and before the ACTIVATED journal write.
    if (not isinstance(admission, ExecutionAdmission)
            or type(admission.schema_version) is not int
            or admission.schema_version != 2):
        raise InstallationUpdateCompositionError("installation update requires a v2 execution admission")
    # Reject an absent, forged, tampered, or stale admission before the
    # executor can reach a service quiesce action.  Every later runtime step
    # repeats the same durable lookup while that executor owns the lock.
    _admitted_target(plan, admission=admission, runner=migration_runner)
    pre_activation_record = _admitted_pre_activation_record(admission)

    def guarded(action: EvidenceAction) -> EvidenceAction:
        def invoke(bound_plan: InstallationUpdatePlan) -> Mapping[str, object]:
            _admitted_target(bound_plan, admission=admission, runner=migration_runner)
            return action(bound_plan)
        return invoke

    return InstallationUpdateActions(
        inventory=lambda bound_plan: _inventory(
            bound_plan,
            target=_admitted_target(bound_plan, admission=admission, runner=migration_runner),
            action=actions.inventory,
            runner=migration_runner,
        ),
        quiesce=guarded(actions.quiesce),
        backup=guarded(installation_update_backup.backup),
        migrate=lambda bound_plan: installation_update_migration.migrate(
            bound_plan,
            interpreter=_bound_target(bound_plan, admission=admission, runner=migration_runner),
            runner=migration_runner,
        ),
        activate=lambda bound_plan: installation_update_activation.activate(
            bound_plan,
            interpreter=_bound_target(bound_plan, admission=admission, runner=migration_runner),
            pre_activation_record=pre_activation_record,
            home=activation_home,
            runner=activation_runner,
        ),
        verify=lambda bound_plan: _verify(
            bound_plan,
            admission=admission,
            action=actions.verify,
            runner=migration_runner,
        ),
    )


def execute(plan: InstallationUpdatePlan, *, admission: ExecutionAdmission,
            actions: InstallationUpdateOperationalActions,
            migration_runner: MigrationRunner = subprocess.run,
            activation_home: Path | None = None,
            activation_runner: server_service.Runner | None = None) -> dict[str, object]:
    """Execute or resume the one exact EP update using the existing lock."""
    return installation_update_executor.execute(
        plan,
        compose(
            plan, admission=admission, actions=actions,
            migration_runner=migration_runner, activation_home=activation_home,
            activation_runner=activation_runner,
        ),
    )
