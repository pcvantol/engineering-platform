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


def _version(value: str, label: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise InstallationUpdatePlanError(f"{label} version is invalid")
    return tuple(int(part) for part in value.split("."))


def verify_exact_artifact(plan: InstallationUpdatePlan) -> dict[str, str]:
    """Re-read the planned wheel before an update leaves ``PREPARED``.

    A plan records a wheel digest, not a durable copy of its bytes.  An
    interrupted or delayed execution must therefore recheck the same named
    artifact before quiescing the operational service; choosing a newer wheel
    or trusting a changed path would break the plan's identity binding.
    """
    artifact = Path(plan.artifact).expanduser().resolve()
    if _digest(artifact) != plan.target_digest:
        raise InstallationUpdatePlanError("target artifact changed after update preparation")
    return {"path": str(artifact), "digest": plan.target_digest, "version": plan.target_version}


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
    current_version = str(current["version"])
    current_semver, target_semver = _version(current_version, "registered operational"), _version(target_version, "target")
    if target_semver < current_semver:
        # A database rollback must be explicitly proven compatible.  This
        # update primitive has no reverse-migration authority, so it may not
        # turn an older wheel into an accidental rollback.
        raise InstallationUpdatePlanError("downgrade or rollback requires a compatible recovery operation")
    if target_semver == current_semver:
        if target_digest != str(current["artifact_digest"]):
            raise InstallationUpdatePlanError("same release identity cannot use different artifact bytes")
        if target_source_revision != str(current["source_revision"]):
            # Provenance is part of the release identity.  Reusing matching
            # bytes from a different source revision must not make a distinct
            # candidate look already installed or safely resumable.
            raise InstallationUpdatePlanError("same release identity cannot use different source revision")
    exact_artifact = Path(artifact).expanduser().resolve()
    if _digest(exact_artifact) != target_digest:
        raise InstallationUpdatePlanError("target artifact does not match the requested digest")
    operation_root = root / "operations" / operation_id
    cleanup = tuple(str(path) for path in (
        operation_root / "build", operation_root / "download", operation_root / "pip-cache",
    ))
    return InstallationUpdatePlan(
        operation_id=operation_id, installation_id=str(current["installation_id"]), data_root=str(root),
        current_version=current_version, current_digest=str(current["artifact_digest"]),
        target_version=target_version, target_digest=target_digest,
        target_source_revision=target_source_revision, artifact=str(exact_artifact),
        cleanup_targets=cleanup,
        steps=("INSTALLATION_LOCK", "INVENTORY_AND_COMPATIBILITY", "EXACT_ARTIFACT",
               "QUIESCE", "BACKUP_AND_MIGRATION", "ACTIVATE", "VERIFY", "CLEANUP"),
    )
