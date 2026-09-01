from __future__ import annotations

import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest

from engineering_platform import agent_trust, project_agent, project_topology, server


FIXTURE = Path(__file__).parent / "fixtures" / "repository_attachment" / "python-authority.json"


class ProjectTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "server"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); self.port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=self.port); server.start(self.root)
        self.endpoint = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        server.stop(self.root); self.temp.cleanup()

    def _agent(self, name: str) -> tuple[Path, Path, str]:
        folder = Path(self.temp.name) / name
        identity, config = folder / "identity.json", folder / "server.json"
        agent_id = project_agent.load_or_create_identity(project_agent.observe_host_identity(), identity).agent_id
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            code = agent_trust.create_pairing_code(connection, agent_id)["pairing_code"]
        project_agent.pair(self.endpoint, code, identity_path=identity, configuration_path=config)
        return identity, config, agent_id

    def _repository(self, name: str, payload: dict[str, object] | None = None) -> Path:
        root = Path(self.temp.name) / name
        target = root / ".engineering-platform" / "repository.json"; target.parent.mkdir(parents=True)
        target.write_text(json.dumps(payload or json.loads(FIXTURE.read_text())), encoding="utf-8")
        return root

    def test_authenticated_attachment_is_idempotent_and_portable(self) -> None:
        identity, config, agent_id = self._agent("agent-one")
        repository = self._repository("a")
        first = project_agent.attach(repository, identity_path=identity, configuration_path=config)
        second = project_agent.attach(repository, identity_path=identity, configuration_path=config)
        self.assertEqual(first, second)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            report = project_topology.topology(connection)
            self.assertEqual(len(report["projects"]), 1)
            self.assertEqual(report["projects"][0]["repositories"][0]["attachments"][0]["agent_id"], agent_id)
            self.assertNotIn(str(repository), json.dumps(report))

    def test_unpaired_revoked_and_conflicting_attachment_fail_closed(self) -> None:
        identity, config, agent_id = self._agent("agent-one")
        repository = self._repository("a")
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            agent_trust.revoke(connection, agent_id)
        with self.assertRaises(ValueError): project_agent.attach(repository, identity_path=identity, configuration_path=config)
        one_identity, one_config, _ = self._agent("agent-two")
        project_agent.attach(repository, identity_path=one_identity, configuration_path=one_config)
        conflict = json.loads(FIXTURE.read_text()); conflict["project"]["id"] = "other-project"
        with self.assertRaises(ValueError): project_agent.attach(self._repository("conflict", conflict), identity_path=one_identity, configuration_path=one_config)

    def test_multiple_agents_and_revocation_change_only_physical_availability(self) -> None:
        repository = self._repository("a")
        first_identity, first_config, first = self._agent("agent-one")
        second_identity, second_config, second = self._agent("agent-two")
        project_agent.attach(repository, identity_path=first_identity, configuration_path=first_config)
        project_agent.attach(repository, identity_path=second_identity, configuration_path=second_config)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            agent_trust.revoke(connection, first)
            attachments = project_topology.topology(connection)["projects"][0]["repositories"][0]["attachments"]
        self.assertEqual({item["agent_id"] for item in attachments}, {first, second})
        self.assertIn("UNAVAILABLE", {item["availability"] for item in attachments})
