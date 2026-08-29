"""Qualified provider contracts and current local implementations.

Provider selection is configuration-owned.  These protocols deliberately expose
diagnostics only; they do not grant execution, repository or network authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
import os
from pathlib import Path
import re
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


class DeterministicValidationExecutor:
    """Run one resolved validation control outside provider-agent dispatch."""

    def __init__(self, process: ProcessProvider | None = None) -> None:
        self.process = process or LocalProcessProvider()

    def run(self, root: Path, command: tuple[str, ...]) -> "DeterministicValidationResult":
        try:
            completed = self.process.execute(root, command)
            stdout = completed.stdout
            stderr = completed.stderr
            return DeterministicValidationResult(
                exit_code=completed.returncode,
                stdout=stdout if isinstance(stdout, str) else None,
                stderr=stderr if isinstance(stderr, str) else None,
                diagnostic_capture_available=isinstance(stdout, str) and isinstance(stderr, str),
            )
        except OSError:
            return DeterministicValidationResult(
                exit_code=None, stdout=None, stderr=None,
                diagnostic_capture_available=False,
            )


@dataclass(frozen=True)
class DeterministicValidationResult:
    """One deterministic command outcome, including non-authoritative output."""

    exit_code: int | None
    stdout: str | None
    stderr: str | None
    diagnostic_capture_available: bool


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


def engineering_platform_codex_cli_prefix() -> Path:
    """Return the user-owned prefix reserved for Engineering Platform's CLI."""
    return Path.home() / ".local" / "share" / "engineering-platform" / "codex-cli"


def codex_cli_executable() -> str | None:
    """Return only Engineering Platform's managed Codex CLI executable."""
    managed = engineering_platform_codex_cli_prefix() / "bin" / "codex"
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
    return None


class CodexCliProvider(LocalProcessProvider):
    """Codex process adapter pinned exclusively to EP's managed launcher."""

    def __init__(self, executable: str | None = None) -> None:
        del executable  # Runtime injection must not bypass EP's managed CLI.
        self._executable = codex_cli_executable() or ""

    def managed_installation_path(self) -> str | None:
        """Return provenance only when this invocation is pinned to EP's CLI."""
        managed = engineering_platform_codex_cli_prefix() / "bin" / "codex"
        return str(engineering_platform_codex_cli_prefix()) if self._executable == str(managed) else None

    def _arguments(self, arguments: Sequence[str]) -> tuple[str, ...]:
        if arguments and arguments[0] == "codex":
            return (self._executable, *arguments[1:])
        return tuple(arguments)

    def status(self) -> ProviderStatus:
        available = bool(self._executable) and Path(self._executable).is_file() and os.access(self._executable, os.X_OK)
        return ProviderStatus("codex_cli", "configured", available, "available" if available else "codex unavailable")

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        if not self._executable:
            raise FileNotFoundError("Engineering Platform managed Codex CLI is unavailable")
        return subprocess.run((self._executable, *args), text=True, capture_output=True, check=False)

    def app_server(self) -> subprocess.Popen[str]:
        """Open the provider-owned interactive Codex app-server channel."""
        if not self._executable:
            raise FileNotFoundError("Engineering Platform managed Codex CLI is unavailable")
        return subprocess.Popen(
            (self._executable, "app-server"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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

    def invoke(
        self,
        root: Path,
        arguments: tuple[str, ...],
        *,
        timeout: float | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a complete Codex command; callers never spawn its CLI directly."""
        command = self._arguments(arguments)
        # Execution remains behind the provider boundary.  The current
        # Engineering callers stream long-running work through ``spawn`` and
        # do not supply a timeout here; retaining the argument preserves the
        # public provider contract for compatible callers.
        del timeout
        if environment is None and input_text is None:
            return self.execute(root, command)
        # The executable is this provider's configured Codex launcher, never a
        # caller-selected command. Remaining values are Codex CLI arguments.
        return subprocess.run(
            (self._executable, *command[1:]), cwd=root,
            env=dict(environment) if environment is not None else None,
            text=True, input=input_text, capture_output=True, check=False,
        )

    def spawn_invocation(
        self, root: Path, arguments: tuple[str, ...], *, environment: Mapping[str, str] | None = None
    ) -> subprocess.Popen[str]:
        command = self._arguments(arguments)
        if environment is None:
            return self.spawn(root, command)
        return subprocess.Popen(
            (self._executable, *command[1:]), cwd=root, env=dict(environment),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )


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

    def runtime_status(self, label: str) -> ProviderStatus:
        """Return whether one owned LaunchAgent has a live service process.

        ``launchctl print`` succeeding only proves that a job remains loaded.
        A KeepAlive job can be loaded while repeatedly exiting, so reporting it
        as healthy would project a stale "active" status to the dashboard.
        """
        executable = shutil.which("launchctl")
        if not executable:
            return ProviderStatus("launchd", "configured", False, "launchctl unavailable")
        completed = subprocess.run(
            (executable, "print", f"gui/{os.getuid()}/{label}"),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            return ProviderStatus("launchd", "configured", False, "LaunchAgent is not loaded")
        output = completed.stdout
        active_count = re.search(r"(?m)^\s*active count\s*=\s*(\d+)", output)
        has_active_process = (
            active_count is not None and int(active_count.group(1)) > 0
        ) or re.search(r"(?m)^\s*pid\s*=\s*[1-9]\d*", output) is not None
        if has_active_process:
            return ProviderStatus("launchd", "configured", True, "LaunchAgent process is active")
        return ProviderStatus(
            "launchd",
            "configured",
            False,
            "LaunchAgent is loaded but has no active process",
        )

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
