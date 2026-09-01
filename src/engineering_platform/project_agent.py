"""Standalone, local-only Project Agent foundation.

The Project Agent observes one Host/OS-user context and zero or more explicit
repository roots.  It is an execution edge only: no durable execution state,
admission, scheduling, locking, or Server authority belongs here.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Protocol, Sequence
from uuid import uuid4


IDENTITY_FORMAT = "engineering-platform-project-agent/v1"
DEFAULT_TOOLCHAINS = ("python3", "node", "npm", "go", "cargo", "rustc", "java", "docker", "podman")
DEFAULT_PROVIDER_CLIS = ("gh", "glab", "az", "codex")


@dataclass(frozen=True)
class HostIdentity:
    """Observed Host/OS-user boundary; this is not a pairing credential."""

    hostname: str
    os_user: str
    operating_system: str
    architecture: str

    @property
    def context_key(self) -> str:
        return ":".join((self.hostname, self.os_user, self.operating_system, self.architecture))


@dataclass(frozen=True)
class AgentIdentity:
    """Stable local installation identity reserved for B6 pairing."""

    agent_id: str
    identity_format: str
    host_context_key: str
    created_at: str


@dataclass(frozen=True)
class ToolCapability:
    name: str
    available: bool
    path: str | None
    version: str | None


@dataclass(frozen=True)
class CapabilitySnapshot:
    host: HostIdentity
    git: ToolCapability
    toolchains: tuple[ToolCapability, ...]
    provider_clis: tuple[ToolCapability, ...]


@dataclass(frozen=True)
class RepositoryEnvironment:
    """A local repository observation, not an attachment or execution claim."""

    requested_root: str
    resolved_root: str | None
    exists: bool
    is_git_repository: bool


@dataclass(frozen=True)
class AgentSnapshot:
    identity: AgentIdentity
    capabilities: CapabilitySnapshot
    repositories: tuple[RepositoryEnvironment, ...]
    observed_at: str

    def payload(self) -> dict[str, object]:
        value = asdict(self)
        value["capabilities"]["host"]["context_key"] = self.capabilities.host.context_key
        return value


class AgentControlPlaneClient(Protocol):
    """B6 placeholder: transport and pairing are intentionally unspecified."""

    def publish_observation(self, snapshot: AgentSnapshot) -> None: ...


def observe_host_identity() -> HostIdentity:
    return HostIdentity(
        hostname=platform.node() or "unknown-host",
        os_user=getpass.getuser() or "unknown-user",
        operating_system=platform.system() or "unknown-os",
        architecture=platform.machine() or "unknown-architecture",
    )


def default_identity_path() -> Path:
    configured = os.environ.get("ENGINEERING_PLATFORM_AGENT_IDENTITY_PATH")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Engineering Platform" / "Project Agent" / "identity.json"
    return Path.home() / ".config" / "engineering-platform" / "project-agent-identity.json"


def load_or_create_identity(host: HostIdentity, path: Path | None = None) -> AgentIdentity:
    """Persist only installation identity; never execution, queue, or lock data."""
    identity_path = path or default_identity_path()
    try:
        raw = json.loads(identity_path.read_text(encoding="utf-8"))
        identity = AgentIdentity(**raw)
        if identity.identity_format == IDENTITY_FORMAT and identity.host_context_key == host.context_key:
            return identity
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    identity = AgentIdentity(str(uuid4()), IDENTITY_FORMAT, host.context_key, datetime.now(timezone.utc).isoformat())
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = identity_path.with_suffix(identity_path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(identity), sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, identity_path)
    return identity


def _version(executable: str) -> str | None:
    try:
        result = subprocess.run((executable, "--version"), capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0][:200] if output else None


def discover_tool(name: str) -> ToolCapability:
    executable = shutil.which(name)
    return ToolCapability(name, executable is not None, executable, _version(executable) if executable else None)


def discover_capabilities() -> CapabilitySnapshot:
    return CapabilitySnapshot(
        host=observe_host_identity(),
        git=discover_tool("git"),
        toolchains=tuple(discover_tool(name) for name in DEFAULT_TOOLCHAINS),
        provider_clis=tuple(discover_tool(name) for name in DEFAULT_PROVIDER_CLIS),
    )


def inventory_repositories(roots: Sequence[Path]) -> tuple[RepositoryEnvironment, ...]:
    """Inspect only explicitly supplied roots; discovery never creates attachments."""
    inventory: list[RepositoryEnvironment] = []
    for requested in roots:
        resolved = requested.expanduser().resolve()
        exists = resolved.is_dir()
        is_git = False
        if exists:
            try:
                result = subprocess.run(("git", "-C", str(resolved), "rev-parse", "--is-inside-work-tree"), capture_output=True, text=True, timeout=2, check=False)
                is_git = result.returncode == 0 and result.stdout.strip() == "true"
            except (OSError, subprocess.SubprocessError):
                pass
        inventory.append(RepositoryEnvironment(str(requested), str(resolved) if exists else None, exists, is_git))
    return tuple(inventory)


def observe(repository_roots: Sequence[Path] = (), *, identity_path: Path | None = None) -> AgentSnapshot:
    capabilities = discover_capabilities()
    return AgentSnapshot(
        identity=load_or_create_identity(capabilities.host, identity_path),
        capabilities=capabilities,
        repositories=inventory_repositories(repository_roots),
        observed_at=datetime.now(timezone.utc).isoformat(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engineering-project-agent", description="Observe local Project Agent capabilities without connecting to EP Server.")
    parser.add_argument("--repository-root", action="append", default=[], type=Path, help="Repository root to inspect; may be repeated.")
    parser.add_argument("--identity-path", type=Path, help="Local installation identity file; contains no execution state.")
    return parser


def main(argv: list[str] | None = None) -> int:
    supplied = list(argv) if argv is not None else sys.argv[1:]
    if supplied and supplied[0] in {"install", "uninstall", "start", "stop", "restart", "status", "service"}:
        return service_main(supplied)
    args = build_parser().parse_args(supplied)
    print(json.dumps(observe(args.repository_root, identity_path=args.identity_path).payload(), indent=2, sort_keys=True))
    return 0


def service_main(argv: list[str]) -> int:
    """Dispatch packaging lifecycle commands without changing B4 observation CLI."""
    from . import project_agent_service as service
    parser = argparse.ArgumentParser(prog="engineering-project-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "uninstall", "start", "stop", "restart", "status"):
        commands.add_parser(name)
    run_parser = commands.add_parser("service")
    run_commands = run_parser.add_subparsers(dest="service_command", required=True)
    run = run_commands.add_parser("run")
    run.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            # Entry-point invocation is the authoritative installed artifact;
            # consulting PATH here could select a different developer install.
            result: object = service.install(executable=Path(sys.argv[0]))
        elif args.command == "uninstall":
            service.uninstall()
            result = {"state": "uninstalled"}
        elif args.command == "start":
            service.start()
            result = service.status()
        elif args.command == "stop":
            service.stop()
            result = service.status()
        elif args.command == "restart":
            service.stop()
            service.start()
            result = service.status()
        elif args.command == "status":
            result = service.status()
        else:
            return service.run(args.config)
    except service.AgentServiceError as error:
        parser.error(str(error))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
