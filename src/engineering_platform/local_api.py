"""Dedicated loopback-only, read-only Local Consumer API v1 service."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.request import urlopen

from .contracts.local_consumer_api import (
    ContractError,
    ErrorCode,
    ErrorEnvelope,
    RequestEnvelope,
    ResponseEnvelope,
)
from .local_api_credentials import CredentialAuthority
from .providers import LaunchdProvider
from .storage import open_storage

# The retired identity is recognized only to migrate an already installed
# Local API.  New installation and lifecycle ownership use LABEL exclusively.
LEGACY_LABEL = "com.djconnect.engineering-local-api"
LABEL = "com.engineeringplatform.local-api"
LOOPBACK_ADDRESS = "127.0.0.1"
DEFAULT_PORT = 8766
MAX_BODY_BYTES = 8192
REQUEST_TIMEOUT_SECONDS = 15


class LocalApiCutoverError(RuntimeError):
    """Fail closed when the exact Local API identity migration is unsafe."""


@dataclass(frozen=True)
class PreCutoverState:
    legacy_plist_present: bool
    legacy_loaded: bool
    legacy_process_active: bool
    legacy_functional_probe: bool | None


def valid_port(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1024 <= value <= 65535:
        raise ValueError("Local Consumer API port is invalid.")
    return value


def _error(
    handler: BaseHTTPRequestHandler,
    status: int,
    request_id: str | None = None,
    code: str = ErrorCode.MALFORMED_REQUEST,
) -> None:
    body = ErrorEnvelope(request_id, code).serialize().encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _contract_error(handler: BaseHTTPRequestHandler, status: int, error: ContractError) -> None:
    body = error.to_error_envelope().serialize().encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class LocalApiServer(ThreadingHTTPServer):
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(
        self, root: Path, port: int = DEFAULT_PORT, authority: CredentialAuthority | None = None
    ) -> None:
        self.root, self.authority = root, authority or CredentialAuthority(root)
        # Ephemeral port zero is test-only; production CLI/configuration still
        # goes through the bounded valid_port contract.
        if port != 0:
            port = valid_port(port)
        super().__init__((LOOPBACK_ADDRESS, port), LocalApiHandler)
        self.timeout = REQUEST_TIMEOUT_SECONDS

    def ready(self) -> bool:
        return readiness(self.root, self.authority)

    def handle_error(self, request: object, client_address: object) -> None:
        """Avoid socket-level tracebacks and never surface request data."""

        del request, client_address


def readiness(root: Path, authority: CredentialAuthority | None = None) -> bool:
    """Return readiness without creating a second listener."""

    try:
        connection = open_storage(root)
        connection.close()
    except Exception:
        return False
    return (authority or CredentialAuthority(root)).ready()


class LocalApiHandler(BaseHTTPRequestHandler):
    server: LocalApiServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: object) -> None:
        pass

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            _error(self, 404)
            return
        ready = self.server.ready()
        self._json(
            200 if ready else 503,
            {"health": "ok" if ready else "not_ready", "healthy": ready, "contract_version": "1.0"},
        )

    def do_POST(self) -> None:
        if self.path != "/v1/capabilities":
            _error(self, 404)
            return
        if self.headers.get_content_type() != "application/json":
            _error(self, 415)
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            size = -1
        if not 0 <= size <= MAX_BODY_BYTES:
            _error(self, 413)
            return
        try:
            envelope = RequestEnvelope.parse(json.loads(self.rfile.read(size)))
        except ContractError as error:
            _contract_error(self, 400, error)
            return
        except json.JSONDecodeError:
            _error(self, 400)
            return
        if envelope.payload:
            _error(self, 400, envelope.request_id, ErrorCode.MALFORMED_REQUEST)
            return
        if not self.server.ready():
            _error(self, 503, envelope.request_id, ErrorCode.SERVICE_NOT_READY)
            return
        header = self.headers.get("Authorization")
        if not isinstance(header, str) or not header.startswith("Bearer ") or not header[7:]:
            _error(self, 401, envelope.request_id, ErrorCode.UNAUTHENTICATED)
            return
        scope = self.server.authority.authenticate(header[7:])
        if scope is None:
            _error(self, 401, envelope.request_id, ErrorCode.UNAUTHENTICATED)
            return
        if not self.server.authority.authorized(scope):
            _error(self, 403, envelope.request_id, ErrorCode.PROJECT_NOT_AUTHORIZED)
            return
        if (
            scope.consumer_id != envelope.consumer.consumer_id
            or scope.project_id != envelope.project_id
        ):
            _error(self, 403, envelope.request_id, ErrorCode.PROJECT_NOT_AUTHORIZED)
            return
        response = ResponseEnvelope(
            envelope.request_id,
            {
                "contract_version": "1.0",
                "project_id": envelope.project_id,
                "capabilities": ["contract.foundation"],
                "read_only": True,
            },
        )
        self._json(200, response.to_dict())

    def do_PUT(self) -> None:
        _error(self, 405)

    def do_DELETE(self) -> None:
        _error(self, 405)

    def do_PATCH(self) -> None:
        _error(self, 405)

    def do_HEAD(self) -> None:
        _error(self, 405)

    def do_OPTIONS(self) -> None:
        _error(self, 405)


def run(root: Path, *, port: int = DEFAULT_PORT) -> None:
    LocalApiServer(root.resolve(), port).serve_forever(poll_interval=0.5)


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def legacy_launch_agent_path() -> Path:
    """Return the one retired Local API plist that may be migrated."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_LABEL}.plist"


def local_api_functional_probe(port: int = DEFAULT_PORT) -> bool:
    """Perform the bounded, token-free loopback health check used for commit."""
    try:
        with urlopen(f"http://{LOOPBACK_ADDRESS}:{valid_port(port)}/health", timeout=2) as response:  # noqa: S310 -- fixed loopback endpoint
            return 200 <= response.status < 300
    except (OSError, URLError):
        return False


