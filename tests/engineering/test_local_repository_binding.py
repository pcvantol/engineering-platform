"""Schema-44 private local execution repository binding coverage."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from engineering_platform import local_repository_binding as bindings
from engineering_platform import project_topology, server


FIXTURE = Path(__file__).parent / "fixtures" / "repository_attachment" / "python-authority.json"


class LocalRepositoryBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.data_root = self.base / "central"
        server.initialize(self.data_root)
        self.declaration = json.loads(FIXTURE.read_text())
        with sqlite3.connect(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            project_topology.register_attachment(connection, agent_id=self._agent(connection), declaration=self.declaration, availability="AVAILABLE")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _agent(self, connection: sqlite3.Connection) -> str:
        # Direct registration avoids transport concerns; only logical topology matters.
        connection.execute("INSERT INTO ep_agent_registrations(agent_id,state,credential_id,credential_verifier,created_at,updated_at,last_seen_at) VALUES('agent-one','ACTIVE','credential-one',X'00','now','now','now')")
        return "agent-one"

    def _checkout(self, name: str, payload: dict[str, object] | None = None) -> Path:
        root = self.base / name
        config = root / ".engineering-platform" / "repository.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(payload or self.declaration), encoding="utf-8")
        return root

    def _bind(self, root: Path, *, rebind: bool = False):
        with sqlite3.connect(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            return bindings.bind_local_repository(connection, project_id="acme-data", repository_id="acme-data", local_root=root, data_root=self.data_root, rebind=rebind)

    def test_fresh_schema_45_has_no_bindings_and_integrity_passes(self) -> None:
        identity = server.initialize(self.data_root)
        with sqlite3.connect(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_local_repository_bindings").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(server.validate_store(self.data_root, identity)["schema_version"], 45)

    def test_valid_bind_resolves_and_rebind_is_explicit(self) -> None:
        first, second = self._checkout("first"), self._checkout("second")
        self.assertEqual(self._bind(first).local_root, first.resolve())
        self.assertEqual(self._bind(first).state, "BOUND")
        with self.assertRaisesRegex(bindings.LocalRepositoryBindingError, "REBIND_REQUIRED"):
            self._bind(second)
        self.assertEqual(self._bind(second, rebind=True).local_root, second.resolve())
        with sqlite3.connect(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(bindings.resolve_execution_repository(connection, project_id="acme-data", repository_id="acme-data", data_root=self.data_root).local_root, second.resolve())

    def test_invalid_topology_path_and_declaration_fail_closed(self) -> None:
        root = self._checkout("root")
        with sqlite3.connect(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            for project_id, repository_id, path, error in (
                ("missing", "acme-data", root, "UNKNOWN_PROJECT"),
                ("acme-data", "missing", root, "UNKNOWN_REPOSITORY"),
                ("acme-data", "acme-data", "relative", "NOT_ABSOLUTE"),
                ("acme-data", "acme-data", self.base / "missing", "UNAVAILABLE"),
            ):
                with self.assertRaisesRegex(bindings.LocalRepositoryBindingError, error):
                    bindings.bind_local_repository(connection, project_id=project_id, repository_id=repository_id, local_root=path, data_root=self.data_root)
        wrong = dict(self.declaration); wrong["project"] = dict(wrong["project"]); wrong["project"]["id"] = "wrong-project"
        with self.assertRaisesRegex(bindings.LocalRepositoryBindingError, "DECLARATION_MISMATCH"):
            self._bind(self._checkout("wrong", wrong))

    def test_cross_project_mismatch_and_two_project_isolation(self) -> None:
        second = dict(self.declaration)
        second["project"] = {"id": "other-project", "authority_repository_id": "other-repo"}
        second["repository"] = {"id": "other-repo", "role": "authority"}
        first_root, second_root = self._checkout("first"), self._checkout("second", second)
        with sqlite3.connect(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            project_topology.register_attachment(connection, agent_id="agent-one", declaration=second, availability="AVAILABLE")
            with self.assertRaisesRegex(bindings.LocalRepositoryBindingError, "PROJECT_REPOSITORY_MISMATCH"):
                bindings.bind_local_repository(connection, project_id="acme-data", repository_id="other-repo", local_root=second_root, data_root=self.data_root)
            bindings.bind_local_repository(connection, project_id="acme-data", repository_id="acme-data", local_root=first_root, data_root=self.data_root)
            bindings.bind_local_repository(connection, project_id="other-project", repository_id="other-repo", local_root=second_root, data_root=self.data_root)
            self.assertEqual(bindings.resolve_execution_repository(connection, project_id="acme-data", repository_id="acme-data", data_root=self.data_root).local_root, first_root.resolve())
            self.assertEqual(bindings.resolve_execution_repository(connection, project_id="other-project", repository_id="other-repo", data_root=self.data_root).local_root, second_root.resolve())

    def test_unbind_preserves_topology_and_no_public_path_leak(self) -> None:
        root = self._checkout("root")
        self._bind(root)
        with sqlite3.connect(self.data_root / server.SERVER_DATABASE_FILENAME) as connection:
            bindings.unbind_local_repository(connection, project_id="acme-data", repository_id="acme-data")
            self.assertEqual(len(project_topology.topology(connection)["projects"]), 1)
            with self.assertRaisesRegex(bindings.LocalRepositoryBindingError, "UNBOUND"):
                bindings.resolve_execution_repository(connection, project_id="acme-data", repository_id="acme-data", data_root=self.data_root)
        self.assertNotIn(str(root), json.dumps(server.operations_projection(self.data_root)))

    def test_schema_43_migrates_without_inferred_binding(self) -> None:
        root = self.base / "migration"; root.mkdir()
        identity = server.RuntimeIdentity("test-installation", "now")
        db = root / server.SERVER_DATABASE_FILENAME
        with sqlite3.connect(db) as connection:
            server._install_schema_41(connection, identity); server._migrate_schema_42(connection); server._migrate_schema_43(connection)
            connection.execute("INSERT INTO ep_agent_registrations(agent_id,state,credential_id,credential_verifier,created_at,updated_at,last_seen_at) VALUES('agent-migration','ACTIVE','credential-migration',X'00','now','now','now')")
            project_topology.register_attachment(connection, agent_id="agent-migration", declaration=self.declaration, availability="AVAILABLE")
        (root / server.SERVER_CONFIGURATION_FILENAME).write_text('{"version":1,"bind_host":"127.0.0.1","bind_port":8765}', encoding="utf-8")
        (root / server.SERVER_IDENTITY_FILENAME).write_text(json.dumps({"instance_id": identity.instance_id, "created_at": identity.created_at}), encoding="utf-8")
        server.initialize(root)
        with sqlite3.connect(db) as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0], 45)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_local_repository_bindings").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_project_registrations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_repository_registrations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_agent_repository_attachments").fetchone()[0], 1)
