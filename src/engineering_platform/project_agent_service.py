"""macOS per-user LaunchAgent lifecycle for the EP Project Agent (B6B).

This module owns local packaging and supervision only.  It deliberately does
not define Agent pairing, authentication, registration, or repository access.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
from typing import Callable, Mapping, Sequence


LABEL = "com.engineeringplatform.project-agent"
CONFIG_VERSION = 1
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PRODUCT = "Engineering Platform"
COMPONENT_ROLE = "project-agent"


class AgentServiceError(ValueError):
    """Raised when a bounded Agent lifecycle operation cannot proceed safely."""


@dataclass(frozen=True)
class AgentPaths:
    config_dir: Path
    state_dir: Path
    log_dir: Path
    launch_agents_dir: Path

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_dir / f"{LABEL}.plist"

    @property
    def stdout_log(self) -> Path:
        # Launchd output is a bounded process diagnostic, never a second EP
        # operational log authority.  The unpaired Project Agent has no
        # CENTRAL writer of its own, so discard its idle-loop output instead
        # of retaining a persistent per-agent logfile.
        return Path("/dev/null")

    @property
    def stderr_log(self) -> Path:
        return Path("/dev/null")


@dataclass(frozen=True)
class AgentConfiguration:
    version: int
    toolchain_paths: tuple[str, ...] = ()
    b6a: Mapping[str, str] | None = None

    @classmethod
    def load(cls, path: Path) -> "AgentConfiguration":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AgentServiceError("Project Agent configuration is unavailable.") from error
        if not isinstance(raw, dict) or set(raw) != {"version", "toolchain_paths", "b6a"}:
            raise AgentServiceError("Project Agent configuration has unexpected fields.")
        paths, b6a = raw["toolchain_paths"], raw["b6a"]
        if raw["version"] != CONFIG_VERSION or not isinstance(paths, list) or not all(isinstance(item, str) and Path(item).is_absolute() for item in paths):
            raise AgentServiceError("Project Agent configuration is invalid.")
        if b6a is not None:
            allowed = {"server_endpoint", "paired_agent_identity", "credential_reference", "protocol_version"}
            if not isinstance(b6a, dict) or not set(b6a).issubset(allowed) or not all(isinstance(key, str) and isinstance(value, str) and value for key, value in b6a.items()):
                raise AgentServiceError("Project Agent B6A extension configuration is invalid.")
        return cls(CONFIG_VERSION, tuple(paths), b6a)

    def payload(self) -> dict[str, object]:
        return {"version": self.version, "toolchain_paths": list(self.toolchain_paths), "b6a": dict(self.b6a) if self.b6a else None}


def default_paths(home: Path | None = None) -> AgentPaths:
    root = (home or Path.home()).expanduser()
    return AgentPaths(
        root / "Library" / "Application Support" / PRODUCT / "Project Agent",
        root / "Library" / "Caches" / PRODUCT / "Project Agent",
        root / "Library" / "Logs" / PRODUCT / "Project Agent",
        root / "Library" / "LaunchAgents",
    )


def runtime_path(configuration: AgentConfiguration) -> str:
    return ":".join((*configuration.toolchain_paths, DEFAULT_PATH))


def artifact_metadata(executable: Path) -> dict[str, str | None]:
    """Metadata envelope for a future qualified artifact publication process."""
    from importlib.metadata import version
    try:
        installed_version = version("engineering-platform")
    except Exception:  # pragma: no cover - source-tree developer invocation
        installed_version = "unknown"
    return {"product": PRODUCT, "component_role": COMPONENT_ROLE, "version": installed_version, "source_repository": "pcvantol/engineering-platform", "source_sha": None, "artifact_identity": str(executable), "artifact_sha256": None, "platform": "macos", "architecture": platform.machine() or None, "compatibility": "macos user LaunchAgent", "release_channel": None, "qualification_reference": None, "signature_attestation": None}


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def initialize(paths: AgentPaths) -> AgentConfiguration:
    for directory in (paths.config_dir, paths.state_dir, paths.log_dir, paths.launch_agents_dir):
        directory.mkdir(parents=True, exist_ok=True)
    for directory in (paths.config_dir, paths.state_dir, paths.log_dir):
        directory.chmod(0o700)
    if not paths.config_path.exists():
        _write_json(paths.config_path, AgentConfiguration(CONFIG_VERSION).payload())
    return AgentConfiguration.load(paths.config_path)


def resolve_installed_executable(candidate: str | Path | None = None) -> Path:
    raw = str(candidate) if candidate else shutil.which("engineering-project-agent")
    if not raw:
        raise AgentServiceError("The installed engineering-project-agent executable was not found on PATH.")
    executable = Path(raw).expanduser().absolute()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise AgentServiceError("The Project Agent executable is not an executable installed artifact.")
    return executable


def plist_payload(paths: AgentPaths, executable: Path, configuration: AgentConfiguration) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "service", "run", "--config", str(paths.config_path)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": runtime_path(configuration), "ENGINEERING_PLATFORM_AGENT_CONFIG": str(paths.config_path)},
        "StandardOutPath": str(paths.stdout_log),
        "StandardErrorPath": str(paths.stderr_log),
    }


def plist_text(payload: Mapping[str, object]) -> str:
    import plistlib
    return plistlib.dumps(dict(payload), fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")


def write_plist(paths: AgentPaths, executable: Path, configuration: AgentConfiguration) -> Path:
    content = plist_text(plist_payload(paths, executable, configuration))
    if any(word in content.lower() for word in ("secret", "password", "token=")):
        raise AgentServiceError("Refusing to write a plist containing secret material.")
    temporary = paths.plist_path.with_suffix(".plist.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o644)
    os.replace(temporary, paths.plist_path)
    paths.plist_path.chmod(0o644)
    return paths.plist_path


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _launchctl(arguments: Sequence[str], runner: Runner | None = None) -> subprocess.CompletedProcess[str]:
    if platform.system() != "Darwin":
        raise AgentServiceError("Project Agent LaunchAgent lifecycle is supported only on macOS.")
    return (runner or (lambda command: subprocess.run(command, capture_output=True, text=True, check=False)))(["launchctl", *arguments])


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(paths: AgentPaths | None = None, *, executable: str | Path | None = None, runner: Runner | None = None) -> dict[str, object]:
    locations = paths or default_paths()
    configuration = initialize(locations)
    installed_executable = resolve_installed_executable(executable)
    plist = write_plist(locations, installed_executable, configuration)
    result = _launchctl(("bootstrap", _domain(), str(plist)), runner)
    # Already loaded is the expected idempotent repair outcome; do not hide other failures.
    if result.returncode and "service already loaded" not in (result.stderr or "").lower():
        raise AgentServiceError("Unable to bootstrap Project Agent LaunchAgent.")
    return {"state": "installed", "plist": str(plist), "configuration": str(locations.config_path), "artifact": artifact_metadata(installed_executable)}


def start(paths: AgentPaths | None = None, *, runner: Runner | None = None) -> None:
    locations = paths or default_paths()
    if not locations.plist_path.exists():
        raise AgentServiceError("Project Agent is not installed.")
    result = _launchctl(("bootstrap", _domain(), str(locations.plist_path)), runner)
    if result.returncode and "service already loaded" not in (result.stderr or "").lower():
        raise AgentServiceError("Unable to load Project Agent LaunchAgent.")
    result = _launchctl(("kickstart", "-k", f"{_domain()}/{LABEL}"), runner)
    if result.returncode:
        raise AgentServiceError("Unable to start Project Agent LaunchAgent.")


def stop(paths: AgentPaths | None = None, *, runner: Runner | None = None) -> None:
    result = _launchctl(("bootout", f"{_domain()}/{LABEL}"), runner)
    if result.returncode and not _not_loaded(result.stderr):
        raise AgentServiceError("Unable to stop Project Agent LaunchAgent.")


def uninstall(paths: AgentPaths | None = None, *, runner: Runner | None = None) -> None:
    locations = paths or default_paths()
    if locations.plist_path.exists():
        result = _launchctl(("bootout", _domain(), str(locations.plist_path)), runner)
        if result.returncode and not _not_loaded(result.stderr):
            raise AgentServiceError("Unable to unload Project Agent LaunchAgent.")
        locations.plist_path.unlink(missing_ok=True)
    # State is installation-owned and transient.  Config/identity (and any future
    # credential reference) are intentionally preserved for explicit reset only.
    if locations.state_dir.exists():
        shutil.rmtree(locations.state_dir)


def status(paths: AgentPaths | None = None, *, runner: Runner | None = None) -> dict[str, str]:
    locations = paths or default_paths()
    if not locations.plist_path.exists():
        return {"state": "not-installed"}
    try:
        AgentConfiguration.load(locations.config_path)
    except AgentServiceError:
        return {"state": "misconfigured"}
    result = _launchctl(("print", f"{_domain()}/{LABEL}"), runner)
    return {"state": "running" if result.returncode == 0 else "stopped"}


def _not_loaded(stderr: str) -> bool:
    return any(marker in (stderr or "").lower() for marker in ("could not find service", "no such process", "not found"))


def run(config_path: Path, *, poll_seconds: float = 30.0) -> int:
    """Long-running, intentionally unpaired process for launchd supervision."""
    AgentConfiguration.load(config_path)
    while True:
        time.sleep(poll_seconds)
