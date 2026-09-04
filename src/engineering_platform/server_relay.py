"""Server-owned lifecycle for the bounded Tailnet Console relay.

The relay is an access adapter only: it forwards the Tailnet listener to the
installed Server's loopback Console.  It owns no submission, project, File
Inbox, Action or run state.
"""
from __future__ import annotations

from html import escape
from pathlib import Path
import shutil

from .platform_components import PLATFORM_COMPONENT_BY_ID
from .providers import LaunchdProvider, LocalProcessProvider
from .resources import package_path


RUNTIME_DIRECTORY = "runtime"
RELAY_BINARY_FILENAME = "engineering-dashboard-relay"


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


def render_launch_agent(binary: Path) -> Path:
    """Render the one canonical Relay LaunchAgent from an installed binary."""
    destination = launch_agent_path()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{_definition_label()}</string><key>ProgramArguments</key><array><string>{escape(str(binary))}</string></array><key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>',
        encoding="utf-8",
    )
    return destination


def install(data_root: Path) -> dict[str, str]:
    """Install the bounded relay through the Server's component definition."""
    binary = build_relay(data_root)
    plist = render_launch_agent(binary)
    LaunchdProvider().install(_definition_label(), plist)
    return {"component": "dashboard_relay", "binary": str(binary), "launch_agent": str(plist)}


def uninstall() -> dict[str, str]:
    """Remove only the canonical relay LaunchAgent; Server data is untouched."""
    plist = launch_agent_path()
    LaunchdProvider().uninstall(plist)
    plist.unlink(missing_ok=True)
    return {"component": "dashboard_relay", "launch_agent": str(plist)}
