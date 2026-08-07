"""Qualified provider contracts and current local implementations.

Provider selection is configuration-owned.  These protocols deliberately expose
diagnostics only; they do not grant execution, repository or network authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
import shutil
import subprocess
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    version: str
    qualified: bool
    detail: str


class RuntimeProvider(Protocol):
    def status(self) -> ProviderStatus: ...


class ProcessProvider(Protocol):
    """The sole boundary for local child-process execution."""

    def execute(self, root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]: ...

    def spawn(self, root: Path, arguments: Sequence[str]) -> subprocess.Popen[str]: ...

    def spawn_detached(self, root: Path, arguments: Sequence[str], environment: Mapping[str, str]) -> subprocess.Popen[bytes]: ...


class LocalProcessProvider:
    """Default local process adapter; orchestration code never imports subprocess for work."""

    def execute(self, root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(arguments, cwd=root, text=True, capture_output=True, check=False)

    def spawn(self, root: Path, arguments: Sequence[str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            tuple(arguments), cwd=root, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
        )

    def spawn_detached(self, root: Path, arguments: Sequence[str], environment: Mapping[str, str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            tuple(arguments), cwd=root, env=dict(environment), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


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


class CodexCliProvider(LocalProcessProvider):
    def status(self) -> ProviderStatus:
        available = shutil.which("codex") is not None
        return ProviderStatus("codex_cli", "configured", available, "available" if available else "codex unavailable")

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("codex", *args), text=True, capture_output=True, check=False)

    def app_server(self) -> subprocess.Popen[str]:
        """Open the provider-owned interactive Codex app-server channel."""
        return subprocess.Popen(
            ("codex", "app-server"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )

    def close_app_server(self, process: subprocess.Popen[str]) -> None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def invoke(self, root: Path, arguments: tuple[str, ...], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        """Execute a complete Codex command; callers never spawn its CLI directly."""
        if timeout is None:
            return self.execute(root, arguments)
        try:
            return subprocess.run(arguments, cwd=root, text=True, capture_output=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise OSError("Codex provider invocation timed out") from error


class GitProvider(LocalProcessProvider):
    """Local Git provider, deliberately separate from the GitHub API provider."""

    def execute(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return super().execute(root, args)

    def command(self, root: Path, *args: str) -> str:
        """Run Git and expose its bounded text result to repository orchestration."""
        completed = self.execute(root, *args)
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git command failed")
        return completed.stdout.strip()


class GitHubProvider:
    def status(self, root: Path) -> ProviderStatus:
        remote = GitProvider().execute(root, "git", "remote", "get-url", "origin")
        qualified = remote.returncode == 0 and "github" in remote.stdout.lower()
        return ProviderStatus("github", "configured", qualified, remote.stdout.strip() if qualified else "GitHub origin unavailable")

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

    def inspect(self, label: str) -> bool:
        executable = shutil.which("launchctl")
        if not executable:
            return False
        return subprocess.run((executable, "print", f"gui/{__import__('os').getuid()}/{label}"), text=True, capture_output=True, check=False).returncode == 0

    def restart(self, label: str) -> None:
        executable = shutil.which("launchctl")
        if not executable:
            raise OSError("launchctl unavailable")
        completed = subprocess.run((executable, "kickstart", "-k", f"gui/{__import__('os').getuid()}/{label}"), text=True, capture_output=True, check=False)
        if completed.returncode:
            raise OSError(completed.stderr.strip() or "launchd restart failed")


class ICloudInboxProvider:
    def status(self) -> ProviderStatus:
        return ProviderStatus("icloud_inbox", "configured", True, "workspace path is resolved by the watcher")


class TailscaleProvider:
    _TAILSCALE_NETWORK = IPv4Network("100.64.0.0/10")

    def ipv4_address(self) -> str | None:
        """Return only the local, routable Tailscale IPv4 address.

        This is a read-only diagnostic query.  It never changes Tailnet
        configuration, ACLs, Funnel, or port-forwarding state.
        """
        executable = shutil.which("tailscale")
        if not executable:
            return None
        observed = subprocess.run((executable, "ip", "-4"), text=True, capture_output=True, check=False)
        if observed.returncode:
            return None
        for candidate in observed.stdout.splitlines():
            try:
                address = IPv4Address(candidate.strip())
            except ValueError:
                continue
            if address in self._TAILSCALE_NETWORK:
                return str(address)
        return None

    def status(self) -> ProviderStatus:
        executable = shutil.which("tailscale")
        if not executable:
            return ProviderStatus("tailscale", "configured", False, "tailscale unavailable")
        observed = subprocess.run((executable, "status", "--json"), text=True, capture_output=True, check=False)
        return ProviderStatus("tailscale", "configured", observed.returncode == 0, "connected" if observed.returncode == 0 else "not connected")


def registry(root: Path) -> dict[str, ProviderStatus]:
    """Return the deterministic current-provider registry."""
    return {"runtime": CodexCliProvider().status(), "repository": GitHubProvider().status(root), "service_manager": LaunchdProvider().status(), "remote_submission": ICloudInboxProvider().status(), "private_remote_access": TailscaleProvider().status()}
