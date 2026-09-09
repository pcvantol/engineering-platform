"""Prepare an exact, non-operational EP runtime for one update plan.

This is deliberately narrower than an installer.  It copies the immutable
wheel named by an already prepared plan into that plan's operation directory
and builds a candidate virtual environment there.  It does not stop or start
a service, migrate CENTRAL, replace an operational-installation record, or
invoke the update executor.

Keeping the candidate beneath the operation root matters: it gives a reboot
or a lost caller response one deterministic, identity-bound runtime to inspect
without selecting a wheel, venv, or Python from PATH.  The candidate remains
non-operational until a later EP-owned provisioner explicitly activates it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable, Mapping

from . import installation_update_plan, operational_installation
from .installation_update_plan import InstallationUpdatePlan
from .operational_installation_lock import (
    OperationalInstallationLock,
    OperationalInstallationLockError,
)


_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_MARKER_FILENAME = "candidate-runtime.json"
_CANDIDATE_DIRECTORY = "candidate-venv"


class InstallationUpdatePreparationError(ValueError):
    """The exact update candidate cannot safely be prepared or reused."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PreparedUpdateCandidate:
    """Identity of a staged, non-operational target runtime.

    Every path is owned by the immutable update operation.  None is a service
    selection or an authorization to make the candidate operational.
    """

    operation_id: str
    installation_id: str
    operation_root: str
    staged_artifact: str
    artifact_digest: str
    candidate_venv: str
    interpreter: str
    pip_cache: str
    package: Mapping[str, str]

    def payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _OperationPaths:
    root: Path
    operation: Path
    download: Path
    staged_artifact: Path
    candidate: Path
    interpreter: Path
    pip_cache: Path
    marker: Path


def _digest(path: Path) -> str:
    """Hash a regular, non-symlink file without trusting its filename."""
    if path.is_symlink() or not path.is_file():
        raise InstallationUpdatePreparationError("operation artifact is not a regular file")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise InstallationUpdatePreparationError("operation artifact is unreadable") from error
    return "sha256:" + digest.hexdigest()


def _contained(root: Path, candidate: Path, label: str) -> None:
    """Reject a path that escapes an EP-owned normalized data root."""
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise InstallationUpdatePreparationError(f"{label} escapes the update operation root") from error


def _directory(path: Path, *, root: Path, label: str) -> None:
    """Create one owned directory without following a pre-existing symlink."""
    _contained(root, path, label)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise InstallationUpdatePreparationError(f"{label} is not an owned directory")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise InstallationUpdatePreparationError(f"{label} could not be created") from error
    if path.is_symlink() or not path.is_dir():
        raise InstallationUpdatePreparationError(f"{label} is not an owned directory")
    _contained(root, path, label)


def _paths(plan: InstallationUpdatePlan) -> _OperationPaths:
    """Derive every mutable location from the pre-existing plan identity."""
    if not isinstance(plan, InstallationUpdatePlan):
        raise InstallationUpdatePreparationError("update plan is invalid")
    if _OPERATION.fullmatch(plan.operation_id) is None:
        raise InstallationUpdatePreparationError("update operation ID is invalid")
    root = Path(plan.data_root).expanduser().resolve(strict=False)
    operation = root / "operations" / plan.operation_id
    expected_cleanup = tuple(str(operation / name) for name in ("build", "download", "pip-cache"))
    if plan.cleanup_targets != expected_cleanup:
        raise InstallationUpdatePreparationError("update plan does not own its cleanup paths")
    download = operation / "download"
    # The plan's operation directory and durable marker bind the digest.  Keep
    # the staged filename itself PEP 427-compatible so isolated ``pip`` can
    # install it without relying on an original temporary filename.
    staged = download / f"engineering_platform-{plan.target_version}-py3-none-any.whl"
    candidate = operation / _CANDIDATE_DIRECTORY
    paths = _OperationPaths(
        root=root,
        operation=operation,
        download=download,
        staged_artifact=staged,
        candidate=candidate,
        interpreter=candidate / "bin" / "python",
        pip_cache=operation / "pip-cache",
        marker=operation / _MARKER_FILENAME,
    )
    for label, path in (
        ("update operation", paths.operation),
        ("operation download directory", paths.download),
        ("staged operation artifact", paths.staged_artifact),
        ("candidate runtime", paths.candidate),
        ("operation pip cache", paths.pip_cache),
        ("candidate runtime marker", paths.marker),
    ):
        _contained(root, path, label)
    return paths


