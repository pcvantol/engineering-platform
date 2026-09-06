"""Historical Local Consumer API contract fixture; not an EP service.

The current product's supported submission ingress is the Server HTTP JSON
endpoint, installed CLI and Server-owned File Inbox.  This module retains the
small read-only contract fixture needed by provenance and regression tests; it
does not provide an install, LaunchAgent, lifecycle, repair or health surface.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from .contracts.local_consumer_api import (
    ContractError,
    ErrorCode,
    ErrorEnvelope,
    RequestEnvelope,
    ResponseEnvelope,
)
from .local_api_credentials import CredentialAuthority
from .storage import open_storage

# These labels are historical identities only.  They are deliberately not
# component identifiers, lifecycle targets or installation defaults.
HISTORICAL_LEGACY_LABEL = "com.djconnect.engineering-local-api"
HISTORICAL_NEUTRAL_LABEL = "com.engineeringplatform.local-api"
LOOPBACK_ADDRESS = "127.0.0.1"
DEFAULT_PORT = 8766
MAX_BODY_BYTES = 8192
REQUEST_TIMEOUT_SECONDS = 15


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


def retirement_status() -> dict[str, object]:
    """Expose the bounded historical identity classification for tooling."""

    return {
        "state": "RETIRED",
        "historical_labels": (HISTORICAL_LEGACY_LABEL, HISTORICAL_NEUTRAL_LABEL),
        "installable": False,
        "lifecycle_owned": False,
        "supported_submission_ingress": False,
    }
