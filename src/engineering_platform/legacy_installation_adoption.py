"""Bounded adoption evidence for an artifact-identified legacy EP runtime.

This is intentionally separate from :mod:`operational_installation_record`.
The latter is a release assertion and therefore always requires a source
revision.  An adoption is an observation and local-owner authorization for a
single transition; it never reconstructs a historical build receipt.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import base64
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
import tempfile
from typing import Callable, Mapping
import zipfile

from . import operational_installation, operational_installation_record, server_service
from .operational_installation_lock import OperationalInstallationLock, OperationalInstallationLockError


FILENAME = "legacy-installation-adoption.json"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")


class LegacyInstallationAdoptionError(ValueError):
    """The legacy runtime is not sufficiently identified or authorized."""


class _LegacyAdoptionNotFound(LegacyInstallationAdoptionError):
    """No directory entry exists for the adoption decision."""


@dataclass(frozen=True)
class LegacyInstallationObservation:
    """Facts observed without claiming unavailable historical provenance."""
    instance_id: str
    data_root: str
    service_label: str
    interpreter: str
    package: str
    version: str
    artifact_digest: str
    wheel: str
    source_revision: None = None

    def payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyAdoptionAuthorization:
    """The exact owner-approved transition, not a reusable force flag."""
    instance_id: str
    data_root: str
    service_label: str
    interpreter: str
    old_artifact_digest: str
    target_version: str
    target_artifact_digest: str
    target_source_revision: str
    operation_id: str
    acknowledge_unknown_source_revision: bool

    def payload(self) -> dict[str, object]:
        return asdict(self)


def _digest(path: Path) -> str:
    if not path.is_file() or path.suffix != ".whl":
        raise LegacyInstallationAdoptionError("preserved artifact must be a wheel")
    try:
        with path.open("rb") as stream:
            value = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as error:
        raise LegacyInstallationAdoptionError("preserved artifact is unreadable") from error
    return "sha256:" + value


def _wheel_record(path: Path) -> tuple[str, dict[str, str]]:
    """Read metadata and hashes from a wheel; never import wheel code."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise LegacyInstallationAdoptionError("preserved wheel has duplicate archive members")
            for name in names:
                member = PurePosixPath(name)
                if member.is_absolute() or ".." in member.parts or not name or "\\" in name:
                    raise LegacyInstallationAdoptionError("preserved wheel has unsafe archive members")
            metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
            records = [name for name in names if name.endswith(".dist-info/RECORD")]
            if len(metadata) != 1 or len(records) != 1:
                raise LegacyInstallationAdoptionError("preserved wheel has ambiguous metadata")
            fields: dict[str, str] = {}
            for line in archive.read(metadata[0]).decode("utf-8", "strict").splitlines():
                key, separator, value = line.partition(":")
                if separator and key in {"Name", "Version"} and key not in fields:
                    fields[key] = value.strip()
            if fields.get("Name") != "engineering-platform" or not _VERSION.fullmatch(fields.get("Version", "")):
                raise LegacyInstallationAdoptionError("preserved wheel does not identify an EP release")
            hashes: dict[str, str] = {}
            for line in archive.read(records[0]).decode("utf-8", "strict").splitlines():
                name, digest, _size = line.split(",", 2)
                algorithm, separator, encoded = digest.partition("=")
                if algorithm == "sha256" and separator and name.startswith("engineering_platform/"):
                    if name not in names:
                        raise LegacyInstallationAdoptionError("preserved wheel RECORD names a missing package member")
                    actual = base64.urlsafe_b64encode(
                        hashlib.sha256(archive.read(name)).digest()
                    ).rstrip(b"=").decode("ascii")
                    if actual != encoded:
                        raise LegacyInstallationAdoptionError("preserved wheel package bytes differ from RECORD")
                    hashes[name] = encoded
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
        if isinstance(error, LegacyInstallationAdoptionError):
            raise
        raise LegacyInstallationAdoptionError("preserved wheel is invalid") from error
    if not hashes:
        raise LegacyInstallationAdoptionError("preserved wheel has no EP package hashes")
    return fields["Version"], hashes