def pre_cutover_state(
    launchd: LaunchdProvider, *, port: int = DEFAULT_PORT, probe=local_api_functional_probe
) -> PreCutoverState:
    """Capture non-secret legacy state so rollback restores it exactly."""
    loaded = launchd.inspect(LEGACY_LABEL)
    runtime = launchd.runtime_status(LEGACY_LABEL) if loaded else None
    active = bool(runtime and runtime.qualified)
    return PreCutoverState(
        legacy_launch_agent_path().is_file(), loaded, active, probe(port) if active else None
    )


def _unload_and_verify(launchd: LaunchdProvider, label: str, plist: Path, *, code: str) -> None:
    if not launchd.inspect(label):
        return
    launchd.uninstall(plist)
    if launchd.inspect(label):
        raise LocalApiCutoverError(code)


def _restore_legacy(
    launchd: LaunchdProvider,
    state: PreCutoverState,
    legacy: Path,
    neutral: Path,
    *,
    port: int,
    probe=local_api_functional_probe,
) -> None:
    """Restore exactly the captured legacy state only after neutral is absent."""
    _unload_and_verify(
        launchd, LABEL, neutral, code="ROLLBACK_NEUTRAL_UNLOAD_UNVERIFIED"
    )
    neutral.unlink(missing_ok=True)
    if state.legacy_loaded:
        if not state.legacy_plist_present:
            raise LocalApiCutoverError("ROLLBACK_LEGACY_PLIST_MISSING")
        if not launchd.inspect(LEGACY_LABEL):
            launchd.install(LEGACY_LABEL, legacy)
        if not launchd.inspect(LEGACY_LABEL):
            raise LocalApiCutoverError("ROLLBACK_LEGACY_LOAD_UNVERIFIED")
        restored = launchd.runtime_status(LEGACY_LABEL).qualified
        if restored != state.legacy_process_active:
            raise LocalApiCutoverError("ROLLBACK_LEGACY_PROCESS_STATE_MISMATCH")
        if state.legacy_functional_probe is True and not probe(port):
            raise LocalApiCutoverError("ROLLBACK_LEGACY_FUNCTIONAL_PROBE_FAILED")
    elif launchd.inspect(LEGACY_LABEL):
        raise LocalApiCutoverError("ROLLBACK_LEGACY_STATE_MISMATCH")


def launch_agent(repo: Path, port: int = DEFAULT_PORT) -> Path:
    destination = launch_agent_path()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array><string>{sys.executable}</string><string>-m</string><string>engineering_platform.local_api</string><string>run</string><string>--repo</string><string>{repo}</string><string>--port</string><string>{valid_port(port)}</string></array><key>WorkingDirectory</key><string>{repo}</string><key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>',
        encoding="utf-8",
    )
    return destination


def install(
    repo: Path, *, port: int = DEFAULT_PORT, probe=local_api_functional_probe
) -> Path:
    """Install the canonical Local API and retire its single migration source.

    The commit point requires legacy absence plus neutral loaded, active and
    functionally healthy proof. Before that point the legacy plist and its
    captured loaded state remain the sole rollback authority.
    """
    port = valid_port(port)
    launchd = LaunchdProvider()
    legacy = legacy_launch_agent_path()
    neutral = launch_agent_path()
    state = pre_cutover_state(launchd, port=port, probe=probe)
    if launchd.inspect(LABEL) or neutral.exists():
        raise LocalApiCutoverError("NEUTRAL_ALREADY_PRESENT")
    try:
        if state.legacy_loaded:
            _unload_and_verify(launchd, LEGACY_LABEL, legacy, code="LEGACY_UNLOAD_UNVERIFIED")
        plist = launch_agent(repo, port)
        launchd.install(LABEL, plist)
        if not launchd.inspect(LABEL):
            raise LocalApiCutoverError("NEUTRAL_LOAD_UNVERIFIED")
        if not launchd.runtime_status(LABEL).qualified:
            raise LocalApiCutoverError("NEUTRAL_PROCESS_UNHEALTHY")
        if not probe(port):
            raise LocalApiCutoverError("NEUTRAL_FUNCTIONAL_PROBE_FAILED")
    except LocalApiCutoverError as error:
        _restore_legacy(launchd, state, legacy, neutral, port=port, probe=probe)
        if str(error) == "LEGACY_UNLOAD_UNVERIFIED":
            raise
        raise LocalApiCutoverError("LOCAL_API_CUTOVER_ROLLED_BACK") from error
    except Exception as error:
        _restore_legacy(launchd, state, legacy, neutral, port=port, probe=probe)
        raise LocalApiCutoverError("LOCAL_API_CUTOVER_ROLLED_BACK") from error
    if state.legacy_plist_present:
        legacy.unlink()
    return plist


def uninstall() -> Path:
    """Remove only the canonical Local API LaunchAgent."""
    agent = launch_agent_path()
    LaunchdProvider().uninstall(agent)
    agent.unlink(missing_ok=True)
    return agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "install", "uninstall", "doctor"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if args.command == "run":
        run(repo, port=args.port)
        return 0
    if args.command == "install":
        install(repo, port=args.port)
        return 0
    if args.command == "uninstall":
        uninstall()
        return 0
    try:
        port = valid_port(args.port)
    except ValueError:
        ready = False
        port = args.port
    else:
        ready = readiness(repo)
    print(
        json.dumps(
            {
                "state": "READY" if ready else "NOT_READY",
                "bind": LOOPBACK_ADDRESS,
                "port": port,
                "contract_version": "1.0",
            },
            sort_keys=True,
        )
    )
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
