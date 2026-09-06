"""Server-owned lifecycle for the bounded Tailnet Console relay.

The relay is an access adapter only: it forwards the Tailnet listener to the
installed Server's loopback Console.  It owns no submission, project, File
Inbox, Action or run state.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
import shutil
from urllib.error import URLError
from urllib.request import urlopen

from .platform_components import PLATFORM_COMPONENT_BY_ID
from .providers import LaunchdProvider, LocalProcessProvider
from .resources import package_path


RUNTIME_DIRECTORY = "runtime"
RELAY_BINARY_FILENAME = "engineering-dashboard-relay"
# This identifier is retained solely to migrate an already installed legacy
# relay.  The canonical component model is the only active lifecycle owner.
LEGACY_RELAY_LABEL = "com.djconnect.engineering-dashboard-relay"


class RelayCutoverError(RuntimeError):
    """Fail-closed error for the exact relay identity cutover."""


@dataclass(frozen=True)
class PreCutoverState:
    legacy_plist_present: bool
    legacy_loaded: bool
    legacy_process_active: bool
    legacy_functional_probe: bool | None


def _definition_label() -> str:
    label = PLATFORM_COMPONENT_BY_ID["dashboard_relay"].lifecycle_label
    if not label:
        raise RuntimeError("Dashboard Relay has no canonical lifecycle label.")
    return label


def relay_binary(data_root: Path) -> Path:
    """Return the installation-owned relay binary path, never a checkout path."""
    return data_root.resolve() / RUNTIME_DIRECTORY / RELAY_BINARY_FILENAME


def build_relay(data_root: Path) -> Path:
    """Compile the package-owned relay into the Server installation runtime."""
    compiler = shutil.which("swiftc")
    if compiler is None:
        raise RuntimeError("Swift compiler is unavailable; Dashboard Relay cannot start.")
    binary = relay_binary(data_root)
    binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    completed = LocalProcessProvider().execute(
        data_root.resolve(), (compiler, str(package_path("dashboard_supervisor.swift")), "-o", str(binary)),
    )
    if completed.returncode:
        raise RuntimeError("Dashboard Relay compilation failed.")
    binary.chmod(0o700)
    return binary


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_definition_label()}.plist"


def legacy_launch_agent_path() -> Path:
    """Return the one retired relay plist that may be migrated."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_RELAY_LABEL}.plist"


def relay_functional_probe() -> bool:
    """Perform the bounded, token-free relay health check used for commit."""
    try:
        with urlopen("http://127.0.0.1:8765/health", timeout=2) as response:  # noqa: S310 -- fixed loopback endpoint
            return 200 <= response.status < 300
    except (OSError, URLError):
        return False


def pre_cutover_state(launchd: LaunchdProvider, *, probe=relay_functional_probe) -> PreCutoverState:
    """Capture the non-secret legacy state that rollback must restore exactly."""
    legacy = legacy_launch_agent_path()
    loaded = launchd.inspect(LEGACY_RELAY_LABEL)
    runtime = launchd.runtime_status(LEGACY_RELAY_LABEL) if loaded else None
    active = bool(runtime and runtime.qualified)
    return PreCutoverState(legacy.is_file(), loaded, active, probe() if active else None)


def _unload_and_verify(launchd: LaunchdProvider, label: str, plist: Path, *, code: str) -> None:
    if not launchd.inspect(label):
        return
    launchd.uninstall(plist)
    if launchd.inspect(label):
        raise RelayCutoverError(code)


def _restore_legacy(launchd: LaunchdProvider, state: PreCutoverState, legacy: Path, neutral: Path, *, probe=relay_functional_probe) -> None:
    """Restore exactly the captured legacy loaded state after neutral is absent."""
    _unload_and_verify(launchd, _definition_label(), neutral, code="ROLLBACK_NEUTRAL_UNLOAD_UNVERIFIED")
    neutral.unlink(missing_ok=True)
    if state.legacy_loaded:
        if not state.legacy_plist_present:
            raise RelayCutoverError("ROLLBACK_LEGACY_PLIST_MISSING")
        launchd.install(LEGACY_RELAY_LABEL, legacy)
        if not launchd.inspect(LEGACY_RELAY_LABEL):
            raise RelayCutoverError("ROLLBACK_LEGACY_LOAD_UNVERIFIED")
        restored = launchd.runtime_status(LEGACY_RELAY_LABEL).qualified
        if restored != state.legacy_process_active:
            raise RelayCutoverError("ROLLBACK_LEGACY_PROCESS_STATE_MISMATCH")
        if state.legacy_functional_probe is True and not probe():
            raise RelayCutoverError("ROLLBACK_LEGACY_FUNCTIONAL_PROBE_FAILED")
    elif launchd.inspect(LEGACY_RELAY_LABEL):
        raise RelayCutoverError("ROLLBACK_LEGACY_STATE_MISMATCH")


def render_launch_agent(binary: Path) -> Path:
    """Render the one canonical Relay LaunchAgent from an installed binary."""
    destination = launch_agent_path()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{_definition_label()}</string><key>ProgramArguments</key><array><string>{escape(str(binary))}</string></array><key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>',
        encoding="utf-8",
    )
    return destination


def install(data_root: Path, *, probe=relay_functional_probe) -> dict[str, str]:
    """Install the canonical relay and retire its single migration source.

    The commit point is after neutral loaded/process/functional proof.  Before
    that point the untouched legacy plist and captured lifecycle state are the
    sole rollback authority.
    """
    binary = build_relay(data_root)
    plist = render_launch_agent(binary)
    launchd = LaunchdProvider()
    legacy = legacy_launch_agent_path()
    state = pre_cutover_state(launchd, probe=probe)
    if launchd.inspect(_definition_label()):
        raise RelayCutoverError("NEUTRAL_ALREADY_LOADED")
    if state.legacy_loaded:
        _unload_and_verify(launchd, LEGACY_RELAY_LABEL, legacy, code="LEGACY_UNLOAD_UNVERIFIED")
    try:
        launchd.install(_definition_label(), plist)
        if not launchd.inspect(_definition_label()):
            raise RelayCutoverError("NEUTRAL_LOAD_UNVERIFIED")
        if not launchd.runtime_status(_definition_label()).qualified:
            raise RelayCutoverError("NEUTRAL_PROCESS_UNHEALTHY")
        if not probe():
            raise RelayCutoverError("NEUTRAL_FUNCTIONAL_PROBE_FAILED")
    except Exception as error:
        _restore_legacy(launchd, state, legacy, plist, probe=probe)
        raise RelayCutoverError("RELAY_CUTOVER_ROLLED_BACK") from error
    # Commit point: legacy is proven inactive, and neutral is loaded, active,
    # and functionally healthy. Only now may the migration source be retired.
    if state.legacy_plist_present:
        legacy.unlink()
    return {"component": "dashboard_relay", "binary": str(binary), "launch_agent": str(plist)}


def uninstall() -> dict[str, str]:
    """Remove only the canonical relay LaunchAgent; Server data is untouched."""
    plist = launch_agent_path()
    LaunchdProvider().uninstall(plist)
    plist.unlink(missing_ok=True)
    return {"component": "dashboard_relay", "launch_agent": str(plist)}
