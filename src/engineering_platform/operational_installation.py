"""Read-only identity checks for the one selected EP operational runtime.

PATH is only a diagnostic input.  The selected runtime is the interpreter
stored by the EP-owned service record, normalized to its real filesystem path.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from importlib import metadata as importlib_metadata
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Iterable, Mapping

from . import operational_installation_record


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class OperationalInstallationError(ValueError):
    """An operational installation cannot be identified safely."""


@dataclass(frozen=True)
class OperationalInstallation:
    interpreter: str
    data_root: str
    instance_id: str
    configured_version: str | None
    runtime_pid: int | None
    path_candidates: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return asdict(self)


def normalized(path: str | Path) -> Path:
    """Normalize a durable data path, including symlinks, before comparison."""
    return Path(path).expanduser().resolve(strict=False)


def launcher(path: str | Path) -> Path:
    """Return an absolute interpreter *invocation* path without resolving it.

    A virtual environment's ``bin/python`` is normally a symlink to a shared
    base interpreter.  Resolving it would make two different EP venvs appear
    to be one runtime and would make the base interpreter look like it owns
    the package.  The lexical absolute launcher is therefore the operational
    identity; data roots continue to use :func:`normalized`.
    """
    return Path(path).expanduser().absolute()


def _object(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationalInstallationError(f"operational {label} is unreadable") from error
    if not isinstance(value, dict):
        raise OperationalInstallationError(f"operational {label} is invalid")
    return value


def resolve(data_root: Path, *, interpreter: str | Path,
            path_candidates: Iterable[str | Path] = ()) -> OperationalInstallation:
    """Resolve only the EP-owned service interpreter and data-root identity."""
    root = normalized(data_root)
    config, identity = _object(root / "server.json", "server configuration"), _object(root / "runtime-identity.json", "identity")
    # ``server.json.version`` is the server-configuration schema revision, not
    # a package/product version.  Only an explicit product_version may bind a
    # product claim; otherwise the selected interpreter metadata is authority.
    version, instance_id = config.get("product_version"), identity.get("instance_id")
    if version is not None and not isinstance(version, str):
        raise OperationalInstallationError("operational product version is invalid")
    if not isinstance(instance_id, str) or not instance_id:
        raise OperationalInstallationError("operational configuration lacks instance identity")
    runtime_path = root / "runtime.json"
    runtime = _object(runtime_path, "runtime") if runtime_path.exists() else {}
    pid = runtime.get("pid")
    if pid is not None and (not isinstance(pid, int) or pid <= 0):
        raise OperationalInstallationError("operational runtime PID is invalid")
    claimed = runtime.get("instance_id")
    if claimed is not None and claimed != instance_id:
        raise OperationalInstallationError("operational runtime belongs to a different instance")
    selected = launcher(interpreter)
    if not selected.is_file():
        raise OperationalInstallationError("selected operational interpreter is unavailable")
    candidates = tuple(str(launcher(candidate)) for candidate in path_candidates)
    return OperationalInstallation(str(selected), str(root), instance_id, version, pid, candidates)


def record_status(installation: OperationalInstallation) -> Mapping[str, object]:
    """Return the EP-owned record state, rejecting a conflicting record.

    A missing record is reported explicitly so an incidental PATH package can
    never be treated as the registered operational installation.  This helper
    is deliberately read-only: registration remains an installation-lifecycle
    responsibility.
    """
    root = Path(installation.data_root)
    path = root / operational_installation_record.FILENAME
    if not path.exists():
        return {"state": "UNREGISTERED", "path": str(path)}
    try:
        record = operational_installation_record.load(root)
    except operational_installation_record.OperationalInstallationRecordError as error:
        raise OperationalInstallationError("operational installation record is unreadable") from error
    if (record.get("installation_id") != installation.instance_id
            or (installation.configured_version is not None and record.get("version") != installation.configured_version)
            or launcher(str(record.get("interpreter", ""))) != launcher(installation.interpreter)):
        raise OperationalInstallationError("operational installation record does not match selected runtime")
    return {
        "state": "REGISTERED",
        "path": str(path),
        "installation_id": record["installation_id"],
        "version": record["version"],
        "channel": record["channel"],
        "artifact_digest": record["artifact_digest"],
        "source_revision": record["source_revision"],
        "desired_state": record["desired_state"],
        "observed_state": record["observed_state"],
    }


def validate_health(installation: OperationalInstallation, response: Mapping[str, object]) -> None:
    """Reject an otherwise healthy response from a different EP instance."""
    if response.get("service") != "engineering-platform-server" or response.get("instance_id") != installation.instance_id:
        raise OperationalInstallationError("health response does not identify the selected operational instance")
    if response.get("healthy") is not True:
        raise OperationalInstallationError("selected operational instance is not healthy")
    if (installation.configured_version is not None
            and response.get("product_version") != installation.configured_version):
        raise OperationalInstallationError("health response does not identify the selected operational release")


def package_identity(interpreter: str | Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> Mapping[str, str]:
    """Ask the exact selected interpreter, never the caller's PATH, for EP metadata."""
    selected = launcher(interpreter)
    program = (
        "import importlib.metadata,json,pathlib,sys;d=importlib.metadata.distribution('engineering-platform');"
        "print(json.dumps({'interpreter':sys.executable,'version':d.version,'metadata':str(d._path),"
        "'package':str(pathlib.Path(d.locate_file('engineering_platform')).resolve())}))"
    )
    result = runner((str(selected), "-I", "-c", program), capture_output=True, text=True, check=False)
    if result.returncode:
        raise OperationalInstallationError("selected interpreter does not provide an EP package identity")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise OperationalInstallationError("selected interpreter returned invalid EP package identity") from error
    if not isinstance(value, dict) or set(value) != {"interpreter", "version", "metadata", "package"} or not all(isinstance(item, str) and item for item in value.values()):
        raise OperationalInstallationError("selected interpreter returned incomplete EP package identity")
    return value


