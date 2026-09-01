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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from . import agent_trust
from .repository_attachment import RepositoryAttachmentError, load_repository_attachment


IDENTITY_FORMAT = "engineering-platform-project-agent/v1"
DEFAULT_TOOLCHAINS = ("python3", "node", "npm", "go", "cargo", "rustc", "java", "docker", "podman")
DEFAULT_PROVIDER_CLIS = ("gh", "glab", "az", "codex")
AGENT_CONFIGURATION_VERSION = 1


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


def default_configuration_path() -> Path:
    return default_identity_path().with_name("project-agent-server.json")


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _post(endpoint: str, route: str, body: dict[str, object], token: str | None = None) -> tuple[dict[str, object], str]:
    if not endpoint.startswith("http://127.0.0.1:") and not endpoint.startswith("http://localhost:"):
        raise ValueError("insecure non-loopback Server endpoint is forbidden")
    headers = {"Content-Type": "application/json"}
    if token: headers["Authorization"] = f"Bearer {token}"
    request = Request(endpoint.rstrip("/") + route, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=5) as response:
            instance = response.headers.get("EP-Server-Instance")
            raw = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as error:
        raise ValueError("EP Server request was rejected or unavailable") from error
    if not isinstance(raw, dict) or not isinstance(instance, str) or not instance:
        raise ValueError("EP Server identity response is invalid")
    return raw, instance


def _attachment_reports(repository_roots: Sequence[Path]) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for root in repository_roots:
        try:
            reports.append({"attachment": load_repository_attachment(root).agent_read_surface()})
        except RepositoryAttachmentError:
            continue
    return reports


def pair(endpoint: str, pairing_code: str, *, identity_path: Path | None = None, configuration_path: Path | None = None) -> dict[str, str]:
    snapshot = observe((), identity_path=identity_path)
    response, instance = _post(endpoint, "/v1/agent/pair", {"protocol_version": agent_trust.PROTOCOL_VERSION, "agent_id": snapshot.identity.agent_id, "pairing_code": pairing_code})
    credential = response.get("credential")
    if not isinstance(credential, str) or response.get("agent_id") != snapshot.identity.agent_id:
        raise ValueError("EP Server pairing response is invalid")
    _write_private_json(configuration_path or default_configuration_path(), {"version": AGENT_CONFIGURATION_VERSION, "endpoint": endpoint.rstrip("/"), "server_instance_id": instance, "agent_id": snapshot.identity.agent_id, "credential": credential})
    return {"agent_id": snapshot.identity.agent_id, "server_instance_id": instance, "paired": "true"}


