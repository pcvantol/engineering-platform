"""Qualified provider contracts and current local implementations.

Provider selection is configuration-owned.  These protocols deliberately expose
diagnostics only; they do not grant execution, repository or network authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Protocol


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    version: str
    qualified: bool
    detail: str


class RuntimeProvider(Protocol):
    def status(self) -> ProviderStatus: ...


class RepositoryProvider(Protocol):
    def status(self, root: Path) -> ProviderStatus: ...
    def command(self, root: Path, *args: str) -> str: ...


class ServiceManagerProvider(Protocol):
    def status(self) -> ProviderStatus: ...
    def install(self, label: str, plist: Path) -> None: ...
    def uninstall(self, plist: Path) -> None: ...


class RemoteSubmissionProvider(Protocol):
    def status(self) -> ProviderStatus: ...


class PrivateRemoteAccessProvider(Protocol):
    def status(self) -> ProviderStatus: ...


class CodexCliProvider:
    def status(self) -> ProviderStatus:
        available = shutil.which("codex") is not None
        return ProviderStatus("codex_cli", "configured", available, "available" if available else "codex unavailable")

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("codex", *args), text=True, capture_output=True, check=False)


class GitHubProvider:
    def status(self, root: Path) -> ProviderStatus:
        remote = subprocess.run(("git", "remote", "get-url", "origin"), cwd=root, text=True, capture_output=True, check=False)
        qualified = remote.returncode == 0 and "github" in remote.stdout.lower()
        return ProviderStatus("github", "configured", qualified, remote.stdout.strip() if qualified else "GitHub origin unavailable")

    def command(self, root: Path, *args: str) -> str:
        completed = subprocess.run(args, cwd=root, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "repository provider command failed")
        return completed.stdout.strip()

    def github(self, *args: str) -> str:
        completed = subprocess.run(("gh", *args), text=True, capture_output=True, check=False)
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "GitHub provider command failed")
        return completed.stdout.strip()


class LaunchdProvider:
    def status(self) -> ProviderStatus:
        available = shutil.which("launchctl") is not None
        return ProviderStatus("launchd", "configured", available, "available" if available else "launchctl unavailable")

    def install(self, label: str, plist: Path) -> None:
        subprocess.run(("launchctl", "bootout", f"gui/{__import__('os').getuid()}", str(plist)), check=False, capture_output=True)
        subprocess.run(("launchctl", "bootstrap", f"gui/{__import__('os').getuid()}", str(plist)), check=False)

    def uninstall(self, plist: Path) -> None:
        subprocess.run(("launchctl", "bootout", f"gui/{__import__('os').getuid()}", str(plist)), check=False)


class ICloudInboxProvider:
    def status(self) -> ProviderStatus:
        return ProviderStatus("icloud_inbox", "configured", True, "workspace path is resolved by the watcher")


class TailscaleProvider:
    def status(self) -> ProviderStatus:
        executable = shutil.which("tailscale")
        if not executable:
            return ProviderStatus("tailscale", "configured", False, "tailscale unavailable")
        observed = subprocess.run((executable, "status", "--json"), text=True, capture_output=True, check=False)
        return ProviderStatus("tailscale", "configured", observed.returncode == 0, "connected" if observed.returncode == 0 else "not connected")


def registry(root: Path) -> dict[str, ProviderStatus]:
    """Return the deterministic current-provider registry."""
    return {"runtime": CodexCliProvider().status(), "repository": GitHubProvider().status(root), "service_manager": LaunchdProvider().status(), "remote_submission": ICloudInboxProvider().status(), "private_remote_access": TailscaleProvider().status()}