def validate_package_identity(installation: OperationalInstallation, identity: Mapping[str, str]) -> None:
    """Ensure the queried package is the configured service runtime, not PATH."""
    if set(identity) != {"interpreter", "version", "metadata", "package"}:
        raise OperationalInstallationError("selected interpreter returned incomplete EP package identity")
    if launcher(identity["interpreter"]) != launcher(installation.interpreter):
        raise OperationalInstallationError("package identity belongs to a different interpreter")
    if installation.configured_version is not None and identity["version"] != installation.configured_version:
        raise OperationalInstallationError("package version does not match selected operational runtime")


def validate_registered_package_identity(record: Mapping[str, object], identity: Mapping[str, str]) -> None:
    """Bind a registered release claim to the exact interpreter's package.

    Configuration versions are optional because ``server.json`` has its own
    schema revision. A registered operational installation always names an EP
    release, so it must agree with metadata from the selected interpreter.
    """
    if record.get("state") != "REGISTERED":
        return
    version = record.get("version")
    if not isinstance(version, str) or not version:
        raise OperationalInstallationError("registered operational installation lacks package version")
    if identity.get("version") != version:
        raise OperationalInstallationError("registered installation version does not match selected package")


def _direct_artifact_identity(metadata_path: Path) -> dict[str, object]:
    """Read optional PEP 610 wheel provenance without inventing a digest.

    An installed wheel is not reversible into its original wheel bytes.  Pip
    can, however, retain the exact archive hash in ``direct_url.json`` for a
    direct wheel installation.  Treat every other install form as explicitly
    unavailable rather than copying the registered digest into a live claim.
    """
    try:
        value = json.loads((metadata_path / "direct_url.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "UNAVAILABLE"}
    archive = value.get("archive_info") if isinstance(value, dict) else None
    if not isinstance(archive, dict):
        return {"state": "UNAVAILABLE"}
    digest = archive.get("hash")
    if isinstance(digest, str) and digest.startswith("sha256="):
        digest = "sha256:" + digest.removeprefix("sha256=")
    if not isinstance(digest, str):
        hashes = archive.get("hashes")
        candidate = hashes.get("sha256") if isinstance(hashes, dict) else None
        digest = "sha256:" + candidate if isinstance(candidate, str) else None
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        return {"state": "UNAVAILABLE"}
    return {"state": "OBSERVED", "digest": digest}


def live_runtime_identity(*, package: str | Path, product_version: str) -> dict[str, object]:
    """Describe the actual process that generated a server health response.

    This is intentionally an in-process observation.  It never looks at
    ``PATH`` or an arbitrary checkout.  Installed distribution metadata and
    its optional wheel provenance remain separate from product-version data
    embedded in the running package.
    """
    if not isinstance(product_version, str) or not product_version:
        raise OperationalInstallationError("live operational product version is invalid")
    identity: dict[str, object] = {
        "interpreter": str(launcher(sys.executable)),
        "executable": str(launcher(sys.executable)),
        "package": str(normalized(package)),
        "package_version": None,
        "metadata": None,
        "artifact": {"state": "UNAVAILABLE"},
    }
    try:
        distribution = importlib_metadata.distribution("engineering-platform")
        metadata_path = normalized(Path(distribution._path))
    except (importlib_metadata.PackageNotFoundError, OSError, ValueError):
        # Distribution metadata is supplemental to the process facts above.
        # A malformed or unreadable dist-info directory must not make the
        # generic health endpoint claim an invented artifact identity.
        return identity
    identity["package_version"] = distribution.version
    identity["metadata"] = str(metadata_path)
    identity["artifact"] = _direct_artifact_identity(metadata_path)
    return identity


def _observed_namespace(value: Mapping[str, object] | None, namespace: str) -> dict[str, object]:
    """Keep source, wheel, installation and live observations disjoint."""
    if value is None:
        return {"state": "UNOBSERVED"}
    if not isinstance(value, Mapping):
        raise OperationalInstallationError(f"{namespace} observation is invalid")
    return {"state": "OBSERVED", "identity": dict(value)}


def _required_string(value: Mapping[str, object], key: str, label: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise OperationalInstallationError(f"{label} is incomplete")
    return candidate


def qualify_runtime_response(
    installation: OperationalInstallation,
    *,
    record: Mapping[str, object],
    package: Mapping[str, str],
    response: Mapping[str, object],
    source_observation: Mapping[str, object] | None = None,
    wheel_observation: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """Fail closed unless the real health response names the selected release.

    ``source_observation`` and ``wheel_observation`` are intentionally
    optional and never used as a substitute for installed or live facts.  A
    caller can present all four namespaces in one diagnostic without allowing
    an installation observation to overwrite a source or wheel observation.
    """
    if record.get("state") != "REGISTERED":
        raise OperationalInstallationError("operational installation is not registered")
    record_version = _required_string(record, "version", "registered operational installation")
    record_digest = _required_string(record, "artifact_digest", "registered operational installation")
    if _DIGEST.fullmatch(record_digest) is None:
        raise OperationalInstallationError("registered operational artifact digest is invalid")
    record_instance = _required_string(record, "installation_id", "registered operational installation")
    if record_instance != installation.instance_id:
        raise OperationalInstallationError("registered operational installation does not match selected instance")

    package_value = dict(package)
    validate_package_identity(installation, package_value)
    validate_registered_package_identity(record, package_value)
    package_interpreter = _required_string(package_value, "interpreter", "selected package identity")
    package_path = _required_string(package_value, "package", "selected package identity")
    metadata_path = _required_string(package_value, "metadata", "selected package identity")
    package_version = _required_string(package_value, "version", "selected package identity")
    if package_version != record_version:
        raise OperationalInstallationError("registered installation version does not match selected package")

    validate_health(installation, response)
    if response.get("product_version") != record_version:
        raise OperationalInstallationError("health response does not identify the registered operational release")
    live = response.get("runtime_identity")
    if not isinstance(live, Mapping):
        raise OperationalInstallationError("health response lacks live runtime identity")
    live_interpreter = _required_string(live, "interpreter", "health response runtime identity")
    live_executable = _required_string(live, "executable", "health response runtime identity")
    live_package = _required_string(live, "package", "health response runtime identity")
    live_version = _required_string(live, "package_version", "health response runtime identity")
    live_metadata = _required_string(live, "metadata", "health response runtime identity")
    if launcher(live_interpreter) != launcher(installation.interpreter):
        raise OperationalInstallationError("health response interpreter differs from selected operational runtime")
    if launcher(live_executable) != launcher(package_interpreter):
        raise OperationalInstallationError("health response executable differs from selected package")
    if normalized(live_package) != normalized(package_path):
        raise OperationalInstallationError("health response package differs from selected package")
    if normalized(live_metadata) != normalized(metadata_path):
        raise OperationalInstallationError("health response metadata differs from selected package")
    if live_version != package_version or live_version != record_version:
        raise OperationalInstallationError("health response package version differs from selected release")

    artifact = live.get("artifact")
    if not isinstance(artifact, Mapping):
        raise OperationalInstallationError("health response artifact identity is invalid")
    artifact_state = artifact.get("state")
    if artifact_state == "OBSERVED" and set(artifact) == {"state", "digest"}:
        live_digest = _required_string(artifact, "digest", "health response artifact identity")
        if _DIGEST.fullmatch(live_digest) is None:
            raise OperationalInstallationError("health response artifact digest is invalid")
        if live_digest != record_digest:
            raise OperationalInstallationError("health response artifact digest differs from registered installation")
        artifact_observation: Mapping[str, object] = {"state": "PASS", "digest": live_digest}
    elif artifact_state == "UNAVAILABLE" and set(artifact) == {"state"}:
        artifact_observation = {"state": "UNAVAILABLE"}
    else:
        raise OperationalInstallationError("health response artifact identity is invalid")

    return {
        "qualification": "PASS",
        "source": _observed_namespace(source_observation, "source"),
        "wheel": _observed_namespace(wheel_observation, "wheel"),
        "installation": {
            "record": {
                "installation_id": record_instance,
                "version": record_version,
                "artifact_digest": record_digest,
                "source_revision": _required_string(record, "source_revision", "registered operational installation"),
            },
            "package": {
                "interpreter": package_interpreter,
                "package": package_path,
                "metadata": metadata_path,
                "version": package_version,
            },
        },
        "live": {
            "service": _required_string(response, "service", "health response"),
            "instance_id": _required_string(response, "instance_id", "health response"),
            "product_version": _required_string(response, "product_version", "health response"),
            "runtime_identity": {
                "interpreter": live_interpreter,
                "executable": live_executable,
                "package": live_package,
                "package_version": live_version,
                "metadata": live_metadata,
                "artifact": dict(artifact),
            },
            "artifact_verification": dict(artifact_observation),
        },
    }


def inventory(installation: OperationalInstallation, *, service_references: Mapping[str, str | Path],
              candidates: Iterable[str | Path], runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> Mapping[str, object]:
    """Classify explicit candidate interpreters without selecting or mutating one.

    This intentionally inventories only paths supplied by the owning service
    contract/operator.  It does not pretend to enumerate every account or
    disk location, and it never promotes a PATH candidate to operational use.
    """
    selected = launcher(installation.interpreter)
    references: dict[str, Path] = {}
    for label, value in service_references.items():
        if not isinstance(label, str) or not label or not isinstance(value, (str, Path)):
            raise OperationalInstallationError("operational service reference is invalid")
        candidate = launcher(value)
        if not candidate.is_absolute():
            raise OperationalInstallationError("operational service reference must be absolute")
        references[label] = candidate
    paths = {selected, *(launcher(candidate) for candidate in candidates), *references.values()}
    entries: list[dict[str, object]] = []
    for interpreter in sorted(paths, key=str):
        labels = sorted(label for label, value in references.items() if value == interpreter)
        try:
            identity = dict(package_identity(interpreter, runner=runner))
            status = "SELECTED" if interpreter == selected else ("CONFLICTING_SERVICE_REFERENCE" if labels else "PATH_OR_EXPLICIT_CANDIDATE")
            entries.append({"interpreter": str(interpreter), "status": status, "service_references": labels, "identity": identity})
        except OperationalInstallationError as error:
            entries.append({"interpreter": str(interpreter), "status": "UNAVAILABLE_OR_NOT_EP", "service_references": labels, "diagnostic": str(error)})
    conflicts = [entry for entry in entries if entry["status"] == "CONFLICTING_SERVICE_REFERENCE"]
    # This call intentionally has no authority to enumerate other macOS
    # accounts, system LaunchDaemons, or arbitrary disks.  Absence of a
    # conflict among explicit paths is useful evidence, but never proof of the
    # machine-wide invariant.
    scope = {
        "required": "MACOS_MACHINE",
        "observed": "CURRENT_OS_USER_EXPLICIT_REFERENCES_ONLY",
        "status": "INCOMPLETE",
    }
    return {"selected_interpreter": str(selected), "coverage": "EXPLICIT_PATHS_ONLY", "scope": scope,
            "entries": entries, "conflicting_service_references": conflicts,
            "single_operational_installation": False,
            "single_operational_installation_status": "UNVERIFIED_SCOPE" if not conflicts else "CONFLICTS_DETECTED"}
