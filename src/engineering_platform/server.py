"""Standalone Engineering Platform Server foundation.

This module intentionally owns no project, Agent transport, credential, or
execution authority.  It is the installation-owned runtime boundary on which
those later capabilities can be composed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import http.server
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Protocol
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

from . import agent_trust


SERVER_CONFIGURATION_FILENAME = "server.json"
SERVER_IDENTITY_FILENAME = "runtime-identity.json"
SERVER_RUNTIME_FILENAME = "runtime.json"
SERVER_DATABASE_FILENAME = "ep-server.db"
SERVER_CONFIGURATION_VERSION = 1
SERVER_STORE_SCHEMA_VERSION = 1
SERVER_ENVIRONMENT_DATA_ROOT = "EP_SERVER_DATA_ROOT"
_CHILDREN: dict[int, subprocess.Popen[object]] = {}


class ServerConfigurationError(ValueError):
    """Raised when an installation-owned server configuration is invalid."""


@dataclass(frozen=True)
class ServerConfiguration:
    version: int
    bind_host: str
    bind_port: int

    @classmethod
    def load(cls, data_root: Path) -> "ServerConfiguration":
        path = data_root / SERVER_CONFIGURATION_FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ServerConfigurationError("EP Server configuration is unavailable.") from error
        if (
            not isinstance(raw, dict)
            or set(raw) != {"version", "bind_host", "bind_port"}
            or raw["version"] != SERVER_CONFIGURATION_VERSION
            or not isinstance(raw["bind_host"], str)
            or raw["bind_host"] != "127.0.0.1"
            or not isinstance(raw["bind_port"], int)
            or not 1 <= raw["bind_port"] <= 65535
        ):
            raise ServerConfigurationError("EP Server configuration is invalid.")
        return cls(raw["version"], raw["bind_host"], raw["bind_port"])


@dataclass(frozen=True)
class RuntimeIdentity:
    instance_id: str
    created_at: str


@dataclass(frozen=True)
class AgentRegistrationRequest:
    """Transport-neutral future Agent registration input.

    B3 deliberately does not define authentication, enrollment persistence,
    project attachment, or any network representation for this request.
    """

    agent_id: str
    agent_kind: str
    capabilities: tuple[str, ...]


class AgentRegistrationIntake(Protocol):
    """Future internal extension point; no transport/auth contract is implied."""

    def accept(self, request: AgentRegistrationRequest) -> None: ...


def default_data_root() -> Path:
    override = os.environ.get(SERVER_ENVIRONMENT_DATA_ROOT)
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Engineering Platform Server"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Engineering Platform Server"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "engineering-platform-server"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def initialize(data_root: Path, *, bind_host: str = "127.0.0.1", bind_port: int = 8765) -> RuntimeIdentity:
    """Create or validate an empty, installation-owned server instance."""
    data_root = data_root.resolve()
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = data_root / SERVER_CONFIGURATION_FILENAME
    if not config_path.exists():
        if bind_host != "127.0.0.1" or not 1 <= bind_port <= 65535:
            raise ServerConfigurationError("EP Server initial bind configuration is invalid.")
        _write_json(config_path, asdict(ServerConfiguration(SERVER_CONFIGURATION_VERSION, bind_host, bind_port)))
    ServerConfiguration.load(data_root)
    identity_path = data_root / SERVER_IDENTITY_FILENAME
    if identity_path.exists():
        try:
            raw = json.loads(identity_path.read_text(encoding="utf-8"))
            identity = RuntimeIdentity(str(raw["instance_id"]), str(raw["created_at"]))
            if not identity.instance_id or not identity.created_at:
                raise ValueError("empty identity")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ServerConfigurationError("EP Server runtime identity is invalid.") from error
    else:
        identity = RuntimeIdentity(str(uuid4()), _utcnow())
        _write_json(identity_path, asdict(identity))
    with sqlite3.connect(data_root / SERVER_DATABASE_FILENAME) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE IF NOT EXISTS ep_server_schema_migrations (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT OR IGNORE INTO ep_server_schema_migrations(version) VALUES(?)", (SERVER_STORE_SCHEMA_VERSION,))
        connection.execute("CREATE TABLE IF NOT EXISTS ep_server_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO ep_server_metadata(key,value) VALUES('instance_id',?)", (identity.instance_id,))
        agent_trust.install_schema(connection)
    (data_root / SERVER_DATABASE_FILENAME).chmod(0o600)
    return identity


def _runtime(data_root: Path) -> dict[str, object] | None:
    try:
        raw = json.loads((data_root / SERVER_RUNTIME_FILENAME).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status(data_root: Path) -> dict[str, object]:
    identity = initialize(data_root)
    config = ServerConfiguration.load(data_root)
    runtime = _runtime(data_root)
    running = bool(runtime and _alive(runtime.get("pid")))
    return {
        "service": "engineering-platform-server",
        "instance_id": identity.instance_id,
        "store": "ready",
        "operational_state": "empty-valid",
        "running": running,
        "bind": {"host": config.bind_host, "port": config.bind_port},
    }


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def _send(self, status_code: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("EP-Server-Instance", initialize(self.server.data_root).instance_id)  # type: ignore[attr-defined]
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/healthz", "/readyz"}:
            self.send_error(404)
            return
        self._send(200, status(self.server.data_root))  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802
        routes = {"/v1/agent/pair": agent_trust.pair, "/v1/agent/register": agent_trust.register, "/v1/agent/heartbeat": agent_trust.heartbeat}
        action = routes.get(self.path)
        if action is None:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 262144:
                raise agent_trust.AgentTrustError("request body is invalid")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            authorization = self.headers.get("Authorization", "")
            token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
            with sqlite3.connect(self.server.data_root / SERVER_DATABASE_FILENAME) as connection:  # type: ignore[attr-defined]
                result = action(connection, body) if action is agent_trust.pair else action(connection, body, token)
            self._send(200, result)
        except (ValueError, OSError, json.JSONDecodeError, agent_trust.AgentTrustError):
            self._send(400 if self.path == "/v1/agent/pair" else 401, {"error": "agent request rejected"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve(data_root: Path) -> int:
    identity = initialize(data_root)
    config = ServerConfiguration.load(data_root)
    server = http.server.ThreadingHTTPServer((config.bind_host, config.bind_port), _HealthHandler)
    server.data_root = data_root.resolve()  # type: ignore[attr-defined]
    _write_json(data_root / SERVER_RUNTIME_FILENAME, {"pid": os.getpid(), "instance_id": identity.instance_id, "started_at": _utcnow()})
    def stop(_signum: int, _frame: object) -> None:
        # ``shutdown`` must run outside the serve_forever thread.
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        (data_root / SERVER_RUNTIME_FILENAME).unlink(missing_ok=True)
    return 0


def start(data_root: Path) -> dict[str, object]:
    current = status(data_root)
    if current["running"]:
        return current
    child = subprocess.Popen([sys.executable, "-m", "engineering_platform.server", "serve", "--data-root", str(data_root.resolve())], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _CHILDREN[child.pid] = child
    for _ in range(40):
        time.sleep(0.05)
        current = status(data_root)
        if current["running"]:
            return current
    raise RuntimeError("EP Server did not become ready.")


def stop(data_root: Path) -> dict[str, object]:
    runtime = _runtime(data_root)
    if runtime and _alive(runtime.get("pid")):
        os.kill(int(runtime["pid"]), signal.SIGTERM)
        child = _CHILDREN.pop(int(runtime["pid"]), None)
        if child is not None:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for _ in range(40):
            time.sleep(0.05)
            if not _alive(runtime["pid"]):
                break
    (data_root / SERVER_RUNTIME_FILENAME).unlink(missing_ok=True)
    return status(data_root)


def health(data_root: Path) -> dict[str, object]:
    result = status(data_root)
    if not result["running"]:
        return {**result, "healthy": False, "ready": False}
    bind = result["bind"]
    try:
        with urlopen(f"http://{bind['host']}:{bind['port']}/readyz", timeout=1) as response:
            response.read()
        return {**result, "healthy": True, "ready": True}
    except (URLError, OSError):
        return {**result, "healthy": False, "ready": False}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engineering-platform-server", description="Manage the standalone Engineering Platform Server foundation")
    parser.add_argument("command", choices=("init", "start", "serve", "stop", "status", "health", "pairing-create", "agent-status", "agent-revoke", "agent-reset"))
    parser.add_argument("--data-root", type=Path, default=default_data_root())
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--bind-port", type=int, default=8765)
    parser.add_argument("--agent-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = {"instance_id": initialize(args.data_root, bind_host=args.bind_host, bind_port=args.bind_port).instance_id, "initialized": True}
        elif args.command == "start": result = start(args.data_root)
        elif args.command == "serve": return serve(args.data_root)
        elif args.command == "stop": result = stop(args.data_root)
        elif args.command == "status": result = status(args.data_root)
        elif args.command == "health": result = health(args.data_root)
        else:
            if not args.agent_id:
                raise ServerConfigurationError("--agent-id is required for Agent lifecycle commands.")
            initialize(args.data_root)
            with sqlite3.connect(args.data_root / SERVER_DATABASE_FILENAME) as connection:
                if args.command == "pairing-create": result = agent_trust.create_pairing_code(connection, args.agent_id)
                elif args.command == "agent-status": result = agent_trust.registration_status(connection, args.agent_id)
                elif args.command == "agent-revoke": result = {"agent_id": args.agent_id, "revoked": agent_trust.revoke(connection, args.agent_id)}
                else: result = {"agent_id": args.agent_id, "reset": agent_trust.reset(connection, args.agent_id)}
    except (OSError, RuntimeError, ServerConfigurationError) as error:
        print(json.dumps({"error": str(error), "ready": False}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ready", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
