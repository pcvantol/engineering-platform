"""One resumable executor for a prepared EP operational-installation update.

The executor owns ordering, locking, durable evidence and the installation
record compare-and-swap.  Product-specific service, backup, migration and
runtime activation mechanics are supplied explicitly by the owning installer;
they cannot be substituted with PATH discovery or an arbitrary shell command.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .installation_update_operation import InstallationUpdateOperationError, InstallationUpdateSession, status
from .installation_update_plan import InstallationUpdatePlan, InstallationUpdatePlanError, verify_exact_artifact
from . import operational_installation_record


EvidenceAction = Callable[[InstallationUpdatePlan], Mapping[str, object]]
ActivationAction = Callable[[InstallationUpdatePlan], Mapping[str, object]]


class InstallationUpdateExecutorError(ValueError):
    """An installer action cannot safely advance its exact update plan."""


@dataclass(frozen=True)
class InstallationUpdateActions:
    inventory: EvidenceAction
    quiesce: EvidenceAction
    backup: EvidenceAction
    migrate: EvidenceAction
    activate: ActivationAction
    verify: EvidenceAction


def _evidence(value: Mapping[str, object], step: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise InstallationUpdateExecutorError(f"installation update {step} evidence is invalid")
    return dict(value)


def _activate(plan: InstallationUpdatePlan, action: ActivationAction) -> dict[str, object]:
    replacement = _evidence(action(plan), "activation")
    if (replacement.get("installation_id") != plan.installation_id
            or replacement.get("version") != plan.target_version
            or replacement.get("artifact_digest") != plan.target_digest
            or replacement.get("source_revision") != plan.target_source_revision):
        raise InstallationUpdateExecutorError("activated installation does not match the exact target identity")
    try:
        updated = operational_installation_record.replace_for_update(
            Path(plan.data_root), expected_version=plan.current_version,
            expected_artifact_digest=plan.current_digest, replacement=replacement,
        )
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateExecutorError("installation update activation record is invalid") from error
    if (updated["version"] != plan.target_version
            or updated["artifact_digest"] != plan.target_digest
            or updated["source_revision"] != plan.target_source_revision):
        raise InstallationUpdateExecutorError("activated installation does not match the exact target identity")
    return {"installation_id": updated["installation_id"], "interpreter": updated["interpreter"],
            "version": updated["version"], "artifact_digest": updated["artifact_digest"],
            "source_revision": updated["source_revision"]}


def _verified(plan: InstallationUpdatePlan, action: EvidenceAction) -> dict[str, object]:
    evidence = _evidence(action(plan), "verification")
    try:
        record = operational_installation_record.load(Path(plan.data_root))
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateExecutorError("activated installation record is unavailable") from error
    if (record["installation_id"] != plan.installation_id or record["version"] != plan.target_version
            or record["artifact_digest"] != plan.target_digest
            or record["source_revision"] != plan.target_source_revision):
        raise InstallationUpdateExecutorError("verified installation does not match the exact target identity")
    return evidence


def _inventory(plan: InstallationUpdatePlan, action: EvidenceAction) -> dict[str, object]:
    """Recheck wheel bytes before accepting the owning inventory evidence."""
    try:
        artifact = verify_exact_artifact(plan)
    except InstallationUpdatePlanError as error:
        raise InstallationUpdateExecutorError("installation update exact artifact is unavailable") from error
    # Product composition owns the shape of its inventory evidence (including
    # target interpreter and compatibility facts), so do not wrap or replace
    # it here. The verified artifact is enforced at the transition boundary;
    # the immutable plan retains its exact path/digest/version identity.
    _ = artifact
    return _evidence(action(plan), "inventory")


def execute(plan: InstallationUpdatePlan, actions: InstallationUpdateActions) -> dict[str, object]:
    """Run or resume one exact plan, without discovering an alternative runtime.

    Each action must be idempotent for the bound plan: a crash after an action
    but before its journal transition causes the same action to be retried.
    No action is reached while another operational update owns the lock.
    """
    steps: tuple[tuple[str, EvidenceAction], ...] = (
        ("INVENTORIED", actions.inventory), ("QUIESCED", actions.quiesce),
        ("BACKED_UP", actions.backup), ("MIGRATED", actions.migrate),
    )
    predecessors = {"INVENTORIED": "PREPARED", "QUIESCED": "INVENTORIED",
                    "BACKED_UP": "QUIESCED", "MIGRATED": "BACKED_UP"}
    try:
        with InstallationUpdateSession(plan) as session:
            current = status(Path(plan.data_root), plan.operation_id)
            for state, action in steps:
                if current["state"] == state:
                    continue
                if current["state"] == predecessors[state]:
                    evidence = _inventory(plan, action) if state == "INVENTORIED" else _evidence(action(plan), state.lower())
                    current = session.advance(state, evidence)
            if current["state"] == "MIGRATED":
                current = session.advance("ACTIVATED", _activate(plan, actions.activate))
            if current["state"] == "ACTIVATED":
                current = session.advance("VERIFIED", _verified(plan, actions.verify))
            if current["state"] in {"VERIFIED", "CLEANUP_PENDING"}:
                current = session.cleanup()
            return current
    except InstallationUpdateOperationError as error:
        raise InstallationUpdateExecutorError("installation update execution failed") from error
