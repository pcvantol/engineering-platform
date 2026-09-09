"""Fail-closed isolation for an explicit Engineering Platform development runtime.

The Server installer remains the only owner of an operational installation.
This module does not install, update, migrate, or select an operational
runtime.  It records a development-only profile beneath an explicitly chosen
data root and rejects inputs that would make that runtime borrow operational
data, service labels, or credentials.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Mapping
from uuid import uuid4


FILENAME = "development-profile.json"
SCHEMA_VERSION = 1
PROFILE = "DEVELOPMENT"
DEFAULT_OPERATIONAL_PORT = 8765
LOG_DIRECTORY = "development-logs"
CACHE_DIRECTORY = "development-cache"

# These names are the supported process-level routes for operational state or
# bearer material.  A development process must receive its own explicit
# profile, rather than inherit any of them from an operational shell.
OPERATIONAL_ENVIRONMENT_NAMES = frozenset({
    "EP_CONSUMER_TOKEN",
    "EP_CENTRAL_OPERATIONAL_DATABASE",
    "ENGINEERING_PLATFORM_ADMITTED_STORAGE_ROOT",
    "ENGINEERING_PLATFORM_ADMITTED_STORAGE_SCHEMA",
})
OPERATIONAL_INSTALLATION_RECORD = "operational-installation.json"
_PROFILE_ID = re.compile(r"^development-[0-9a-f]{32}$")
_CREDENTIAL_REFERENCE = re.compile(r"^development:[A-Za-z0-9._:/-]{1,240}$")
_FIELDS = frozenset({
    "schema_version", "profile", "profile_id", "data_root", "interpreter",
    "venv", "bind_port", "credential_reference", "logs_directory",
    "cache_directory",
})


class DevelopmentProfileError(ValueError):
    """The requested development runtime would cross an operational boundary."""


@dataclass(frozen=True)
class DevelopmentProfile:
    """Non-secret, immutable development-runtime identity."""

    profile_id: str
    data_root: str
    interpreter: str
    venv: str
    bind_port: int
    credential_reference: str
    logs_directory: str
    cache_directory: str

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "profile": PROFILE,
            **asdict(self),
        }

    def server_arguments(self) -> tuple[str, ...]:
        """Return the fixed, non-secret arguments needed by a child Server."""
        return (
            "--runtime-profile", "development",
            "--development-venv", self.venv,
            "--development-credential-reference", self.credential_reference,
            "--bind-port", str(self.bind_port),
        )

    def visible_identity(self) -> dict[str, object]:
        """Expose a token-free diagnostic projection for status and health."""
        return {
            "kind": PROFILE,
            "profile_id": self.profile_id,
            "data_root": self.data_root,
            "interpreter": self.interpreter,
            "venv": self.venv,
            "bind_port": self.bind_port,
            "credential_reference": "CONFIGURED",
            "logs_directory": self.logs_directory,
            "cache_directory": self.cache_directory,
        }


def _root(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _launcher(path: str | Path) -> Path:
    """Keep a venv launcher path lexical; resolving it loses its venv identity."""
    return Path(path).expanduser().absolute()


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _credential_reference(value: object) -> str:
    if not isinstance(value, str) or _CREDENTIAL_REFERENCE.fullmatch(value) is None:
        raise DevelopmentProfileError(
            "development credential reference must be a development-scoped reference"
        )
    return value


def _profile(value: object) -> DevelopmentProfile:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise DevelopmentProfileError("development profile is invalid")
    if value.get("schema_version") != SCHEMA_VERSION or value.get("profile") != PROFILE:
        raise DevelopmentProfileError("development profile is invalid")
    strings = (
        "profile_id", "data_root", "interpreter", "venv", "credential_reference",
        "logs_directory", "cache_directory",
    )
    if not all(isinstance(value.get(key), str) and value[key] for key in strings):
        raise DevelopmentProfileError("development profile is invalid")
    profile_id = str(value["profile_id"])
    if _PROFILE_ID.fullmatch(profile_id) is None:
        raise DevelopmentProfileError("development profile is invalid")
    data_root, interpreter, venv = (
        _root(str(value["data_root"])), _launcher(str(value["interpreter"])),
        _launcher(str(value["venv"])),
    )
    bind_port = value.get("bind_port")
    if (
        not isinstance(bind_port, int)
        or not 1 <= bind_port <= 65535
        or bind_port == DEFAULT_OPERATIONAL_PORT
        or not interpreter.is_file()
        or not (venv / "pyvenv.cfg").is_file()
        or interpreter.parent != venv / "bin"
        or not interpreter.name.startswith("python")
        or _root(str(value["logs_directory"])) != data_root / LOG_DIRECTORY
        or _root(str(value["cache_directory"])) != data_root / CACHE_DIRECTORY
    ):
        raise DevelopmentProfileError("development profile is invalid")
    credential_reference = _credential_reference(value["credential_reference"])
    return DevelopmentProfile(
        profile_id=profile_id,
        data_root=str(data_root),
        interpreter=str(interpreter),
        venv=str(venv),
        bind_port=bind_port,
        credential_reference=credential_reference,
        logs_directory=str(data_root / LOG_DIRECTORY),
        cache_directory=str(data_root / CACHE_DIRECTORY),
    )


def _read(data_root: Path) -> DevelopmentProfile:
    try:
        value = json.loads((data_root / FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DevelopmentProfileError("development profile is unavailable") from error
    return _profile(value)


def _write(data_root: Path, profile: DevelopmentProfile) -> None:
    path = data_root / FILENAME
    descriptor, temporary = tempfile.mkstemp(prefix=".development-profile-", dir=data_root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(profile.payload(), stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _validate_request(
    *,
    data_root: str | Path,
    bind_port: int,
    development_venv: str | Path | None,
    credential_reference: str | None,
    interpreter: str | Path,
    operational_data_roots: Iterable[str | Path],
    operational_interpreters: Iterable[str | Path] = (),
    environment: Mapping[str, str] | None = None,
) -> DevelopmentProfile:
    root, active = _root(data_root), _launcher(interpreter)
    if not isinstance(bind_port, int) or not 1 <= bind_port <= 65535:
        raise DevelopmentProfileError("development bind port is invalid")
    if bind_port == DEFAULT_OPERATIONAL_PORT:
        raise DevelopmentProfileError("development profile must not use the operational bind port")
    if development_venv is None:
        raise DevelopmentProfileError("development profile requires an explicit development venv")
    venv = _launcher(development_venv)
    if (
        not venv.is_absolute()
        or not (venv / "pyvenv.cfg").is_file()
        or not active.is_file()
        or active.parent != venv / "bin"
        or not active.name.startswith("python")
    ):
        raise DevelopmentProfileError("development runtime must use its explicit development venv")
    reference = _credential_reference(credential_reference)
    for candidate in operational_data_roots:
        operational = _root(candidate)
        if root == operational or _within(root, operational):
            raise DevelopmentProfileError("development profile must not use an operational data root")
    inherited_operational = sorted(
        name for name in OPERATIONAL_ENVIRONMENT_NAMES if (environment or os.environ).get(name)
    )
    if inherited_operational:
        raise DevelopmentProfileError(
            "development profile inherits operational credentials or data: " + ", ".join(inherited_operational)
        )
    if (root / OPERATIONAL_INSTALLATION_RECORD).exists():
        raise DevelopmentProfileError("development profile must not use a registered operational installation")
    for candidate in operational_interpreters:
        if active == _launcher(candidate):
            raise DevelopmentProfileError("development runtime must not use the operational interpreter")
    return DevelopmentProfile(
        profile_id="development-" + uuid4().hex,
        data_root=str(root),
        interpreter=str(active),
        venv=str(venv),
        bind_port=bind_port,
        credential_reference=reference,
        logs_directory=str(root / LOG_DIRECTORY),
        cache_directory=str(root / CACHE_DIRECTORY),
    )


def establish(
    *,
    data_root: str | Path,
    bind_port: int,
    development_venv: str | Path | None,
    credential_reference: str | None,
    interpreter: str | Path,
    operational_data_roots: Iterable[str | Path],
    operational_interpreters: Iterable[str | Path] = (),
    environment: Mapping[str, str] | None = None,
) -> DevelopmentProfile:
    """Create or reopen exactly one explicit profile before development init."""
    requested = _validate_request(
        data_root=data_root,
        bind_port=bind_port,
        development_venv=development_venv,
        credential_reference=credential_reference,
        interpreter=interpreter,
        operational_data_roots=operational_data_roots,
        operational_interpreters=operational_interpreters,
        environment=environment,
    )
    root = Path(requested.data_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = root / FILENAME
    if marker.exists():
        existing = _read(root)
        expected = {**requested.payload(), "profile_id": existing.profile_id}
        if existing.payload() != expected:
            raise DevelopmentProfileError("development profile conflicts with its existing identity")
        profile = existing
    else:
        # A nonempty, unmarked root cannot safely be assumed to be a newly
        # isolated development environment.  This blocks accidental adoption
        # of a relocated or older operational Server root.
        forbidden = [item.name for item in root.iterdir() if item.name not in {LOG_DIRECTORY, CACHE_DIRECTORY}]
        if forbidden:
            raise DevelopmentProfileError("development data root is not an empty isolated root")
        profile = requested
        _write(root, profile)
    Path(profile.logs_directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(profile.cache_directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    return profile


def require(
    *,
    data_root: str | Path,
    bind_port: int,
    development_venv: str | Path | None,
    credential_reference: str | None,
    interpreter: str | Path,
    operational_data_roots: Iterable[str | Path],
    operational_interpreters: Iterable[str | Path] = (),
    environment: Mapping[str, str] | None = None,
) -> DevelopmentProfile:
    """Load a profile only when the current process matches it exactly."""
    requested = _validate_request(
        data_root=data_root,
        bind_port=bind_port,
        development_venv=development_venv,
        credential_reference=credential_reference,
        interpreter=interpreter,
        operational_data_roots=operational_data_roots,
        operational_interpreters=operational_interpreters,
        environment=environment,
    )
    existing = _read(Path(requested.data_root))
    expected = {**requested.payload(), "profile_id": existing.profile_id}
    if existing.payload() != expected:
        raise DevelopmentProfileError("development profile does not match the current runtime")
    return existing


def require_explicit_runtime(data_root: str | Path, runtime_profile: str) -> None:
    """Prevent an existing development root from silently becoming operational."""
    root = _root(data_root)
    marker = root / FILENAME
    if marker.exists() and runtime_profile != "development":
        raise DevelopmentProfileError("development data root requires --runtime-profile development")


def describe(data_root: str | Path) -> dict[str, object]:
    """Return a token-free identity projection without selecting a runtime."""
    root = _root(data_root)
    if not (root / FILENAME).exists():
        return {"kind": "OPERATIONAL"}
    return _read(root).visible_identity()


def reject_operational_command(command: str, *, service_labels: Mapping[str, str]) -> None:
    """Keep a development profile out of operational service/credential paths."""
    label = service_labels.get(command)
    if label:
        raise DevelopmentProfileError(
            f"development profile cannot manage operational service label {label}"
        )
    if command in {"issue-consumer-credential", "pairing-create", "agent-reset"}:
        raise DevelopmentProfileError(
            "development profile cannot issue, reset, or use operational credentials"
        )
