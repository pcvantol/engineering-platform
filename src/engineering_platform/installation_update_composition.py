"""Compose one bounded EP-owned operational-installation update.

The generic executor owns the durable session and installation lock.  This
module binds that executor to EP's retained CENTRAL backup, exact-target
migration and one-service activation primitives.  It deliberately leaves
service quiescence and live health verification as explicit EP installer
adapters: no PATH lookup, arbitrary command, or cross-product installer is
introduced here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable, Mapping

from . import (
    installation_update_activation,
    installation_update_backup,
    installation_update_executor,
    installation_update_migration,
    operational_installation,
    operational_installation_record,
    server_service,
)
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


def _interpreter(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise InstallationUpdateCompositionError("target interpreter must be an absolute path")
    # Keep a venv launcher spelling intact.  Resolving it would turn a
    # ``venv/bin/python`` symlink into its base interpreter.
    return candidate.absolute()


def _evidence(value: Mapping[str, object], step: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise InstallationUpdateCompositionError(f"installation update {step} evidence is invalid")
    return dict(value)


def _same_launcher(left: str | Path, right: Path) -> bool:
    """Compare launcher spellings without collapsing distinct venv symlinks."""
    candidate = Path(left).expanduser()
    return candidate.is_absolute() and candidate.absolute() == right


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


def _bound_target(plan: InstallationUpdatePlan, target: Path) -> Path:
    """Require a recovery attempt to keep the target proven at inventory."""
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


def _verify(plan: InstallationUpdatePlan, *, target: Path,
            action: EvidenceAction) -> Mapping[str, object]:
    """Keep verification bound to the runtime activated from the inventoried path."""
    expected = _bound_target(plan, target)
    try:
        record = operational_installation_record.load(Path(plan.data_root))
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateCompositionError("activated operational installation is unavailable") from error
    if not _same_launcher(str(record["interpreter"]), expected):
        raise InstallationUpdateCompositionError("activated operational interpreter differs from the planned target")
    return _evidence(action(plan), "verification")


def compose(plan: InstallationUpdatePlan, *, target_interpreter: str | Path,
            actions: InstallationUpdateOperationalActions,
            migration_runner: MigrationRunner = subprocess.run,
            activation_home: Path | None = None,
            activation_runner: server_service.Runner | None = None) -> InstallationUpdateActions:
    """Bind EP primitives to a prepared plan; the executor acquires the lock.

    The target path is deliberately supplied rather than discovered.  The
    resulting callbacks are passed only to :func:`execute`, which enters the
    existing ``InstallationUpdateSession`` before inventory, quiescence,
    backup, migration, activation, verification and cleanup can progress.
    """
    target = _interpreter(target_interpreter)
    return InstallationUpdateActions(
        inventory=lambda bound_plan: _inventory(
            bound_plan, target=target, action=actions.inventory, runner=migration_runner,
        ),
        quiesce=actions.quiesce,
        backup=installation_update_backup.backup,
        migrate=lambda bound_plan: installation_update_migration.migrate(
            bound_plan, interpreter=_bound_target(bound_plan, target), runner=migration_runner,
        ),
        activate=lambda bound_plan: installation_update_activation.activate(
            bound_plan, interpreter=_bound_target(bound_plan, target), home=activation_home,
            runner=activation_runner,
        ),
        verify=lambda bound_plan: _verify(bound_plan, target=target, action=actions.verify),
    )


def execute(plan: InstallationUpdatePlan, *, target_interpreter: str | Path,
            actions: InstallationUpdateOperationalActions,
            migration_runner: MigrationRunner = subprocess.run,
            activation_home: Path | None = None,
            activation_runner: server_service.Runner | None = None) -> dict[str, object]:
    """Execute or resume the one exact EP update using the existing lock."""
    return installation_update_executor.execute(
        plan,
        compose(
            plan, target_interpreter=target_interpreter, actions=actions,
            migration_runner=migration_runner, activation_home=activation_home,
            activation_runner=activation_runner,
        ),
    )
