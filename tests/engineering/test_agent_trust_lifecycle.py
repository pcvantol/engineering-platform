from __future__ import annotations

from pathlib import Path
import socket
import tempfile
import unittest

from engineering_platform import agent_trust, project_agent, server


class AgentTrustLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "server"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); self.port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=self.port)
        server.start(self.root)
        self.agent_dir = Path(self.temp.name) / "agent"
        self.identity, self.config = self.agent_dir / "identity.json", self.agent_dir / "server.json"
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self.agent_id = project_agent.load_or_create_identity(project_agent.observe_host_identity(), self.identity).agent_id

    def tearDown(self) -> None:
        server.stop(self.root); self.temp.cleanup()

    def _approved_pair(self) -> None:
        import sqlite3
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            code = agent_trust.create_pairing_code(connection, self.agent_id)["pairing_code"]
        project_agent.pair(self.endpoint, code, identity_path=self.identity, configuration_path=self.config)

    def test_pair_register_heartbeat_restart_and_stale_lifecycle(self) -> None:
        self._approved_pair()
        self.assertEqual(project_agent.register((), identity_path=self.identity, configuration_path=self.config)["state"], "REGISTERED")
        self.assertEqual(project_agent.heartbeat(configuration_path=self.config)["state"], "ONLINE")
        server.stop(self.root); server.start(self.root)
        self.assertEqual(project_agent.heartbeat(configuration_path=self.config)["state"], "ONLINE")
        import sqlite3
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("UPDATE ep_agent_registrations SET last_seen_at='2000-01-01T00:00:00+00:00' WHERE agent_id=?", (self.agent_id,))
            self.assertEqual(agent_trust.registration_status(connection, self.agent_id)["liveness"], "STALE")

    def test_unapproved_invalid_revoked_and_version_mismatch_fail_closed(self) -> None:
        with self.assertRaises(ValueError): project_agent.pair(self.endpoint, "not-approved", identity_path=self.identity, configuration_path=self.config)
        self._approved_pair()
        with self.assertRaises(ValueError): project_agent._post(self.endpoint, "/v1/agent/heartbeat", {"protocol_version": "999", "agent_id": self.agent_id}, "bad")
        import sqlite3
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection: agent_trust.revoke(connection, self.agent_id)
        with self.assertRaises(ValueError): project_agent.heartbeat(configuration_path=self.config)

    def test_localhost_does_not_bypass_authentication_and_malformed_is_rejected(self) -> None:
        with self.assertRaises(ValueError): project_agent._post(self.endpoint, "/v1/agent/register", {"protocol_version": agent_trust.PROTOCOL_VERSION, "agent_id": self.agent_id}, None)
        with self.assertRaises(ValueError): project_agent._post("http://192.0.2.1:8765", "/v1/agent/pair", {}, None)

    def test_multiple_b5_attachment_reports_are_optional_and_bounded(self) -> None:
        self._approved_pair()
        self.assertEqual(project_agent.register((Path(self.temp.name), Path(self.temp.name) / "missing"), identity_path=self.identity, configuration_path=self.config)["state"], "REGISTERED")
