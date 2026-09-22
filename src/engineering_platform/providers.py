"""Qualified provider contracts and current local implementations.

Provider selection is configuration-owned.  These protocols deliberately expose
diagnostics only; they do not grant execution, repository or network authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
import os
import pwd
from pathlib import Path
import re
import selectors
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    version: str
    qualified: bool
    detail: str


@dataclass(frozen=True)
class LaunchdRuntimeDetails:
    """Host observation for one owned LaunchAgent, without control authority."""

    label: str
    loaded: bool
    active: bool
    pid: int | None
    last_exit_code: str | None
    memory_kib: int | None = None
    uptime_seconds: int | None = None


def _elapsed_seconds(value: str) -> int | None:
    """Parse macOS ``ps etime`` without treating malformed host output as fact."""
    match = re.fullmatch(r"(?:(\d+)-)?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})", value)
    if not match:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        return None
    return days * 86_400 + hours * 3_600 + minutes * 60 + seconds


class RuntimeProvider(Protocol):
    def status(self) -> ProviderStatus: ...


class ProcessProvider(Protocol):
    """The sole boundary for local child-process execution."""

    def execute(
        self, root: Path, arguments: Sequence[str], *, environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...

    def spawn(self, root: Path, arguments: Sequence[str]) -> subprocess.Popen[str]: ...

    def spawn_detached(self, root: Path, arguments: Sequence[str], environment: Mapping[str, str]) -> subprocess.Popen[bytes]: ...


class LocalProcessProvider:
    """Default local process adapter; orchestration code never imports subprocess for work."""

    def execute(
        self, root: Path, arguments: Sequence[str], *, environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            arguments, cwd=root, env=dict(environment) if environment is not None else None,
            text=True, capture_output=True, check=False,
        )

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


def installed_python_environment() -> dict[str, str]:
    """Return the Server-owned interpreter environment for child validation.

    An installed Engineering Platform Server is executed by its installation
    virtual environment.  Child validation must retain that interpreter when a
    repository asks for the conventional ``python -m ...`` command; otherwise
    it can accidentally resolve a different system Python.
    """
    # Keep the venv launcher path intact.  Resolving it selects the framework
    # base interpreter and lets a child validation command escape the EP venv.
    executable = Path(sys.executable).expanduser().absolute()
    environment = dict(os.environ)
    environment["PATH"] = str(executable.parent) + os.pathsep + environment.get("PATH", "")
    virtual_environment = executable.parent.parent
    if (virtual_environment / "pyvenv.cfg").is_file():
        environment["VIRTUAL_ENV"] = str(virtual_environment)
    return environment


VALIDATION_SCRATCH_UNAVAILABLE = "VALIDATION_SCRATCH_UNAVAILABLE"
VALIDATION_SCRATCH_LOST = "VALIDATION_SCRATCH_LOST"


class ValidationEnvironmentError(RuntimeError):
    """A bounded validation child cannot use its EP-owned scratch directory."""

    def __init__(self, diagnostic_code: str) -> None:
        super().__init__(diagnostic_code)
        self.diagnostic_code = diagnostic_code


class ValidationScratch:
    """One private, short-lived filesystem boundary for a validation child.

    The parent belongs to the already selected EP data root.  Each direct
    validation subprocess gets a unique child directory, so neither a target
    checkout nor a system-wide temporary location becomes execution state.
    """

    def __init__(self, parent: Path, *, run_id: str) -> None:
        self.parent = parent
        self.run_id = run_id
        self.directory: Path | None = None
        self._base_fd: int | None = None
        self._directory_name: str | None = None
        self._directory_identity: os.stat_result | None = None

    @staticmethod
    def _safe_prefix(run_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", run_id)[:64]
        return f"validation-{safe or 'run'}-"

    @staticmethod
    def _probe(directory: Path) -> None:
        """Prove precisely the operations the child validation may need."""
        created = directory / ".ep-validation-probe"
        renamed = directory / ".ep-validation-probe-renamed"
        database = directory / ".ep-validation-probe.sqlite"
        connection: sqlite3.Connection | None = None
        descriptor: int | None = None
        try:
            descriptor = os.open(created, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, b"engineering-platform-validation\n")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if created.read_bytes() != b"engineering-platform-validation\n":
                raise OSError("validation scratch readback mismatch")
            os.replace(created, renamed)
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE validation_probe(value TEXT NOT NULL)")
            connection.execute("INSERT INTO validation_probe(value) VALUES('ok')")
            connection.commit()
            row = connection.execute("SELECT value FROM validation_probe").fetchone()
            if row != ("ok",):
                raise OSError("validation scratch sqlite readback mismatch")
        except (OSError, sqlite3.Error) as error:
            raise ValidationEnvironmentError(VALIDATION_SCRATCH_UNAVAILABLE) from error
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if connection is not None:
                connection.close()
            # These exact paths were created only inside this unique directory.
            for path in (
                created, renamed, database,
                database.with_name(database.name + "-journal"),
                database.with_name(database.name + "-wal"),
                database.with_name(database.name + "-shm"),
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def __enter__(self) -> Path:
        base = self.parent / "validation-scratch"
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            if base.is_symlink() or not base.is_dir():
                raise OSError("validation scratch parent is not a directory")
            flags = os.O_RDONLY | os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            self._base_fd = os.open(base, flags)
            if not os.path.samestat(os.lstat(base), os.fstat(self._base_fd)):
                raise OSError("validation scratch parent changed while opening")
            self.directory = Path(tempfile.mkdtemp(prefix=self._safe_prefix(self.run_id), dir=base))
            self._directory_name = self.directory.name
            self._directory_identity = os.stat(self.directory, follow_symlinks=False)
            self._probe(self.directory)
            return self.directory
        except ValidationEnvironmentError:
            self.__exit__(None, None, None)
            raise
        except OSError as error:
            self.__exit__(None, None, None)
            raise ValidationEnvironmentError(VALIDATION_SCRATCH_UNAVAILABLE) from error

    def __exit__(self, *_: object) -> None:
        base_fd, self._base_fd = self._base_fd, None
        directory_name, self._directory_name = self._directory_name, None
        identity, self._directory_identity = self._directory_identity, None
        self.directory = None
        if base_fd is None:
            return
        try:
            if directory_name is None or identity is None:
                return
            # Address the leaf through the parent fd held before launching the
            # child. A child may rename the parent or replace its path with a
            # symlink, but that cannot redirect this cleanup outside the
            # original EP-owned directory. shutil.rmtree's fd implementation
            # also refuses a leaf swapped for a symlink after this check.
            observed = os.stat(directory_name, dir_fd=base_fd, follow_symlinks=False)
            if stat.S_ISDIR(observed.st_mode) and os.path.samestat(observed, identity):
                try:
                    shutil.rmtree(directory_name, dir_fd=base_fd)
                except OSError:
                    # The child may already have removed or replaced its own
                    # leaf. Never chase an unowned replacement for cleanup.
                    pass
        except FileNotFoundError:
            pass
        finally:
            os.close(base_fd)

    def is_current_directory(self) -> bool:
        """Whether the original scratch leaf still exists beneath its held fd."""
        if self._base_fd is None or self._directory_name is None or self._directory_identity is None:
            return False
        try:
            observed = os.stat(
                self._directory_name, dir_fd=self._base_fd, follow_symlinks=False,
            )
        except OSError:
            return False
        return stat.S_ISDIR(observed.st_mode) and os.path.samestat(
            observed, self._directory_identity,
        )


def validation_child_environment(root: Path, scratch: Path) -> dict[str, str]:
    """Build the exact child environment after the scratch preflight passes."""
    environment = installed_python_environment()
    source = root / "src"
    import_root = source if source.is_dir() else root
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(import_root) + (os.pathsep + inherited if inherited else "")
    for name in ("TMPDIR", "TMP", "TEMP"):
        environment[name] = str(scratch)
    return environment


class DeterministicValidationExecutor:
    """Run one resolved validation control outside provider-agent dispatch."""

    def __init__(self, process: ProcessProvider | None = None) -> None:
        self.process = process or LocalProcessProvider()

    def run(
        self, root: Path, command: tuple[str, ...], *, scratch_parent: Path | None = None,
        run_id: str | None = None,
    ) -> "DeterministicValidationResult":
        try:
            if scratch_parent is None:
                completed = self.process.execute(root, command, environment=installed_python_environment())
            else:
                scratch_area = ValidationScratch(scratch_parent, run_id=run_id or "validation")
                with scratch_area as scratch:
                    environment = validation_child_environment(root, scratch)
                    if not scratch_area.is_current_directory():
                        raise ValidationEnvironmentError(VALIDATION_SCRATCH_LOST)
                    completed = self.process.execute(
                        root, command, environment=environment,
                    )
                    if not scratch_area.is_current_directory():
                        raise ValidationEnvironmentError(VALIDATION_SCRATCH_LOST)
            stdout = completed.stdout
            stderr = completed.stderr
            return DeterministicValidationResult(
                exit_code=completed.returncode,
                stdout=stdout if isinstance(stdout, str) else None,
                stderr=stderr if isinstance(stderr, str) else None,
                diagnostic_capture_available=isinstance(stdout, str) and isinstance(stderr, str),
            )
        except ValidationEnvironmentError as error:
            return DeterministicValidationResult(
                exit_code=None, stdout=None, stderr=None,
                diagnostic_capture_available=False, infrastructure_diagnostic=error.diagnostic_code,
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
    infrastructure_diagnostic: str | None = None


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


MANAGED_CODEX_CLI_PREFIX_ENVIRONMENT = "EP_MANAGED_CODEX_CLI_PREFIX"
MANAGED_GITHUB_CLI_EXECUTABLE_ENVIRONMENT = "EP_GITHUB_CLI_EXECUTABLE"


def default_engineering_platform_codex_cli_prefix() -> Path:
    """Return the stable account-owned default, independent of ``$HOME``.

    A runner may deliberately receive an isolated HOME for tool state.  That
    must never manufacture a second EP-managed CLI installation location.
    """
    try:
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        account_home = Path.home()
    return account_home / ".local" / "share" / "engineering-platform" / "codex-cli"


def engineering_platform_codex_cli_prefix() -> Path:
    """Return the installation-pinned CLI prefix, never a process HOME path."""
    configured = os.environ.get(MANAGED_CODEX_CLI_PREFIX_ENVIRONMENT)
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate.resolve(strict=False)
    return default_engineering_platform_codex_cli_prefix()


def codex_cli_executable() -> str | None:
    """Return only Engineering Platform's managed Codex CLI executable."""
    managed = engineering_platform_codex_cli_prefix() / "bin" / "codex"
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
    return None


