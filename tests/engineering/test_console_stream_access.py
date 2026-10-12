"""Actual HTTP stream boundaries against durable CENTRAL access changes."""
import http.server
import json
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from engineering_platform import server
from engineering_platform.storage import sqlite_connection
from tests.engineering.dashboard_run_evidence_fixture import RealTopologyFixture


class ConsoleStreamAccessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = RealTopologyFixture()
        self.addCleanup(self.fixture.close)
        self.errors = []
        errors = self.errors

        class Listener(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                import sys
                import traceback
                errors.append(sys.exc_info()[1])
                traceback.print_exc()

        self.httpd = Listener(("127.0.0.1", 0), server._HealthHandler)
        self.httpd.data_root = self.fixture.data
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def revoke(self):
        with sqlite_connection(self.fixture.database) as connection:
            connection.execute("UPDATE ep_project_registrations SET status='DISABLED' WHERE project_id='project'")

    def snapshot(self, root, project):
        # Exercise actual project_context/FIFO reads; isolate platform metadata.
        return {"project_id": project, "scope": "PROJECT", "queue": server._console_queue_projection(root, project)}

    def request(self, path="/api/events"):
        return urlopen(f"http://127.0.0.1:{self.httpd.server_port}{path}?project=project", timeout=5)

    def test_payload_prepared_before_revocation_is_not_published(self):
        def snapshot(root, project):
            result = self.snapshot(root, project)
            self.revoke()
            return result
        with patch.object(server, "_central_console_project_snapshot", snapshot):
            from urllib.error import HTTPError
            with self.assertRaises(HTTPError) as denied:
                self.request()
            self.assertEqual(denied.exception.code, 409)
            denied.exception.close()
        self.assertEqual(self.errors, [])

    def test_list_context_revocation_race_is_expected_denial_before_headers(self):
        original = server.project_context
        def context(*args, **kwargs):
            self.revoke()
            return original(*args, **kwargs)
        with patch.object(server, "_central_console_project_snapshot", self.snapshot), patch.object(server, "project_context", context):
            from urllib.error import HTTPError
            with self.assertRaises(HTTPError) as denied:
                self.request()
            self.assertEqual(denied.exception.code, 409)
            denied.exception.close()
        self.assertEqual(self.errors, [])

    def test_open_stream_revocation_ends_with_scoped_control_only(self):
        calls = []
        def snapshot(root, project):
            if calls:
                self.revoke()
            calls.append(project)
            return self.snapshot(root, project)
        with patch.object(server, "_central_console_project_snapshot", snapshot), patch.object(
                server.central_database, "console_interval_configuration", return_value={"dashboard_stream_interval_seconds": 0}):
            with self.request() as response:
                body = response.read().decode()
        self.assertEqual(body.count("event: dashboard\n"), 1)
        self.assertIn("event: console-read-error\n", body)
        control = body.split("event: console-read-error\ndata: ")[1].split("\n")[0]
        self.assertEqual(json.loads(control), {"project_id": "project", "error": "CONSOLE_PROJECT_UNAVAILABLE", "outcome": "DENIED"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.errors, [])

    def test_initial_unavailable_source_is_503_and_does_not_create_database(self):
        displaced = self.fixture.database.with_suffix(".unavailable")
        self.fixture.database.rename(displaced)
        try:
            from urllib.error import HTTPError
            for path in ("/api/events", "/api/dashboard-snapshot", "/api/prompt-history"):
                with self.assertRaises(HTTPError) as unavailable:
                    self.request(path)
                self.assertEqual(unavailable.exception.code, 503)
                self.assertEqual(json.load(unavailable.exception), {"error": "CENTRAL_UNAVAILABLE"})
                unavailable.exception.close()
                self.assertFalse(self.fixture.database.exists())
        finally:
            displaced.rename(self.fixture.database)
        self.assertEqual(self.errors, [])

    def test_health_read_of_missing_source_cannot_bootstrap_an_installation(self):
        displaced = self.fixture.data.with_name("central-unavailable")
        self.fixture.data.rename(displaced)
        try:
            from urllib.error import HTTPError
            for path in ("/health", "/api/health"):
                with self.assertRaises(HTTPError) as unavailable:
                    self.request(path)
                with unavailable.exception as response:
                    status = response.code
                    payload = json.load(response)
                self.assertFalse(self.fixture.database.exists())
                self.assertFalse((self.fixture.data / server.SERVER_IDENTITY_FILENAME).exists())
                self.assertEqual(status, 503)
                self.assertEqual(payload, {"error": "CENTRAL_UNAVAILABLE"})
            self.assertFalse(self.fixture.database.exists())
            self.assertFalse((self.fixture.data / server.SERVER_IDENTITY_FILENAME).exists())
        finally:
            if self.fixture.data.exists():
                self.fixture.data.rename(self.fixture.data.with_name("central-observed-health-bootstrap"))
            displaced.rename(self.fixture.data)
        self.assertEqual(self.errors, [])

    def test_status_read_retains_official_store_integrity_checks(self):
        with sqlite_connection(self.fixture.database) as connection:
            connection.execute("UPDATE engineering_metadata SET value='invalid' WHERE key='installation.instance_id'")
        with self.assertRaises(server.ServerConfigurationError):
            server.status(self.fixture.data)

    def test_open_stream_source_loss_is_unavailable_not_denied(self):
        calls = []
        displaced = self.fixture.database.with_suffix(".unavailable")
        def snapshot(root, project):
            result = self.snapshot(root, project)
            calls.append(project)
            if len(calls) == 2:
                self.fixture.database.rename(displaced)
            return result
        try:
            with patch.object(server, "_central_console_project_snapshot", snapshot), patch.object(
                    server.central_database, "console_interval_configuration", return_value={"dashboard_stream_interval_seconds": 0}):
                with self.request() as response:
                    body = response.read().decode()
            self.assertEqual(body.count("event: dashboard\n"), 1)
            self.assertIn('"outcome":"UNAVAILABLE"', body)
            self.assertNotIn('"outcome":"DENIED"', body)
            self.assertEqual(self.errors, [])
        finally:
            if displaced.exists():
                displaced.rename(self.fixture.database)

    def test_unexpected_late_handler_error_remains_detected(self):
        calls = []
        def snapshot(root, project):
            if calls:
                raise ValueError("unexpected ordinary regression sentinel")
            calls.append(project)
            return self.snapshot(root, project)
        with patch.object(server, "_central_console_project_snapshot", snapshot), patch.object(
                server.central_database, "console_interval_configuration", return_value={"dashboard_stream_interval_seconds": 0}):
            with self.request() as response:
                body = response.read().decode()
        self.assertEqual(body.count("event: dashboard\n"), 1)
        self.assertNotIn("event: console-read-error", body)
        self.assertEqual([str(error) for error in self.errors], ["unexpected ordinary regression sentinel"])
