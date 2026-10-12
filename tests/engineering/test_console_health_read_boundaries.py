"""Actual HTTP health publication under CENTRAL loss and replacement."""
import json
import os
import shutil
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

from engineering_platform import providers, server
from tests.engineering import test_console_stream_access
from tests.engineering.dashboard_run_evidence_fixture import IsolatedProviderTransport, RealTopologyFixture


class ConsoleHealthReadBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.listener = test_console_stream_access.ConsoleStreamAccessTests()
        self.listener.setUp()
        self.transport = None
        self.addCleanup(self.close)
        self.transport = IsolatedProviderTransport()
        self.transport.start()
        self.root = self.listener.fixture.data
        self.original_database = self.listener.fixture.database.read_bytes()

    def close(self):
        # stop waits for the actual HTTP listener and request threads. Keep
        # provider guards installed if this cannot establish their closure.
        self.listener.stop()
        if self.transport is not None:
            self.transport.close()
        self.listener.fixture.close()

    def read(self, path="/health"):
        try:
            response = urlopen(f"http://127.0.0.1:{self.listener.httpd.server_port}{path}", timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def assert_original(self):
        self.assertEqual(self.listener.fixture.database.read_bytes(), self.original_database)
        self.assertEqual(self.listener.errors, [])

    def test_unknown_listener_stop_keeps_provider_guard_on_actual_live_requests(self):
        original_guard = providers.GitHubProvider.github
        with patch.object(self.listener, "stop", side_effect=RuntimeError("listener stop not established")):
            with self.assertRaisesRegex(RuntimeError, "listener stop not established"):
                self.close()
        self.assertTrue(self.listener.thread.is_alive())
        self.assertIs(providers.GitHubProvider.github, original_guard)
        previous_calls = self.transport.github_quota_requests
        self.assertEqual(self.read("/api/github-rate-limit"), (200, {"limited": False}))
        self.assertEqual(self.transport.github_quota_requests, previous_calls + 1)
        self.assert_original()

    def test_loss_between_identity_and_configuration_is_typed_and_restorable(self):
        moved = self.root.with_name("health-source-displaced")
        original = server.ServerConfiguration.load
        def lose(root):
            self.assertEqual(root, self.root)
            root.rename(moved)
            return original(root)
        try:
            with patch.object(server.ServerConfiguration, "load", lose):
                self.assertEqual(self.read(), (503, {"error": "CENTRAL_UNAVAILABLE"}))
            self.assertFalse(self.root.exists())
            self.assertEqual((moved / server.SERVER_DATABASE_FILENAME).read_bytes(), self.original_database)
        finally:
            moved.rename(self.root)
        code, report = self.read()
        self.assertIn(code, (200, 503))
        self.assertEqual(report["instance_id"], server._console_source_identity(self.root).instance_id)
        self.assert_original()

    def test_present_missing_and_corrupt_configuration_remain_ordinary_errors(self):
        configuration = self.root / server.SERVER_CONFIGURATION_FILENAME
        original = configuration.read_bytes()
        try:
            for replacement, cause in ((None, FileNotFoundError), (b"{invalid-json", json.JSONDecodeError)):
                with self.subTest(cause=cause.__name__):
                    if replacement is None:
                        configuration.unlink()
                    else:
                        configuration.write_bytes(replacement)
                    with self.assertRaises(server.ServerConfigurationError) as caught:
                        server.status(self.root)
                    self.assertIsInstance(caught.exception.__cause__, cause)
                    configuration.write_bytes(original)
                    self.assert_original()
        finally:
            configuration.write_bytes(original)

    def test_valid_foreign_source_at_configuration_read_cannot_publish_original_identity(self):
        foreign = RealTopologyFixture()
        moved = self.root.with_name("health-source-displaced")
        original = server.ServerConfiguration.load
        def replace(root):
            root.rename(moved)
            shutil.copytree(foreign.data, root)
            return original(root)
        try:
            with patch.object(server.ServerConfiguration, "load", replace):
                self.assertEqual(self.read(), (409, {"error": "CONSOLE_SOURCE_IDENTITY_CONFLICT"}))
        finally:
            if moved.exists():
                shutil.rmtree(self.root)
                moved.rename(self.root)
            foreign.close()
        self.assert_original()

    def test_health_publication_fence_covers_healthy_and_degraded_reports(self):
        original = server._HealthHandler._send
        runtime = self.root / server.SERVER_RUNTIME_FILENAME
        previous_runtime = runtime.read_bytes() if runtime.exists() else None
        def restore_runtime():
            if previous_runtime is None:
                runtime.unlink(missing_ok=True)
            else:
                runtime.write_bytes(previous_runtime)
        self.addCleanup(restore_runtime)
        for healthy in (True, False):
            if healthy:
                # The listener executes in this actual owned process. No
                # invented PID or patched liveness/health result is used.
                runtime.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-10-12T00:00:00+00:00",
                                               "port": self.listener.httpd.server_port}))
            else:
                runtime.unlink(missing_ok=True)
            for replacement in ("missing", "foreign"):
                with self.subTest(healthy=healthy, replacement=replacement):
                    foreign = RealTopologyFixture()
                    moved = self.root.with_name("health-source-displaced")
                    def interleave(handler, code, payload, *args, **kwargs):
                        if "healthy" in payload:
                            self.assertEqual(payload["healthy"], healthy)
                            self.assertEqual(code, 200 if healthy else 503)
                            self.root.rename(moved)
                            if replacement == "foreign":
                                shutil.copytree(foreign.data, self.root)
                        return original(handler, code, payload, *args, **kwargs)
                    try:
                        with patch.object(server._HealthHandler, "_send", interleave):
                            expected = (503, {"error": "CENTRAL_UNAVAILABLE"}) if replacement == "missing" else (
                                409, {"error": "CONSOLE_SOURCE_IDENTITY_CONFLICT"})
                            self.assertEqual(self.read("/api/health"), expected)
                        if replacement == "missing":
                            self.assertFalse(self.root.exists())
                    finally:
                        if moved.exists():
                            if self.root.exists():
                                shutil.rmtree(self.root)
                            moved.rename(self.root)
                        foreign.close()
                    self.assert_original()
