from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import unittest

from engineering_platform import server


class StandaloneServerFoundationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "clean-installation"

    def tearDown(self) -> None:
        try:
            server.stop(self.root)
        except server.ServerConfigurationError:
            pass
        finally:
            self.temporary.cleanup()

    def test_empty_installation_bootstraps_a_clean_valid_store(self) -> None:
        identity = server.initialize(self.root)
        report = server.status(self.root)
        self.assertTrue((self.root / "ep-server.db").is_file())
        self.assertEqual(report["instance_id"], identity.instance_id)
        self.assertEqual(report["operational_state"], "empty-valid")
        self.assertFalse(report["running"])
        self.assertFalse((self.root / ".engineering").exists())

    def test_start_stop_and_http_readiness_work_without_a_checkout(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=port)
        started = server.start(self.root)
        self.assertTrue(started["running"])
        report = server.health(self.root)
        self.assertTrue(report["healthy"])
        self.assertTrue(report["ready"])
        stopped = server.stop(self.root)
        self.assertFalse(stopped["running"])

    def test_configuration_fails_closed(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "server.json").write_text(json.dumps({"version": 2}), encoding="utf-8")
        with self.assertRaises(server.ServerConfigurationError):
            server.initialize(self.root)

    def test_unauthenticated_foundation_refuses_a_non_loopback_bind(self) -> None:
        with self.assertRaises(server.ServerConfigurationError):
            server.initialize(self.root, bind_host="0.0.0.0")

    def test_agent_extension_point_remains_transport_neutral(self) -> None:
        request = server.AgentRegistrationRequest("future-agent", "project-agent", ("execute",))
        self.assertEqual(request.agent_id, "future-agent")
        self.assertFalse(hasattr(request, "credential"))