def github_cli_executable() -> str | None:
    """Select an instance-owned gh executable when the service pins one.

    User-owned Project Agents retain their ordinary user/PATH behavior.  A
    system Server LaunchDaemon always receives the environment variable and
    therefore fails closed instead of falling back when its owned executable
    is missing or substituted.
    """
    configured = os.environ.get(MANAGED_GITHUB_CLI_EXECUTABLE_ENVIRONMENT)
    if configured is not None:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.absolute())
        return None
    return shutil.which("gh")


class ProviderOutputLimitExceeded(RuntimeError):
    """An advisory provider invocation exceeded its retained output budget."""


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

    def command(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if not self._executable:
            raise FileNotFoundError("Engineering Platform managed Codex CLI is unavailable")
        return subprocess.run((self._executable, *args), text=True, capture_output=True, check=False, timeout=timeout)

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
        max_output_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a complete Codex command; callers never spawn its CLI directly."""
        command = self._arguments(arguments)
        # Reviewer invocations are bounded advisory work.  Primary execution
        # streams through ``spawn`` and does not supply a timeout here.
        if max_output_bytes is not None:
            return self._invoke_with_output_limit(
                root, command, timeout=timeout, environment=environment,
                input_text=input_text, max_output_bytes=max_output_bytes,
            )
        if timeout is None and environment is None and input_text is None:
            return self.execute(root, command)
        # The executable is this provider's configured Codex launcher, never a
        # caller-selected command. Remaining values are Codex CLI arguments.
        return subprocess.run(
            (self._executable, *command[1:]), cwd=root,
            env=dict(environment) if environment is not None else None, timeout=timeout,
            text=True, input=input_text, capture_output=True, check=False,
        )

    def _invoke_with_output_limit(
        self, root: Path, command: tuple[str, ...], *, timeout: float | None,
        environment: Mapping[str, str] | None, input_text: str | None,
        max_output_bytes: int,
    ) -> subprocess.CompletedProcess[str]:
        """Capture bounded advisory output; terminate only this invocation's group.

        Nonblocking pipes keep both the byte budget and deadline effective even
        when the child floods stderr or never consumes its input. This opt-in
        path does not alter primary execution or other existing provider users.
        """
        if max_output_bytes < 1:
            raise ValueError("Provider output limit must be positive")
        arguments = (self._executable, *command[1:])
        pending_input = memoryview(input_text.encode("utf-8") if input_text is not None else b"")
        output = {"stdout": bytearray(), "stderr": bytearray()}
        retained = 0
        with subprocess.Popen(
            arguments, cwd=root, env=dict(environment) if environment is not None else None,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        ) as process, selectors.DefaultSelector() as streams:
            deadline = time.monotonic() + timeout if timeout is not None else None
            try:
                for name in output:
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    streams.register(stream, selectors.EVENT_READ, name)
                if process.stdin is not None:
                    if pending_input:
                        os.set_blocking(process.stdin.fileno(), False)
                        streams.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                    else:
                        process.stdin.close()
                while streams.get_map():
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise subprocess.TimeoutExpired(arguments, timeout)
                    for key, _ in streams.select(remaining):
                        if key.data == "stdin":
                            try:
                                pending_input = pending_input[os.write(key.fd, pending_input[:4096]):]
                            except BrokenPipeError:
                                pending_input = memoryview(b"")
                            if not pending_input:
                                streams.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        chunk = os.read(key.fd, min(65_536, max_output_bytes - retained + 1))
                        if not chunk:
                            streams.unregister(key.fileobj)
                            continue
                        retained += len(chunk)
                        if retained > max_output_bytes:
                            raise ProviderOutputLimitExceeded("Provider output byte limit exceeded")
                        output[key.data].extend(chunk)
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                process.wait(timeout=remaining)
            finally:
                # This session was created above for this bounded invocation;
                # never target another provider or the EP server's process group.
                # Clean up descendants even if their launcher exits successfully
                # after closing its pipes. The direct child is always reaped.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        return subprocess.CompletedProcess(
            arguments, process.returncode,
            output["stdout"].decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"),
            output["stderr"].decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"),
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

    def clone_branch(self, root: Path, origin: str, branch: str, destination: Path, *, timeout: int = 120) -> None:
        """Create one bounded, disposable checkout for a pinned remote review."""
        completed = subprocess.run(
            ("git", "clone", "--quiet", "--single-branch", "--no-tags", "--branch", branch,
             origin, str(destination)), cwd=root, text=True, capture_output=True,
            check=False, timeout=timeout,
        )
        if completed.returncode:
            raise RuntimeError("Pinned Git review checkout could not be created.")


class GitHubProvider:
    def status(self, root: Path) -> ProviderStatus:
        remote = GitProvider().execute(root, "git", "remote", "get-url", "origin")
        qualified = remote.returncode == 0 and "github" in remote.stdout.lower()
        return ProviderStatus("github", "configured", qualified, remote.stdout.strip() if qualified else "GitHub origin unavailable")

    def github(self, *args: str) -> str:
        executable = github_cli_executable()
        if executable is None:
            raise RuntimeError("Engineering Platform GitHub CLI is unavailable")
        completed = subprocess.run((executable, *args), text=True, capture_output=True, check=False)
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
        details = self.runtime_details(label)
        if not details.loaded:
            if shutil.which("launchctl") is None:
                return ProviderStatus("launchd", "configured", False, "launchctl unavailable")
            return ProviderStatus("launchd", "configured", False, "LaunchAgent is not loaded")
        if details.active:
            return ProviderStatus("launchd", "configured", True, "LaunchAgent process is active")
        return ProviderStatus(
            "launchd",
            "configured",
            False,
            "LaunchAgent is loaded but has no active process",
        )

    def runtime_details(self, label: str) -> LaunchdRuntimeDetails:
        """Read the actual LaunchAgent host state used by component detail views."""
        executable = shutil.which("launchctl")
        if not executable:
            return LaunchdRuntimeDetails(label, False, False, None, None)
        completed = subprocess.run(
            (executable, "print", f"gui/{os.getuid()}/{label}"),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            return LaunchdRuntimeDetails(label, False, False, None, None)
        output = completed.stdout
        active_count = re.search(r"(?m)^\s*active count\s*=\s*(\d+)", output)
        pid_match = re.search(r"(?m)^\s*pid\s*=\s*([1-9]\d*)", output)
        last_exit = re.search(r"(?m)^\s*last exit code\s*=\s*(.+)$", output)
        pid = int(pid_match.group(1)) if pid_match else None
        active = (active_count is not None and int(active_count.group(1)) > 0) or pid is not None
        memory_kib, uptime_seconds = self._process_metrics(pid) if active and pid else (None, None)
        return LaunchdRuntimeDetails(
            label, True, active, pid,
            last_exit.group(1).strip() if last_exit else None,
            memory_kib, uptime_seconds,
        )

    @staticmethod
    def _process_metrics(pid: int) -> tuple[int | None, int | None]:
        """Read memory and elapsed time for a launchd-proven process only."""
        executable = shutil.which("ps")
        if not executable:
            return None, None
        completed = subprocess.run(
            (executable, "-o", "rss=", "-o", "etime=", "-p", str(pid)),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            return None, None
        columns = completed.stdout.strip().split()
        if len(columns) != 2 or not columns[0].isdigit():
            return None, None
        return int(columns[0]), _elapsed_seconds(columns[1])

    def restart(self, label: str) -> None:
        executable = shutil.which("launchctl")
        if not executable:
            raise OSError("launchctl unavailable")
        completed = subprocess.run((executable, "kickstart", "-k", f"gui/{__import__('os').getuid()}/{label}"), text=True, capture_output=True, check=False)
        if completed.returncode:
            raise OSError(completed.stderr.strip() or "launchd restart failed")

    def quiesce(self, label: str, plist: Path) -> None:
        """Temporarily unload one owned LaunchAgent for bounded maintenance."""
        executable = shutil.which("launchctl")
        if not executable:
            raise OSError("launchctl unavailable")
        completed = subprocess.run(
            (executable, "bootout", f"gui/{os.getuid()}", str(plist)),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise OSError(completed.stderr.strip() or "launchd quiesce failed")
        # A process signal is not enough: both observations must prove launchd
        # no longer owns a runnable job, preventing KeepAlive replacement.
        if self.inspect(label) or self.inspect(label):
            raise OSError("LaunchAgent remained loaded after maintenance quiesce")

    def resume(self, label: str, plist: Path) -> None:
        """Reload a temporarily quiesced owned LaunchAgent and start it."""
        executable = shutil.which("launchctl")
        if not executable:
            raise OSError("launchctl unavailable")
        completed = subprocess.run(
            (executable, "bootstrap", f"gui/{os.getuid()}", str(plist)),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise OSError(completed.stderr.strip() or "launchd resume failed")
        self.restart(label)


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
