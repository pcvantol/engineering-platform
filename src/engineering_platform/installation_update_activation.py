"""EP-owned activation action for one prepared operational update."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from . import operational_installation, operational_installation_record, server_service
from .installation_update_plan import InstallationUpdatePlan


class InstallationUpdateActivationError(ValueError):
    """The exact replacement runtime cannot become the EP service."""


def _matches_current(plan: InstallationUpdatePlan, record: Mapping[str, object]) -> bool:
    return (
        record.get("installation_id") == plan.installation_id
        and record.get("version") == plan.current_version
        and record.get("artifact_digest") == plan.current_digest
    )


def replacement_record(
    plan: InstallationUpdatePlan,
    *,
    current: Mapping[str, object],
    interpreter: str | Path,
) -> dict[str, object]:
    """Derive the only valid record replacement for one exact plan.

    The full old record is retained deliberately: channel, roles and desired
    state are not target inputs and must survive an in-place release update.
    The three lifecycle fields below are the sole expected transient state
    between the record CAS and durable ``ACTIVATED`` journal transition.
    """
    try:
        source = operational_installation_record.validate_record(current)
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateActivationError("operational installation record is invalid") from error
    if not _matches_current(plan, source):
        raise InstallationUpdateActivationError("operational installation changed before activation")
    target = Path(interpreter).expanduser().absolute()
    try:
        return operational_installation_record.validate_record({
            **source,
            "version": plan.target_version,
            "artifact_digest": plan.target_digest,
            "source_revision": plan.target_source_revision,
            "interpreter": str(target),
            "observed_state": "ACTIVATING",
            "verification": {"result": "PENDING"},
            "cleanup": {"result": "PENDING"},
        })
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdateActivationError("replacement installation record is invalid") from error


def legacy_replacement_record(plan: InstallationUpdatePlan, *, interpreter: str | Path) -> dict[str, object]:
    """Create the first release record only for the proven target release."""
    legacy = plan.legacy_adoption
    if not isinstance(legacy, Mapping) or legacy.get("source_revision") is not None:
        raise InstallationUpdateActivationError("legacy adoption baseline is invalid")
    required = ("instance_id", "data_root", "service_label", "interpreter", "version", "artifact_digest")
    if any(not isinstance(legacy.get(key), str) or not legacy[key] for key in required):
        raise InstallationUpdateActivationError("legacy adoption baseline is invalid")
    if (legacy["instance_id"] != plan.installation_id or legacy["data_root"] != plan.data_root
            or legacy["version"] != plan.current_version or legacy["artifact_digest"] != plan.current_digest):
        raise InstallationUpdateActivationError("legacy adoption baseline changed before activation")
    return operational_installation_record.validate_record({
        "schema_version": 1, "installation_id": plan.installation_id, "version": plan.target_version,
        "channel": "stable", "artifact_digest": plan.target_digest, "source_revision": plan.target_source_revision,
        "interpreter": str(Path(interpreter).expanduser().absolute()), "roles": {"server": legacy["service_label"]},
        "desired_state": "ACTIVE", "observed_state": "ACTIVATING", "verification": {"result": "PENDING"},
        "cleanup": {"result": "PENDING"},
    })


def activate(plan: InstallationUpdatePlan, *, interpreter: str | Path,
             pre_activation_record: Mapping[str, object] | None = None,
             home: Path | None = None, runner: server_service.Runner | None = None) -> Mapping[str, object]:
    """Activate one exact installed EP interpreter, without PATH selection.

    The executor performs the following record CAS only after this action
    returns.  When the v2 admission supplies the full pre-activation record,
    a retry also accepts *only* its deterministic replacement record and
    rechecks that the service still selects the exact candidate.
    """
    root, target = Path(plan.data_root), Path(interpreter).expanduser().absolute()
    try:
        legacy = plan.legacy_adoption
        current = operational_installation_record.load(root) if legacy is None else None
        if legacy is not None:
            replacement = legacy_replacement_record(plan, interpreter=target)
            service_expected = Path(str(legacy["interpreter"])).expanduser().absolute()
        else:
            assert current is not None
            expected_current = (
                operational_installation_record.validate_record(pre_activation_record)
                if pre_activation_record is not None else current
            )
            replacement = replacement_record(plan, current=expected_current, interpreter=target)
            if current == expected_current:
                service_expected = Path(str(expected_current["interpreter"])).expanduser().absolute()
            elif pre_activation_record is not None and current == replacement:
                service_expected = target
            else:
                raise InstallationUpdateActivationError("operational installation changed before activation")
        identity = operational_installation.package_identity(target)
        if (identity["version"] != plan.target_version
                or Path(str(identity["interpreter"])).expanduser().absolute() != target):
            raise InstallationUpdateActivationError("replacement interpreter does not provide the target EP version")
        server_service.replace_runtime(
            root, expected_interpreter=service_expected, interpreter=target,
            home=home, runner=runner,
        )
    except (operational_installation.OperationalInstallationError,
            operational_installation_record.OperationalInstallationRecordError,
            server_service.ServerServiceError) as error:
        raise InstallationUpdateActivationError("exact EP runtime activation failed") from error
    # The installation-record schema is intentionally closed; the durable
    # executor journal captures this returned activation record as evidence.
    return replacement