def _configuration(path: Path | None = None) -> dict[str, str]:
    try:
        raw = json.loads((path or default_configuration_path()).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Agent pairing configuration is unavailable") from error
    if not isinstance(raw, dict) or set(raw) != {"version", "endpoint", "server_instance_id", "agent_id", "credential"} or raw.get("version") != AGENT_CONFIGURATION_VERSION or not all(isinstance(raw.get(k), str) and raw[k] for k in ("endpoint", "server_instance_id", "agent_id", "credential")):
        raise ValueError("Agent pairing configuration is invalid")
    return raw  # type: ignore[return-value]


def register(repository_roots: Sequence[Path] = (), *, identity_path: Path | None = None, configuration_path: Path | None = None) -> dict[str, object]:
    config, snapshot = _configuration(configuration_path), observe(repository_roots, identity_path=identity_path)
    if config["agent_id"] != snapshot.identity.agent_id:
        raise ValueError("Agent installation identity differs from pairing configuration")
    capabilities = snapshot.payload()["capabilities"]
    response, instance = _post(config["endpoint"], "/v1/agent/register", {"protocol_version": agent_trust.PROTOCOL_VERSION, "agent_id": snapshot.identity.agent_id, "host": asdict(snapshot.capabilities.host), "capabilities": capabilities, "repositories": _attachment_reports(repository_roots)}, config["credential"])
    if instance != config["server_instance_id"]:
        raise ValueError("EP Server identity changed; re-pair explicitly")
    return response


def heartbeat(*, configuration_path: Path | None = None) -> dict[str, object]:
    config = _configuration(configuration_path)
    response, instance = _post(config["endpoint"], "/v1/agent/heartbeat", {"protocol_version": agent_trust.PROTOCOL_VERSION, "agent_id": config["agent_id"]}, config["credential"])
    if instance != config["server_instance_id"]:
        raise ValueError("EP Server identity changed; re-pair explicitly")
    return response


def attach(repository_root: Path, *, identity_path: Path | None = None, configuration_path: Path | None = None) -> dict[str, object]:
    """Read one explicit root and register only its validated declaration.

    The checkout path remains local to the Agent and is intentionally absent
    from the request and Server topology.
    """
    config, snapshot = _configuration(configuration_path), observe((), identity_path=identity_path)
    if config["agent_id"] != snapshot.identity.agent_id:
        raise ValueError("Agent installation identity differs from pairing configuration")
    try:
        declaration = load_repository_attachment(repository_root).agent_read_surface()
    except RepositoryAttachmentError as error:
        raise ValueError("Repository attachment declaration is unavailable or invalid") from error
    response, instance = _post(config["endpoint"], "/v1/agent/attachment", {"protocol_version": agent_trust.PROTOCOL_VERSION, "agent_id": snapshot.identity.agent_id, "attachment": declaration, "availability": "AVAILABLE"}, config["credential"])
    if instance != config["server_instance_id"]:
        raise ValueError("EP Server identity changed; re-pair explicitly")
    return response


def load_or_create_identity(host: HostIdentity, path: Path | None = None) -> AgentIdentity:
    """Persist only installation identity; never execution, queue, or lock data."""
    identity_path = path or default_identity_path()
    try:
        raw = json.loads(identity_path.read_text(encoding="utf-8"))
        identity = AgentIdentity(**raw)
        if identity.identity_format == IDENTITY_FORMAT and identity.host_context_key == host.context_key:
            # Identity metadata is not a credential, but it is still per-user
            # installation state and must not be readable by other accounts.
            identity_path.chmod(0o600)
            return identity
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    identity = AgentIdentity(str(uuid4()), IDENTITY_FORMAT, host.context_key, datetime.now(timezone.utc).isoformat())
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = identity_path.with_suffix(identity_path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(identity), sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, identity_path)
    identity_path.chmod(0o600)
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
    parser = argparse.ArgumentParser(prog="engineering-project-agent", description="Observe or pair the Project Agent with EP Server.")
    parser.add_argument("command", choices=("observe", "pair", "register", "heartbeat", "attach"), nargs="?", default="observe")
    parser.add_argument("--repository-root", action="append", default=[], type=Path, help="Repository root to inspect; may be repeated.")
    parser.add_argument("--identity-path", type=Path, help="Local installation identity file; contains no execution state.")
    parser.add_argument("--configuration-path", type=Path, help="Private Agent pairing configuration path.")
    parser.add_argument("--server-endpoint")
    parser.add_argument("--pairing-code")
    return parser


def main(argv: list[str] | None = None) -> int:
    supplied = list(argv) if argv is not None else sys.argv[1:]
    if supplied and supplied[0] in {"install", "uninstall", "start", "stop", "restart", "status", "service"}:
        return service_main(supplied)
    parser = build_parser()
    args = parser.parse_args(supplied)
    if args.command == "observe": result = observe(args.repository_root, identity_path=args.identity_path).payload()
    elif args.command == "pair":
        if not args.server_endpoint or not args.pairing_code: parser.error("pair requires --server-endpoint and --pairing-code")
        result = pair(args.server_endpoint, args.pairing_code, identity_path=args.identity_path, configuration_path=args.configuration_path)
    elif args.command == "register": result = register(args.repository_root, identity_path=args.identity_path, configuration_path=args.configuration_path)
    elif args.command == "heartbeat": result = heartbeat(configuration_path=args.configuration_path)
    else:
        if len(args.repository_root) != 1: parser.error("attach requires exactly one --repository-root")
        result = attach(args.repository_root[0], identity_path=args.identity_path, configuration_path=args.configuration_path)
    print(json.dumps(result, indent=2, sort_keys=True))
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
