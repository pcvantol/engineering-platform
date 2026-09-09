"""Fail-closed, side-effect-free preparation for an EP operational update."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import re

from . import operational_installation_record


_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class InstallationUpdatePlanError(ValueError):
    """An update cannot safely progress from its exact immutable inputs."""


@dataclass(frozen=True)
class InstallationUpdatePlan:
    operation_id: str
    installation_id: str
    data_root: str
    current_version: str
    current_digest: str
    target_version: str
    target_digest: str
    target_source_revision: str
    artifact: str
    cleanup_targets: tuple[str, ...]
    steps: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return asdict(self)


def _digest(path: Path) -> str:
    if not path.is_file() or path.suffix != ".whl":
        raise InstallationUpdatePlanError("target artifact must be an available wheel")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(data_root: Path, *, operation_id: str, artifact: Path, target_version: str,
            target_digest: str, target_source_revision: str) -> InstallationUpdatePlan:
    """Bind an update to one record and wheel without changing the machine.

    Callers must execute this returned, exact plan under the EP installation
    lock.  It deliberately does not create a venv, stop a service, migrate
    data, or delete a path.
    """
    if (_OPERATION.fullmatch(operation_id) is None or _VERSION.fullmatch(target_version) is None
            or _DIGEST.fullmatch(target_digest) is None or _REVISION.fullmatch(target_source_revision) is None):
        raise InstallationUpdatePlanError("update operation or target identity is invalid")
    root = Path(data_root).expanduser().resolve()
    try:
        current = operational_installation_record.load(root)
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise InstallationUpdatePlanError("registered operational installation is unavailable") from error
    exact_artifact = Path(artifact).expanduser().resolve()
    if _digest(exact_artifact) != target_digest:
        raise InstallationUpdatePlanError("target artifact does not match the requested digest")
    operation_root = root / "operations" / operation_id
    cleanup = tuple(str(path) for path in (
        operation_root / "build", operation_root / "download", operation_root / "pip-cache",
    ))
    return InstallationUpdatePlan(
        operation_id=operation_id, installation_id=str(current["installation_id"]), data_root=str(root),
        current_version=str(current["version"]), current_digest=str(current["artifact_digest"]),
        target_version=target_version, target_digest=target_digest,
        target_source_revision=target_source_revision, artifact=str(exact_artifact),
        cleanup_targets=cleanup,
        steps=("INSTALLATION_LOCK", "INVENTORY_AND_COMPATIBILITY", "EXACT_ARTIFACT",
               "QUIESCE", "BACKUP_AND_MIGRATION", "ACTIVATE", "VERIFY", "CLEANUP"),
    )
