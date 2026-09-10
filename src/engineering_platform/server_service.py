"""Legacy macOS per-user LaunchAgent compatibility for the installed EP Server.

This module is retained for historical compatibility while a product-owned
system-domain LaunchDaemon provisioner is completed.  It is never the
authority for official operational runtime resolution, installation readback,
qualification, inventory or development-profile isolation; those surfaces use
``system_server_service`` only.

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
import shlex
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
    # Preserve a virtual-environment launcher symlink.  Resolving it would
    # turn ``<venv>/bin/python`` into its base interpreter, which no longer
    # has the EP package installed when launchd invokes it.
    executable = Path(candidate or sys.executable).expanduser().absolute()
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


def configured_interpreter(data_root: Path, *, home: Path | None = None) -> Path | None:
    """Read the owned service's fixed interpreter without consulting PATH."""
    plist = default_paths(data_root, home).plist_path
    if not plist.is_file():
        return None
    try:
        with plist.open("rb") as stream:
            arguments = plistlib.load(stream).get("ProgramArguments")
    except (OSError, plistlib.InvalidFileException):
        return None
    if (not isinstance(arguments, list) or len(arguments) < 3 or not isinstance(arguments[0], str)
            or arguments[1:3] != ["-m", "engineering_platform.server"]):
        return None
    return _installed_interpreter(arguments[0])


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


def replace_runtime(data_root: Path, *, expected_interpreter: str | Path, interpreter: str | Path,
                    home: Path | None = None, runner: Runner | None = None) -> Mapping[str, str]:
    """Switch the one owned LaunchAgent to one exact replacement interpreter.

    This is an installer primitive, not a PATH-based repair command.  It
    refuses an absent or changed service reference, stops that one owned
    service, and only then bootstraps its replacement.  Callers persist their
    installation record after this function reports activation success.
    """
    paths = default_paths(data_root, home)
    current = configured_interpreter(data_root, home=home)
    expected, replacement = _installed_interpreter(expected_interpreter), _installed_interpreter(interpreter)
    if current is None or current not in {expected, replacement}:
        raise ServerServiceError("EP Server service does not reference the expected operational interpreter.")
    if replacement == expected:
        return {"state": "unchanged", "label": LABEL, "plist": str(paths.plist_path), "interpreter": str(replacement)}
    # Stop the service while its existing plist still binds the old runtime.
    # If a crash follows the stop, the unchanged plist permits an exact retry.
    # If it follows the new write, the next retry observes the replacement and
    # only completes the idempotent bootstrap below.
    if current == expected:
        result = _launchctl(("bootout", _domain(), str(paths.plist_path)), runner)
        if result.returncode and "could not find service" not in (result.stderr or "").lower():
            raise ServerServiceError("Unable to stop the owned EP Server LaunchAgent for runtime replacement.")
        plist = write_plist(paths, replacement)
    else:
        plist = paths.plist_path
    result = _launchctl(("bootstrap", _domain(), str(plist)), runner)
    if result.returncode and "service already loaded" not in (result.stderr or "").lower():
        raise ServerServiceError("Unable to activate the replacement EP Server runtime.")
    return {"state": "replaced", "label": LABEL, "plist": str(plist), "interpreter": str(replacement)}


def repoint_after_relocation(previous: Path, destination: Path, *, home: Path | None = None,
                             runner: Runner | None = None) -> bool:
    """Reload an installed owned LaunchAgent with its moved data root.

    Manual ``serve`` invocations have no supervisor to update and simply keep
    running from ``destination``.  An installed LaunchAgent is unloaded and
    bootstrapped again, so its next process has no dependency on the old path.
    """
    old_paths = default_paths(previous, home)
    if not old_paths.plist_path.is_file():
        return False
    new_paths = default_paths(destination, home)
    plist = write_plist(new_paths, _installed_interpreter())
    if runner is not None:
        # The injected runner is used by lifecycle tests; production uses the
        # detached hand-off below so launchd can stop this very process first.
        bootout = _launchctl(("bootout", _domain(), str(plist)), runner)
        if bootout.returncode:
            raise ServerServiceError("Unable to unload EP Server LaunchAgent after platform-data relocation.")
        bootstrap = _launchctl(("bootstrap", _domain(), str(plist)), runner)
        if bootstrap.returncode:
            raise ServerServiceError("Unable to reload EP Server LaunchAgent after platform-data relocation.")
        return True
    if platform.system() != "Darwin":
        return False
    # launchctl bootout terminates this serving process.  A detached, bounded
    # helper therefore performs the unload/reload after the current request
    # has been answered instead of relying on KeepAlive or an old-path link.
    quoted = shlex.quote(str(plist))
    domain = shlex.quote(_domain())
    subprocess.Popen(
        ("/bin/sh", "-c", f"sleep 1; launchctl bootout {domain} {quoted}; launchctl bootstrap {domain} {quoted}"),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
    return True