def _stage(plan: InstallationUpdatePlan, paths: _OperationPaths) -> Path:
    """Copy exact bytes once into the operation-owned download directory.

    A verified staged wheel is the durable resume source.  Once it exists the
    mutable caller-supplied wheel is intentionally not reread; otherwise a
    registry retry or a changed temporary path could make recovery ambiguous.
    """
    staged = paths.staged_artifact
    if staged.exists() or staged.is_symlink():
        if _digest(staged) != plan.target_digest:
            raise InstallationUpdatePreparationError("staged operation artifact differs from the exact update plan")
        return staged
    try:
        source = Path(installation_update_plan.verify_exact_artifact(plan)["path"])
    except installation_update_plan.InstallationUpdatePlanError as error:
        raise InstallationUpdatePreparationError("source artifact changed before operation staging") from error
    _directory(paths.operation, root=paths.root, label="update operation")
    _directory(paths.download, root=paths.root, label="operation download directory")
    descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", suffix=".whl", dir=paths.download)
    temporary_path = Path(temporary)
    try:
        digest = hashlib.sha256()
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
            while chunk := input_stream.read(1024 * 1024):
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        observed = "sha256:" + digest.hexdigest()
        if observed != plan.target_digest:
            raise InstallationUpdatePreparationError("source artifact changed during operation staging")
        os.chmod(temporary_path, 0o600)
        # A concurrent caller must not replace an already staged identity.  A
        # hard link is an atomic no-overwrite publication in this directory.
        try:
            os.link(temporary_path, staged)
        except FileExistsError:
            if _digest(staged) != plan.target_digest:
                raise InstallationUpdatePreparationError("staged operation artifact differs from the exact update plan")
        finally:
            temporary_path.unlink(missing_ok=True)
    except OSError as error:
        temporary_path.unlink(missing_ok=True)
        raise InstallationUpdatePreparationError("exact operation artifact could not be staged") from error
    if _digest(staged) != plan.target_digest:
        raise InstallationUpdatePreparationError("staged operation artifact differs from the exact update plan")
    return staged


def _marker(plan: InstallationUpdatePlan, paths: _OperationPaths, staged: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation_id": plan.operation_id,
        "installation_id": plan.installation_id,
        "target_version": plan.target_version,
        "target_digest": plan.target_digest,
        "target_source_revision": plan.target_source_revision,
        "staged_artifact": str(staged),
        "candidate_venv": str(paths.candidate),
    }


