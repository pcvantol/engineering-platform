"""macOS LaunchAgent lifecycle for the installed EP Server.

The Server remains the lifecycle and CENTRAL authority.  This module only
installs one fixed per-user supervisor for its foreground ``serve`` command;
it never starts a second daemon or accepts arbitrary commands.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import platform
import subprocess
import sys
from typing import Callable, Mapping, Sequence


LABEL = "com.engineeringplatform.server"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class ServerServiceError(ValueError):
    """Raised when the bounded EP Server service cannot be managed safely."""


@dataclass(frozen=True)
class ServicePaths:
    data_root: Path
    launch_agents_dir: Path

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_dir / f"{LABEL}.plist"

    @property
    def log_dir(self) -> Path:
        return self.data_root / "runtime"

    @property
    def stderr_log(self) -> Path:
        return self.log_dir / "server-launchagent.err.log"


def default_paths(data_root: Path, home: Path | None = None) -> ServicePaths:
    return ServicePaths(data_root.resolve(), (home or Path.home()).expanduser() / "Library" / "LaunchAgents")


def _domain() -> str:
    return f"gui/{os.getuid()}"


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _launchctl(arguments: Sequence[str], runner: Runner | None = None) -> subprocess.CompletedProcess[str]:
    if platform.system() != "Darwin":
        raise ServerServiceError("EP Server LaunchAgent lifecycle is supported only on macOS.")
    return (runner or (lambda command: subprocess.run(command, capture_output=True, text=True, check=False)))(["launchctl", *arguments])


def _installed_interpreter(candidate: str | Path | None = None) -> Path:
    executable = Path(candidate or sys.executable).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ServerServiceError("The EP Server interpreter is not an executable installed runtime.")
    return executable


def plist_payload(paths: ServicePaths, interpreter: Path) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(interpreter), "-m", "engineering_platform.server", "serve", "--data-root", str(paths.data_root)],
        "WorkingDirectory": str(paths.data_root),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": DEFAULT_PATH,
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "EP_SERVER_DATA_ROOT": str(paths.data_root),
        },
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(paths.stderr_log),
    }


def write_plist(paths: ServicePaths, interpreter: Path) -> Path:
    paths.launch_agents_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = plistlib.dumps(plist_payload(paths, interpreter), fmt=plistlib.FMT_XML, sort_keys=True)
    temporary = paths.plist_path.with_suffix(".plist.tmp")
    temporary.write_bytes(content)
    temporary.chmod(0o644)
    os.replace(temporary, paths.plist_path)
    paths.plist_path.chmod(0o644)
    return paths.plist_path


def install(data_root: Path, *, interpreter: str | Path | None = None, home: Path | None = None,
            runner: Runner | None = None) -> Mapping[str, str]:
    paths = default_paths(data_root, home)
    if not paths.data_root.is_dir() or not (paths.data_root / "server.json").is_file():
        raise ServerServiceError("EP Server must be initialized before its LaunchAgent is installed.")
    plist = write_plist(paths, _installed_interpreter(interpreter))
    result = _launchctl(("bootstrap", _domain(), str(plist)), runner)
    if result.returncode and "service already loaded" not in (result.stderr or "").lower():
        raise ServerServiceError("Unable to bootstrap EP Server LaunchAgent.")
    return {"state": "installed", "label": LABEL, "plist": str(plist), "data_root": str(paths.data_root), "stderr_log": str(paths.stderr_log)}


def uninstall(data_root: Path, *, home: Path | None = None, runner: Runner | None = None) -> Mapping[str, str]:
    paths = default_paths(data_root, home)
    if paths.plist_path.exists():
        result = _launchctl(("bootout", _domain(), str(paths.plist_path)), runner)
        if result.returncode and not any(marker in (result.stderr or "").lower() for marker in ("could not find service", "no such process", "not found")):
            raise ServerServiceError("Unable to unload EP Server LaunchAgent.")
        paths.plist_path.unlink(missing_ok=True)
    return {"state": "uninstalled", "label": LABEL, "plist": str(paths.plist_path), "data_root": str(paths.data_root)}
