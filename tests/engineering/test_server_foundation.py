from __future__ import annotations

import json
from pathlib import Path
import socket
import sqlite3
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
        self.assertTrue((self.root / server.SERVER_DATABASE_FILENAME).is_file())
        self.assertEqual(report["instance_id"], identity.instance_id)
        self.assertEqual(report["schema_version"], 44)
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

    def test_fresh_store_is_official_schema_44_with_empty_operational_state(self) -> None:
        identity = server.initialize(self.root)
        report = server.validate_store(self.root, identity)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(
                connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0],
                44,
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
        with self.assertRaisesRegex(server.ServerConfigurationError, "schema-44"):
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
        self.assertEqual(report["schema_version"], 44)

    def test_operations_console_projection_is_central_owned_and_empty_safe(self) -> None:
        identity = server.initialize(self.root)
        projection = server.operations_projection(self.root)
        self.assertEqual(projection["installation_id"], identity.instance_id)
        self.assertEqual(projection["schema_version"], 44)
        self.assertEqual(projection["projects"], [])
        self.assertIn(b"/v1/operations/projects", server._operations_console_document())