def _write_marker(path: Path, expected: Mapping[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".candidate-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _read_marker(plan: InstallationUpdatePlan, paths: _OperationPaths, staged: Path) -> None:
    """Require the existing marker to name exactly this candidate identity."""
    expected = _marker(plan, paths, staged)
    if paths.marker.is_symlink() or not paths.marker.is_file():
        raise InstallationUpdatePreparationError("candidate runtime marker is invalid")
    try:
        observed = json.loads(paths.marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallationUpdatePreparationError("candidate runtime marker is unreadable") from error
    if observed != expected:
        raise InstallationUpdatePreparationError("candidate runtime does not match the exact update plan")


def _ensure_marker(plan: InstallationUpdatePlan, paths: _OperationPaths, staged: Path) -> None:
    """Create the identity marker only before the candidate exists."""
    expected = _marker(plan, paths, staged)
    if paths.marker.exists() or paths.marker.is_symlink():
        _read_marker(plan, paths, staged)
        return
    if paths.candidate.exists() or paths.candidate.is_symlink():
        raise InstallationUpdatePreparationError("candidate runtime exists without operation identity")
    try:
        _write_marker(paths.marker, expected)
    except OSError as error:
        raise InstallationUpdatePreparationError("candidate runtime marker could not be retained") from error


def _verified_candidate(
    plan: InstallationUpdatePlan,
    paths: _OperationPaths,
    *,
    candidate: PreparedUpdateCandidate | None,
    runner: Runner,
) -> PreparedUpdateCandidate:
    """Re-read every durable candidate fact without recreating it.

    A caller's original wheel is deliberately absent from this read-only
    recovery boundary.  The stage, marker, and candidate interpreter are the
    operation-owned facts that must survive a lost response or reboot.
    """
    staged = paths.staged_artifact
    if _digest(staged) != plan.target_digest:
        raise InstallationUpdatePreparationError("staged operation artifact differs from the exact update plan")
    _read_marker(plan, paths, staged)
    # A venv launcher is normally a symlink to its base interpreter, which is
    # intentionally outside the candidate directory.  The *venv directory*
    # and its ``bin`` directory must be EP-owned real directories; the final
    # launcher itself may be that expected venv symlink.  Resolving the
    # launcher as a containment check would incorrectly reject every normal
    # venv on recovery and tempt a caller to select a base interpreter.
    launcher_directory = paths.interpreter.parent
    if (paths.candidate.is_symlink() or not paths.candidate.is_dir()
            or launcher_directory.is_symlink() or not launcher_directory.is_dir()
            or not paths.interpreter.is_file()):
        raise InstallationUpdatePreparationError("candidate runtime is unavailable")
    try:
        identity = operational_installation.package_identity(paths.interpreter, runner=runner)
    except operational_installation.OperationalInstallationError as error:
        raise InstallationUpdatePreparationError("candidate interpreter does not provide the planned EP package") from error
    observed = PreparedUpdateCandidate(
        operation_id=plan.operation_id,
        installation_id=plan.installation_id,
        operation_root=str(paths.operation),
        staged_artifact=str(staged),
        artifact_digest=plan.target_digest,
        candidate_venv=str(paths.candidate),
        interpreter=str(paths.interpreter),
        pip_cache=str(paths.pip_cache),
        package=_package_in_candidate(paths, identity, plan),
    )
    if candidate is not None:
        if not isinstance(candidate, PreparedUpdateCandidate) or candidate.payload() != observed.payload():
            raise InstallationUpdatePreparationError("prepared candidate does not match the exact update plan")
    return observed


def verify_prepared_candidate(
    plan: InstallationUpdatePlan,
    *,
    candidate: PreparedUpdateCandidate | None = None,
    runner: Runner = subprocess.run,
) -> PreparedUpdateCandidate:
    """Verify a previously prepared candidate without touching its source wheel.

    This is the reboot/resume readback for OI-4a.  It does not acquire a new
    lock, create a virtual environment, install a package, or replace a
    runtime.  A later installation session supplies the existing lifecycle
    lock while it persists or consumes this result.
    """
    paths = _paths(plan)
    return _verified_candidate(plan, paths, candidate=candidate, runner=runner)


def staged_execution_plan(
    plan: InstallationUpdatePlan,
    *,
    candidate: PreparedUpdateCandidate | None = None,
    runner: Runner = subprocess.run,
) -> InstallationUpdatePlan:
    """Rebind an exact plan to its durable operation-owned staged wheel.

    ``prepare_candidate`` has already copied and hashed the supplied wheel.
    Replanning against that staged copy makes the later executor independent
    of a temporary download path while rechecking the currently registered
    installation.  All non-artifact plan facts must remain unchanged.
    """
    prepared = verify_prepared_candidate(plan, candidate=candidate, runner=runner)
    try:
        rebound = installation_update_plan.prepare(
            Path(plan.data_root),
            operation_id=plan.operation_id,
            artifact=Path(prepared.staged_artifact),
            target_version=plan.target_version,
            target_digest=plan.target_digest,
            target_source_revision=plan.target_source_revision,
        )
    except installation_update_plan.InstallationUpdatePlanError as error:
        raise InstallationUpdatePreparationError("staged candidate cannot be rebound to the registered installation") from error
    unchanged = (
        "operation_id", "installation_id", "data_root", "current_version",
        "current_digest", "target_version", "target_digest",
        "target_source_revision", "cleanup_targets", "steps",
    )
    if any(getattr(rebound, field) != getattr(plan, field) for field in unchanged):
        raise InstallationUpdatePreparationError("registered installation changed before staged candidate binding")
    # ``prepare`` re-hashes the staged path.  Preserve this explicit check at
    # the boundary so a future change cannot accidentally make rebinding trust
    # only the caller's old source path.
    try:
        installation_update_plan.verify_exact_artifact(rebound)
    except installation_update_plan.InstallationUpdatePlanError as error:
        raise InstallationUpdatePreparationError("staged operation artifact differs from the exact update plan") from error
    return rebound


def _base_interpreter(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise InstallationUpdatePreparationError("candidate venv builder must be an absolute executable interpreter")
    # Preserve a virtual-environment launcher spelling if the explicit builder
    # itself is a venv; resolving it would silently select its base Python.
    return candidate.absolute()


def _run(runner: Runner, command: tuple[str, ...], *, environment: Mapping[str, str], label: str) -> None:
    try:
        result = runner(command, capture_output=True, text=True, check=False, env=dict(environment))
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallationUpdatePreparationError(f"{label} could not be started") from error
    if result.returncode:
        raise InstallationUpdatePreparationError(f"{label} failed")


def _venv_environment() -> dict[str, str]:
    return {
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }


def _pip_environment(cache: Path) -> dict[str, str]:
    return {
        **_venv_environment(),
        "PIP_CACHE_DIR": str(cache),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
    }


def _create_or_recover_venv(paths: _OperationPaths, *, builder: Path, runner: Runner) -> None:
    if paths.candidate.is_symlink() or (paths.candidate.exists() and not paths.candidate.is_dir()):
        raise InstallationUpdatePreparationError("candidate runtime is not an owned directory")
    incomplete = paths.candidate.exists() and not paths.interpreter.is_file()
    command = (str(builder), "-I", "-m", "venv", *( ("--clear",) if incomplete else () ), str(paths.candidate))
    if not paths.candidate.exists() or incomplete:
        _run(runner, command, environment=_venv_environment(), label="candidate venv creation")
    launcher_directory = paths.interpreter.parent
    if launcher_directory.is_symlink() or not launcher_directory.is_dir() or not paths.interpreter.is_file():
        raise InstallationUpdatePreparationError("candidate venv did not provide its interpreter")


def _package_in_candidate(paths: _OperationPaths, identity: Mapping[str, str], plan: InstallationUpdatePlan) -> dict[str, str]:
    try:
        observed = dict(identity)
        if set(observed) != {"interpreter", "version", "metadata", "package"}:
            raise ValueError
        if operational_installation.launcher(observed["interpreter"]) != operational_installation.launcher(paths.interpreter):
            raise ValueError
        if observed["version"] != plan.target_version:
            raise InstallationUpdatePreparationError("candidate interpreter does not provide the planned EP version")
        for label in ("metadata", "package"):
            value = Path(observed[label])
            if not value.is_absolute() or not value.is_dir():
                raise ValueError
            _contained(paths.candidate.resolve(strict=False), value, f"candidate {label}")
    except InstallationUpdatePreparationError:
        raise
    except (KeyError, TypeError, ValueError):
        raise InstallationUpdatePreparationError("candidate interpreter package identity is invalid") from None
    return observed


def _install_or_verify_candidate(
    plan: InstallationUpdatePlan,
    paths: _OperationPaths,
    *,
    staged: Path,
    runner: Runner,
) -> dict[str, str]:
    try:
        identity = operational_installation.package_identity(paths.interpreter, runner=runner)
    except operational_installation.OperationalInstallationError:
        identity = None
    if identity is not None:
        # A candidate that already identifies itself as the target is a safe
        # reboot/retry resume.  A different installed package fails closed;
        # it is never overwritten in place.
        return _package_in_candidate(paths, identity, plan)
    _directory(paths.pip_cache, root=paths.root, label="operation pip cache")
    command = (
        str(paths.interpreter), "-I", "-m", "pip", "install",
        "--isolated", "--no-deps", "--no-index", "--no-input",
        "--disable-pip-version-check", "--cache-dir", str(paths.pip_cache), str(staged),
    )
    _run(runner, command, environment=_pip_environment(paths.pip_cache), label="exact candidate wheel installation")
    try:
        identity = operational_installation.package_identity(paths.interpreter, runner=runner)
    except operational_installation.OperationalInstallationError as error:
        raise InstallationUpdatePreparationError("candidate interpreter does not provide the planned EP package") from error
    return _package_in_candidate(paths, identity, plan)


def prepare_candidate(
    plan: InstallationUpdatePlan,
    *,
    venv_builder: str | Path,
    runner: Runner = subprocess.run,
) -> PreparedUpdateCandidate:
    """Stage and prove one exact candidate runtime under the existing plan.

    This is the only side effect in OI-4a, and every created path is contained
    beneath ``data_root/operations/<operation_id>``.  It intentionally does
    not create an update journal, alter an installation record, contact a
    service, run a migration, or call the update executor.
    """
    paths = _paths(plan)
    builder = _base_interpreter(venv_builder)
    lock = OperationalInstallationLock(paths.root)
    try:
        lock.acquire(plan.operation_id)
    except OperationalInstallationLockError as error:
        raise InstallationUpdatePreparationError("another operational update owns the installation lock") from error
    try:
        staged = _stage(plan, paths)
        _ensure_marker(plan, paths, staged)
        _create_or_recover_venv(paths, builder=builder, runner=runner)
        package = _install_or_verify_candidate(plan, paths, staged=staged, runner=runner)
        return PreparedUpdateCandidate(
            operation_id=plan.operation_id,
            installation_id=plan.installation_id,
            operation_root=str(paths.operation),
            staged_artifact=str(staged),
            artifact_digest=plan.target_digest,
            candidate_venv=str(paths.candidate),
            interpreter=str(paths.interpreter),
            pip_cache=str(paths.pip_cache),
            package=package,
        )
    finally:
        lock.release(plan.operation_id)