def _matches_wheel_package(package: Path, hashes: Mapping[str, str]) -> None:
    # Every packaged production file must match.  Extra files are not ignored:
    # they are rejected below, except normal Python cache files.
    expected = {name.removeprefix("engineering_platform/"): digest for name, digest in hashes.items()}
    actual = {str(path.relative_to(package)).replace(os.sep, "/") for path in package.rglob("*")
              if path.is_file() and "__pycache__" not in path.parts}
    if actual != set(expected):
        raise LegacyInstallationAdoptionError("installed EP package differs from preserved artifact")
    for relative, encoded in expected.items():
        data = (package / relative).read_bytes()
        observed = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        if observed != encoded:
            raise LegacyInstallationAdoptionError("installed EP package differs from preserved artifact")


def inspect(*, installation: operational_installation.OperationalInstallation,
            service_label: str, service_interpreter: str | Path, preserved_wheel: Path,
            runner: Callable[..., object] | None = None) -> LegacyInstallationObservation:
    """Read-only proof of the selected service, runtime package and old wheel."""
    if not isinstance(service_label, str) or not service_label:
        raise LegacyInstallationAdoptionError("selected service is invalid")
    selected = operational_installation.launcher(installation.interpreter)
    if selected != operational_installation.launcher(service_interpreter):
        raise LegacyInstallationAdoptionError("selected service does not use the observed interpreter")
    try:
        identity = operational_installation.package_identity(
            selected, runner=operational_installation.subprocess.run if runner is None else runner,
        )
        operational_installation.validate_package_identity(installation, identity)
    except operational_installation.OperationalInstallationError as error:
        raise LegacyInstallationAdoptionError("selected service does not provide an EP package") from error
    wheel = Path(preserved_wheel).expanduser().resolve()
    version, hashes = _wheel_record(wheel)
    if version != identity["version"]:
        raise LegacyInstallationAdoptionError("preserved artifact version differs from selected service")
    _matches_wheel_package(Path(identity["package"]), hashes)
    return LegacyInstallationObservation(
        instance_id=installation.instance_id, data_root=str(Path(installation.data_root).resolve()),
        service_label=service_label, interpreter=str(selected), package=str(Path(identity["package"]).resolve()),
        version=version, artifact_digest=_digest(wheel), wheel=str(wheel), source_revision=None,
    )


def _validate_authorization(value: LegacyAdoptionAuthorization, observation: LegacyInstallationObservation) -> None:
    if not value.acknowledge_unknown_source_revision:
        raise LegacyInstallationAdoptionError("owner must explicitly acknowledge the unknown source revision")
    if not (_DIGEST.fullmatch(value.old_artifact_digest) and _DIGEST.fullmatch(value.target_artifact_digest)
            and _REVISION.fullmatch(value.target_source_revision) and _VERSION.fullmatch(value.target_version)
            and isinstance(value.operation_id, str) and value.operation_id):
        raise LegacyInstallationAdoptionError("adoption authorization identity is invalid")
    if (value.instance_id != observation.instance_id or Path(value.data_root).resolve() != Path(observation.data_root).resolve()
            or value.service_label != observation.service_label
            or operational_installation.launcher(value.interpreter) != operational_installation.launcher(observation.interpreter)
            or value.old_artifact_digest != observation.artifact_digest):
        raise LegacyInstallationAdoptionError("adoption authorization does not bind the observed legacy installation")


def local_owner_authority(authorization: LegacyAdoptionAuthorization, observation: LegacyInstallationObservation) -> None:
    """Native local-owner gate for the existing user-service/data-root topology.

    The operator must own the data root and it must not be group/world writable.
    This is deliberately a real OS authority check in addition to the exact,
    acknowledged authorization object; a JSON id or ``--force`` is insufficient.
    """
    _validate_authorization(authorization, observation)
    root = Path(observation.data_root)
    try:
        details = root.stat()
    except OSError as error:
        raise LegacyInstallationAdoptionError("observed data root is unavailable") from error
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o022:
        raise LegacyInstallationAdoptionError("current local user does not exclusively own the observed data root")


