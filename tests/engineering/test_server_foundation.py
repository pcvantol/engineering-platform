from __future__ import annotations

import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from urllib.request import urlopen

from engineering_platform import local_repository_binding, project_topology, server


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
        self.assertTrue((self.root / server.SERVER_DATABASE_FILENAME).is_file())
        self.assertEqual(report["instance_id"], identity.instance_id)
        self.assertEqual(report["schema_version"], 45)
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

    def test_fresh_store_is_official_schema_45_with_empty_operational_state(self) -> None:
        identity = server.initialize(self.root)
        report = server.validate_store(self.root, identity)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(
                connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0],
                45,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_installations").fetchone()[0], 1)
            for table in (
                "ep_agent_registrations",
                "ep_project_registrations",
                "ep_execution_runs",
                "ep_execution_leases",
                "prompt_execution_history",
                "local_api_credentials",
                "local_api_consumer_registrations",
                "ep_local_repository_bindings",
            ):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(report["integrity"], "PASS")

    def test_bootstrap_does_not_use_a_legacy_database_or_identity(self) -> None:
        legacy = self.root.parent / "legacy-schema40.db"
        with sqlite3.connect(legacy) as connection:
            connection.execute("CREATE TABLE engineering_schema_migrations(version INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO engineering_schema_migrations(version) VALUES(40)")
            connection.execute("CREATE TABLE engineering_transactions(run_id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO engineering_transactions(run_id) VALUES('legacy-run')")
        legacy_before = legacy.read_bytes()
        identity = server.initialize(self.root)
        self.assertEqual(legacy.read_bytes(), legacy_before)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_execution_runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT instance_id FROM ep_installations").fetchone()[0], identity.instance_id)

    def test_readiness_fails_closed_for_an_outdated_store(self) -> None:
        self.root.mkdir()
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("CREATE TABLE engineering_schema_migrations(version INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO engineering_schema_migrations(version) VALUES(40)")
        with self.assertRaisesRegex(server.ServerConfigurationError, "schema-45"):
            server.initialize(self.root)

    def test_restart_preserves_clean_identity_and_schema(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        identity = server.initialize(self.root, bind_port=port)
        server.start(self.root)
        server.stop(self.root)
        server.start(self.root)
        report = server.health(self.root)
        self.assertTrue(report["ready"])
        self.assertEqual(report["instance_id"], identity.instance_id)
        self.assertEqual(report["schema_version"], 45)

    def test_operations_console_projection_is_central_owned_and_empty_safe(self) -> None:
        identity = server.initialize(self.root)
        projection = server.operations_projection(self.root)
        self.assertEqual(projection["installation_id"], identity.instance_id)
        self.assertEqual(projection["schema_version"], 45)
        self.assertEqual(projection["projects"], [])
        self.assertIn(b"/v1/operations/projects", server._operations_console_document())

    def test_root_reuses_historical_console_with_request_scoped_project_selection(self) -> None:
        """Two requests select separate roots without exposing either path."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=port)
        template = json.loads((Path(__file__).parent / "fixtures" / "repository_attachment" / "python-authority.json").read_text(encoding="utf-8"))
        declarations = []
        for identifier in ("djconnect", "engineering-platform"):
            declaration = json.loads(json.dumps(template))
            declaration["project"]["id"] = identifier
            declaration["project"]["authority_repository_id"] = identifier
            declaration["repository"]["id"] = identifier
            declarations.append(declaration)
        roots: list[Path] = []
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("INSERT INTO ep_agent_registrations(agent_id,state,credential_id,credential_verifier,created_at,updated_at,last_seen_at) VALUES('console-agent','ACTIVE','console-credential',X'00','now','now','now')")
            for declaration in declarations:
                checkout = self.root.parent / declaration["project"]["id"]
                attachment = checkout / ".engineering-platform" / "repository.json"
                attachment.parent.mkdir(parents=True)
                attachment.write_text(json.dumps(declaration), encoding="utf-8")
                project_topology.register_attachment(connection, agent_id="console-agent", declaration=declaration, availability="AVAILABLE")
                local_repository_binding.bind_local_repository(connection, project_id=declaration["project"]["id"], repository_id=declaration["repository"]["id"], local_root=checkout, data_root=self.root)
                roots.append(checkout)
        server.start(self.root)
        with urlopen(f"http://127.0.0.1:{port}/?project=djconnect") as response:
            first = response.read().decode("utf-8")
        with urlopen(f"http://127.0.0.1:{port}/?project=engineering-platform") as response:
            second = response.read().decode("utf-8")
        # Browser module and stylesheet requests precede the document's
        # project-aware fetch wrapper, so neutral package assets must remain
        # available without a scope header.
        with urlopen(f"http://127.0.0.1:{port}/assets/dashboard.js") as response:
            asset = response.read()
        self.assertIn('id="consoleProject"', first)
        self.assertIn('value="djconnect" selected', first)
        self.assertIn('value="engineering-platform" selected', second)
        self.assertIn('/assets/dashboard.js', first)
        self.assertIn(b"fetch", asset)
        self.assertNotIn(str(roots[0]), first)
        self.assertNotIn(str(roots[1]), second)
