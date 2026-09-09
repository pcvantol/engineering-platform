"""EP-owned activation action for one prepared operational update."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from . import operational_installation, operational_installation_record, server_service
from .installation_update_plan import InstallationUpdatePlan


class InstallationUpdateActivationError(ValueError):
    """The exact replacement runtime cannot become the EP service."""


def activate(plan: InstallationUpdatePlan, *, interpreter: str | Path,
             home: Path | None = None, runner: server_service.Runner | None = None) -> Mapping[str, object]:
    """Activate one exact installed EP interpreter, without PATH selection.

    The executor performs the following record CAS only after this action
    returns.  A retry accepts the already-written replacement service record
    and completes its idempotent bootstrap through ``replace_runtime``.
    """
    root, target = Path(plan.data_root), Path(interpreter).expanduser().absolute()
    try:
        current = operational_installation_record.load(root)
        if (current["installation_id"] != plan.installation_id
                or current["version"] != plan.current_version
                or current["artifact_digest"] != plan.current_digest):
            raise InstallationUpdateActivationError("operational installation changed before activation")
        identity = operational_installation.package_identity(target)
        if identity["version"] != plan.target_version:
            raise InstallationUpdateActivationError("replacement interpreter does not provide the target EP version")
        server_service.replace_runtime(
            root, expected_interpreter=str(current["interpreter"]), interpreter=target,
            home=home, runner=runner,
        )
    except (operational_installation.OperationalInstallationError,
            operational_installation_record.OperationalInstallationRecordError,
            server_service.ServerServiceError) as error:
        raise InstallationUpdateActivationError("exact EP runtime activation failed") from error
    # The installation-record schema is intentionally closed; the durable
    # executor journal captures this returned activation record as evidence.
    return {**current, "version": plan.target_version, "artifact_digest": plan.target_digest,
            "source_revision": plan.target_source_revision, "interpreter": str(target),
            "observed_state": "ACTIVATING", "verification": {"result": "PENDING"},
            "cleanup": {"result": "PENDING"}}
