"""Focused transport/auth regression coverage for the loopback Local API."""

from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from engineering_platform.local_api import LOOPBACK_ADDRESS, LocalApiServer
from engineering_platform.local_api_credentials import CredentialAuthority


SECRET = "local-api-sentinel-secret-do-not-log"


class LocalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.server = LocalApiServer(
            Path(self.temporary.name),
            0,
            CredentialAuthority.test_fixture(
                SECRET, consumer_id="workspace-client", project_id="project-123"
            ),
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        connection = HTTPConnection(LOOPBACK_ADDRESS, self.server.server_port, timeout=3)
        connection.request(
            method, path, body=None if body is None else json.dumps(body), headers=headers or {}
        )
        response = connection.getresponse()
        content = response.read().decode()
        connection.close()
        return response.status, content

    def envelope(self, project_id: str = "project-123") -> dict[str, object]:
        return {
            "contract_version": "1.0",
            "request_type": "contract.foundation",
            "request_id": "request-123",
            "project_id": project_id,
            "consumer": {"consumer_id": "workspace-client"},
            "auth": {"scheme": "bearer", "credential": "transport-carrier"},
            "payload": {},
        }

    def test_loopback_health_and_authenticated_capability_are_bounded(self) -> None:
        self.assertEqual(self.server.server_address[0], LOOPBACK_ADDRESS)
        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET, health)
        status, body = self.request(
            "POST",
            "/v1/capabilities",
            self.envelope(),
            {"Content-Type": "application/json", "Authorization": f"Bearer {SECRET}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["payload"]["project_id"], "project-123")
        self.assertNotIn(SECRET, body)

    def test_auth_and_project_scope_fail_closed_without_secret_reflection(self) -> None:
        for headers, expected in (
            ({"Content-Type": "application/json"}, 401),
            ({"Content-Type": "application/json", "Authorization": "Basic bad"}, 401),
        ):
            status, body = self.request("POST", "/v1/capabilities", self.envelope(), headers)
            self.assertEqual(status, expected)
            self.assertNotIn(SECRET, body)
        status, body = self.request(
            "POST",
            "/v1/capabilities",
            self.envelope("project-other"),
            {"Content-Type": "application/json", "Authorization": f"Bearer {SECRET}"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "PROJECT_NOT_AUTHORIZED")

    def test_malformed_and_oversized_bodies_fail_closed(self) -> None:
        connection = HTTPConnection(LOOPBACK_ADDRESS, self.server.server_port, timeout=3)
        connection.request(
            "POST",
            "/v1/capabilities",
            body="{",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {SECRET}"},
        )
        self.assertEqual(connection.getresponse().status, 400)
        connection.close()
        status, _ = self.request(
            "POST",
            "/v1/capabilities",
            {"payload": "x" * 9000},
            {"Content-Type": "application/json", "Authorization": f"Bearer {SECRET}"},
        )
        self.assertEqual(status, 413)
