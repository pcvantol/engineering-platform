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
    expected_activation: Mapping[str, object] | None = None
    quiesce_preflight: EvidenceAction | None = None


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
        if plan.legacy_adoption is None:
            updated = operational_installation_record.replace_for_update(
                Path(plan.data_root), expected_version=plan.current_version,
                expected_artifact_digest=plan.current_digest, replacement=replacement,
            )
        else:
            updated = operational_installation_record.record(
                Path(plan.data_root), installation_id=str(replacement["installation_id"]),
                version=str(replacement["version"]), channel=str(replacement["channel"]),
                artifact_digest=str(replacement["artifact_digest"]), source_revision=str(replacement["source_revision"]),
                interpreter=Path(str(replacement["interpreter"])), roles=replacement["roles"],
                desired_state=str(replacement["desired_state"]), observed_state=str(replacement["observed_state"]),
                verification=replacement["verification"], cleanup=replacement["cleanup"],
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


def _recovered_activation(
    plan: InstallationUpdatePlan,
    expected_replacement: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Acknowledge an exact record write whose journal acknowledgement was lost."""
    try:
        expected = (
            operational_installation_record.validate_record(expected_replacement)
            if expected_replacement is not None else None
        )
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateExecutorError("installation update activation expectation is invalid") from error
    if expected is not None and (
        expected["installation_id"] != plan.installation_id
        or expected["version"] != plan.target_version
        or expected["artifact_digest"] != plan.target_digest
        or expected["source_revision"] != plan.target_source_revision
    ):
        raise InstallationUpdateExecutorError("installation update activation expectation is invalid")
    try:
        record = operational_installation_record.load(Path(plan.data_root))
    except operational_installation_record.OperationalInstallationRecordNotFound:
        if plan.legacy_adoption is not None:
            return None
        raise InstallationUpdateExecutorError("registered installation disappeared during activation") from None
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateExecutorError("installation update activation record is invalid") from error
    if expected is not None and record == expected:
        return {"installation_id": record["installation_id"], "interpreter": record["interpreter"],
                "version": record["version"], "artifact_digest": record["artifact_digest"],
                "source_revision": record["source_revision"]}
    if (record["installation_id"] == plan.installation_id
            and record["version"] == plan.current_version
            and record["artifact_digest"] == plan.current_digest):
        return None
    if (expected is None
            and record["installation_id"] == plan.installation_id
            and record["version"] == plan.target_version
            and record["artifact_digest"] == plan.target_digest
            and record["source_revision"] == plan.target_source_revision):
        # Original six-callback callers have no declarative replacement to
        # compare. Never acknowledge the record on identifiers alone; retry
        # their contractually idempotent activation callback instead.
        return None
    raise InstallationUpdateExecutorError("operational installation changed during activation recovery")


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
    try:
        with InstallationUpdateSession(plan) as session:
            current = status(Path(plan.data_root), plan.operation_id)
            if current["state"] == "PREPARED":
                current = session.advance(
                    "INVENTORIED", _inventory(plan, actions.inventory),
                )
            if current["state"] == "INVENTORIED" and actions.quiesce_preflight is not None:
                current = session.advance(
                    "QUIESCING",
                    _evidence(actions.quiesce_preflight(plan), "quiescence preflight"),
                )
            if current["state"] in {"INVENTORIED", "QUIESCING"}:
                current = session.advance(
                    "QUIESCED", _evidence(actions.quiesce(plan), "quiesced"),
                )
            for state, predecessor, action in (
                ("BACKED_UP", "QUIESCED", actions.backup),
                ("MIGRATED", "BACKED_UP", actions.migrate),
            ):
                if current["state"] == predecessor:
                    current = session.advance(state, _evidence(action(plan), state.lower()))
            if current["state"] == "MIGRATED":
                recovered = _recovered_activation(plan, actions.expected_activation)
                current = session.advance(
                    "ACTIVATED", recovered if recovered is not None else _activate(plan, actions.activate),
                )
            if current["state"] == "ACTIVATED":
                current = session.advance("VERIFIED", _verified(plan, actions.verify))
            if current["state"] in {"VERIFIED", "CLEANUP_PENDING"}:
                current = session.cleanup()
            return current
    except InstallationUpdateOperationError as error:
        raise InstallationUpdateExecutorError("installation update execution failed") from error
