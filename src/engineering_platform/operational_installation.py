"""Read-only identity checks for the one selected EP operational runtime.

PATH is only a diagnostic input.  The selected runtime is the interpreter
stored by the EP-owned service record, normalized to its real filesystem path.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from pathlib import Path
import subprocess
from typing import Callable, Iterable, Mapping

from . import operational_installation_record


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
    """Normalize symlinks and whitespace-bearing paths before comparison."""
    return Path(path).expanduser().resolve(strict=False)


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
    selected = normalized(interpreter)
    if not selected.is_file():
        raise OperationalInstallationError("selected operational interpreter is unavailable")
    candidates = tuple(str(normalized(candidate)) for candidate in path_candidates)
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
            or normalized(str(record.get("interpreter", ""))) != normalized(installation.interpreter)):
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


def package_identity(interpreter: str | Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> Mapping[str, str]:
    """Ask the exact selected interpreter, never the caller's PATH, for EP metadata."""
    selected = normalized(interpreter)
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
    if normalized(identity["interpreter"]) != normalized(installation.interpreter):
        raise OperationalInstallationError("package identity belongs to a different interpreter")
    if installation.configured_version is not None and identity["version"] != installation.configured_version:
        raise OperationalInstallationError("package version does not match selected operational runtime")


def inventory(installation: OperationalInstallation, *, service_references: Mapping[str, str | Path],
              candidates: Iterable[str | Path], runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> Mapping[str, object]:
    """Classify explicit candidate interpreters without selecting or mutating one.

    This intentionally inventories only paths supplied by the owning service
    contract/operator.  It does not pretend to enumerate every account or
    disk location, and it never promotes a PATH candidate to operational use.
    """
    selected = normalized(installation.interpreter)
    references: dict[str, Path] = {}
    for label, value in service_references.items():
        if not isinstance(label, str) or not label or not isinstance(value, (str, Path)):
            raise OperationalInstallationError("operational service reference is invalid")
        candidate = normalized(value)
        if not candidate.is_absolute():
            raise OperationalInstallationError("operational service reference must be absolute")
        references[label] = candidate
    paths = {selected, *(normalized(candidate) for candidate in candidates), *references.values()}
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
    return {"selected_interpreter": str(selected), "coverage": "EXPLICIT_PATHS_ONLY", "entries": entries,
            "conflicting_service_references": conflicts, "single_operational_installation": not conflicts}