def _read_decision(path: Path) -> object:
    """Read one regular decision file without following a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        try:
            path.lstat()
        except FileNotFoundError:
            raise _LegacyAdoptionNotFound("legacy adoption record is absent") from error
        except OSError as state_error:
            raise LegacyInstallationAdoptionError("legacy adoption record is unreadable") from state_error
        raise LegacyInstallationAdoptionError("legacy adoption record is unreadable") from error
    except OSError as error:
        raise LegacyInstallationAdoptionError("legacy adoption record is unreadable") from error
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise LegacyInstallationAdoptionError("legacy adoption record is unreadable")
            return json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LegacyInstallationAdoptionError("legacy adoption record is unreadable") from error


def adopt(*, observation: LegacyInstallationObservation, authorization: LegacyAdoptionAuthorization,
          authorizer: Callable[[LegacyAdoptionAuthorization, LegacyInstallationObservation], None] = local_owner_authority) -> dict[str, object]:
    """Atomically persist one non-release adoption fact; never writes a release record."""
    root, path = Path(observation.data_root).resolve(), Path(observation.data_root).resolve() / FILENAME
    payload = {"schema_version": 1, "kind": "OBSERVED_LEGACY_ARTIFACT", "observation": observation.payload(),
               "authorization": authorization.payload()}
    lock = OperationalInstallationLock(root)
    try:
        lock.acquire(authorization.operation_id)
    except OperationalInstallationLockError as error:
        raise LegacyInstallationAdoptionError("legacy adoption cannot acquire the operational installation lock") from error
    try:
        _validate_authorization(authorization, observation)
        authorizer(authorization, observation)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            existing = _read_decision(path)
        except _LegacyAdoptionNotFound:
            existing = None
        if existing is not None:
            if existing != payload:
                raise LegacyInstallationAdoptionError("legacy adoption record already exists with different identity")
            return payload
        descriptor, temporary = tempfile.mkstemp(prefix=".legacy-adoption-", dir=root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = _read_decision(path)
                if existing != payload:
                    raise LegacyInstallationAdoptionError(
                        "legacy adoption record already exists with different identity"
                    )
        except BaseException:
            Path(temporary).unlink(missing_ok=True); raise
        finally:
            Path(temporary).unlink(missing_ok=True)
        return payload
    finally:
        lock.release(authorization.operation_id)


def validate_decision(value: object) -> tuple[dict[str, object], LegacyInstallationObservation,
                                               LegacyAdoptionAuthorization]:
    """Validate the closed durable wire shape without consulting live state."""
    try:
        if not isinstance(value, dict) or set(value) != {"schema_version", "kind", "observation", "authorization"}:
            raise ValueError
        observation = LegacyInstallationObservation(**value["observation"])
        authorization = LegacyAdoptionAuthorization(**value["authorization"])
    except (TypeError, ValueError) as error:
        raise LegacyInstallationAdoptionError("legacy adoption record is invalid") from error
    if value["schema_version"] != 1 or value["kind"] != "OBSERVED_LEGACY_ARTIFACT" or observation.source_revision is not None:
        raise LegacyInstallationAdoptionError("legacy adoption record is invalid")
    _validate_authorization(authorization, observation)
    canonical = {"schema_version": 1, "kind": "OBSERVED_LEGACY_ARTIFACT",
                 "observation": observation.payload(), "authorization": authorization.payload()}
    if canonical != value:
        raise LegacyInstallationAdoptionError("legacy adoption record is invalid")
    return canonical, observation, authorization


def bound_decision(data_root: Path, *, operation_id: str, target_version: str,
                   target_digest: str, target_source_revision: str) -> dict[str, object]:
    """Reopen the exact durable observation and owner authorization."""
    root = Path(data_root).expanduser().resolve()
    try:
        raw = _read_decision(root / FILENAME)
    except LegacyInstallationAdoptionError as error:
        raise LegacyInstallationAdoptionError("legacy adoption record is invalid") from error
    value, observation, authorization = validate_decision(raw)
    if (Path(observation.data_root).resolve() != root or authorization.operation_id != operation_id
            or authorization.target_version != target_version or authorization.target_artifact_digest != target_digest
            or authorization.target_source_revision != target_source_revision):
        raise LegacyInstallationAdoptionError("legacy adoption record is not bound to this update")
    return value


def bound_baseline(data_root: Path, *, operation_id: str, target_version: str,
                   target_digest: str, target_source_revision: str) -> LegacyInstallationObservation:
    """Reopen only the exact, durable adoption decision bound to this update."""
    value = bound_decision(
        data_root, operation_id=operation_id, target_version=target_version,
        target_digest=target_digest, target_source_revision=target_source_revision,
    )
    _canonical, observation, _authorization = validate_decision(value)
    return observation


def revalidate_bound_baseline(
    data_root: Path,
    *,
    operation_id: str,
    target_version: str,
    target_digest: str,
    target_source_revision: str,
    runner: Callable[..., object] | None = None,
    service_home: Path | None = None,
    allowed_target_interpreter: str | Path | None = None,
) -> dict[str, object]:
    """Recheck authority, service selection, wheel and installed old bytes.

    The caller owns the installation session lock.  ``allowed_target_interpreter``
    is used only in the MIGRATED activation acknowledgement window: the old
    package must still match its wheel, while the service may already select
    the exact admitted candidate.
    """
    root = Path(data_root).expanduser().resolve()
    try:
        operational_installation_record.load(root)
    except operational_installation_record.OperationalInstallationRecordNotFound:
        pass
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise LegacyInstallationAdoptionError("operational installation registration conflicts with legacy adoption") from error
    else:
        raise LegacyInstallationAdoptionError("operational installation registration conflicts with legacy adoption")
    decision = bound_decision(
        root, operation_id=operation_id, target_version=target_version,
        target_digest=target_digest, target_source_revision=target_source_revision,
    )
    _canonical, observation, authorization = validate_decision(decision)
    local_owner_authority(authorization, observation)
    if observation.service_label != server_service.LABEL:
        raise LegacyInstallationAdoptionError("legacy adoption service identity is invalid")
    selected = server_service.configured_interpreter(root, home=service_home)
    old = operational_installation.launcher(observation.interpreter)
    allowed = {old}
    if allowed_target_interpreter is not None:
        allowed.add(operational_installation.launcher(allowed_target_interpreter))
    if selected is None or operational_installation.launcher(selected) not in allowed:
        raise LegacyInstallationAdoptionError("selected service changed after legacy adoption")
    try:
        installation = operational_installation.resolve(root, interpreter=old)
    except operational_installation.OperationalInstallationError as error:
        raise LegacyInstallationAdoptionError("observed legacy installation changed after adoption") from error
    allowed_versions = {None, observation.version}
    if allowed_target_interpreter is not None:
        # The exact-target migration invokes target initialize() before the
        # service switch and therefore may already have advanced the product
        # version in server.json.  This is not evidence that the old package
        # changed; validate those bytes against their observed version below.
        allowed_versions.add(target_version)
    if installation.configured_version not in allowed_versions:
        raise LegacyInstallationAdoptionError("configured product changed after legacy adoption")
    legacy_installation = replace(installation, configured_version=observation.version)
    current = inspect(
        installation=legacy_installation, service_label=observation.service_label,
        service_interpreter=old, preserved_wheel=Path(observation.wheel), runner=runner,
    )
    if current != observation:
        raise LegacyInstallationAdoptionError("observed legacy installation changed after adoption")
    return decision
