from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import hashlib
import http.client
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from jsonschema import Draft202012Validator, FormatChecker

from engineering_platform import agent_trust, installation_pairing, project_discovery as discovery, project_topology, server, storage


class _DiscoveryFixture:

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(delete=False)
        self.addCleanup(self.cleanup_store)
        self.root = Path(self.temporary.name)
        self.instance = server.initialize(self.root).instance_id
        self.database = self.root / server.SERVER_DATABASE_FILENAME
        self.register("project-a", "core-a", "authority")
        self.register("project-a", "child-a", "child")
        self.register("project-b", "core-b", "authority")
        self.token = self.issue("grant-a", ["project-a"])["credential"]
        self.headers = {"EP-Discovery-Contract": "1.0", "EP-Instance-ID": self.instance,
            "EP-Consumer-ID": "workspace-consumer", "Workspace-Instance-ID": "workspace-instance",
            "EP-Audience": "workspace"}

    def cleanup_store(self):
        if getattr(self, "listener_stopped", True):
            self.temporary.cleanup()

    def register(self, project, repository, role):
        with storage.sqlite_connection(self.database) as connection:
            return project_topology.register_server_local_topology(connection, declaration={
                "schema_version": "1.0", "project": {"id": project, "authority_repository_id": "core-" + project[-1]},
                "repository": {"id": repository, "role": role}, "validation": {"kind": "none"}})

    def issue(self, grant, projects, **overrides):
        arguments = dict(grant_id=grant, instance_id=self.instance, consumer_id="workspace-consumer",
            workspace_instance_id="workspace-instance", project_ids=projects,
            fields=sorted(discovery.FIELDS), operation_id="issue-" + grant, ttl_seconds=900)
        arguments.update(overrides)
        with storage.sqlite_connection(self.database) as connection:
            return discovery.issue(connection, **arguments)

class ProjectDiscoveryTests(_DiscoveryFixture, unittest.TestCase):
    def read(self, project=None, query=None, token=None, headers=None):
        results = []
        discovery.read(self.root, token=self.token if token is None else token,
            headers=self.headers if headers is None else headers, query={} if query is None else query,
            project_id=project, publish=lambda body, instance, check: (check(), results.append((body, instance))))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], self.instance)
        return results[0][0]

    def assert_refused(self, code, **kwargs):
        with self.assertRaises(discovery.DiscoveryError) as context:
            self.read(**kwargs)
        self.assertEqual(context.exception.code, code)

    def owner_command(self, command, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = server.main([command, "--data-root", str(self.root),
                "--expected-instance-id", self.instance, "--discovery-grant-id", "cli-grant", *arguments])
        return code, json.loads(output.getvalue())

    def test_owner_cli_issue_status_and_revoke_are_same_finite_authority(self):
        code, grant = self.owner_command("discovery-grant-issue", "--consumer-id", "workspace-consumer",
            "--workspace-instance-id", "workspace-instance", "--discovery-project-id", "project-a",
            "--operation-id", "cli-issue", "--discovery-ttl-seconds", "30")
        self.assertEqual(code, 0)
        page = self.read(token=grant["credential"])
        self.assertEqual(page["scope"]["grant_id"], "cli-grant")
        before = self.database.read_bytes()
        code, public = self.owner_command("discovery-grant-status")
        self.assertEqual(code, 0)
        self.assertNotIn("credential", public)
        self.assertEqual(self.database.read_bytes(), before)
        code, revoked = self.owner_command("discovery-grant-revoke", "--operation-id", "cli-revoke")
        self.assertEqual((code, revoked["status"]), (0, "REVOKED"))
        self.assert_refused("DISCOVERY_INACTIVE", token=grant["credential"])

    def test_malformed_attachment_freshness_is_corruption_not_available(self):
        agent = "agent-corrupt-clock"
        with storage.sqlite_connection(self.database) as connection:
            code = agent_trust.create_pairing_code(connection, agent)["pairing_code"]
            token = agent_trust.pair(connection, {"protocol_version": "1.0", "agent_id": agent,
                "pairing_code": code})["credential"]
            agent_trust.heartbeat(connection, {"protocol_version": "1.0", "agent_id": agent}, token)
            agent_trust.register_attachment(connection, {"protocol_version": "1.0",
                "agent_id": agent, "availability": "AVAILABLE", "attachment": {
                "schema_version": "1.0", "project": {"id": "project-a", "authority_repository_id": "core-a"},
                "repository": {"id": "child-a", "role": "child"}, "validation": {"kind": "none"}}}, token)
        for malformed in ("zzz", "2026-10-14T09:00:00", "2026-99-99T09:00:00+00:00"):
            with self.subTest(timestamp=malformed):
                with storage.sqlite_connection(self.database) as connection:
                    connection.execute("UPDATE ep_agent_registrations SET last_seen_at=? WHERE agent_id=?", (malformed, agent))
                before = self.database.read_bytes()
                self.assert_refused("DISCOVERY_TOPOLOGY_CORRUPT")
                self.assertEqual(self.database.read_bytes(), before)

    def test_owner_cli_missing_central_does_not_initialize_or_create_it(self):
        self.database.rename(self.database.with_suffix(".saved"))
        for command in ("discovery-grant-issue", "discovery-grant-status", "discovery-grant-revoke"):
            with self.subTest(command=command):
                code, failure = self.owner_command(command, "--operation-id", "absent")
                self.assertEqual(code, 2)
                self.assertEqual(failure["error"], "DISCOVERY_CENTRAL_UNAVAILABLE")
                self.assertFalse(self.database.exists())

    def after_first_read_close(self, intervention):
        original = storage.sqlite_read_connection
        count = 0

        @contextmanager
        def intercepted(path):
            nonlocal count
            count += 1
            first = count == 1
            with original(path) as connection:
                yield connection
            if first:
                intervention()
        return patch.object(discovery.storage, "sqlite_read_connection", intercepted)

    def test_real_registered_identity_child_and_read_has_zero_database_writes(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        page = self.read()
        self.assertEqual(page["projects"], [{"project_id": "project-a", "authority_repository_id": "core-a"}])
        detail = self.read("project-a", {"expected_topology_revision": [page["topology_revision"]]})
        self.assertEqual(detail["repositories"], [{"repository_id": "child-a", "role": "child"}, {"repository_id": "core-a", "role": "authority"}])
        self.assertEqual(detail["attachments"], [])
        self.assertNotIn("project-b", json.dumps(detail))
        self.assertNotIn(str(self.root), json.dumps(detail))
        self.assertNotIn(self.token, json.dumps(detail))
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_empty_explicit_scope_is_healthy_empty_not_a_wildcard(self):
        issued = self.issue("empty", [])
        self.assertEqual(self.read(token=issued["credential"])["projects"], [])

    def test_field_scope_does_not_claim_absent_attachments(self):
        issued = self.issue("identity-only", ["project-a"], fields=sorted(discovery.IDENTITY_FIELDS))
        page = self.read(token=issued["credential"])
        detail = self.read("project-a", {"expected_topology_revision": [page["topology_revision"]]}, token=issued["credential"])
        self.assertIsNone(detail["attachments"])
        self.assertEqual(detail["attachment_visibility"], "NOT_GRANTED")

    def test_multiple_owned_attachments_preserve_logical_identity_and_revocation(self):
        for agent in ("agent-one", "agent-two"):
            with storage.sqlite_connection(self.database) as connection:
                code = agent_trust.create_pairing_code(connection, agent)["pairing_code"]
                token = agent_trust.pair(connection, {"protocol_version": "1.0",
                    "agent_id": agent, "pairing_code": code})["credential"]
                agent_trust.heartbeat(connection, {"protocol_version": "1.0", "agent_id": agent}, token)
                agent_trust.register_attachment(connection, {"protocol_version": "1.0",
                    "agent_id": agent, "availability": "AVAILABLE", "attachment": {
                    "schema_version": "1.0", "project": {"id": "project-a", "authority_repository_id": "core-a"},
                    "repository": {"id": "child-a", "role": "child"}, "validation": {"kind": "none"}}}, token)
        page = self.read()
        detail = self.read("project-a", {"expected_topology_revision": [page["topology_revision"]]})
        self.assertEqual(len(detail["repositories"]), 2)
        self.assertEqual(len(detail["attachments"]), 2)
        self.assertEqual({a["availability"] for a in detail["attachments"]}, {"AVAILABLE"})
        with storage.sqlite_connection(self.database) as connection:
            agent_trust.revoke(connection, "agent-one")
        self.assert_refused("DISCOVERY_SNAPSHOT_CONFLICT", project="project-a",
            query={"expected_topology_revision": [page["topology_revision"]]})
        page = self.read()
        detail = self.read("project-a", {"expected_topology_revision": [page["topology_revision"]]})
        self.assertEqual({a["agent_id"]: a["availability"] for a in detail["attachments"]},
            {"agent-one": "UNAVAILABLE", "agent-two": "AVAILABLE"})
        before = self.database.read_bytes()
        with patch.object(discovery, "_now", return_value=discovery._now() + discovery.timedelta(seconds=100)):
            stale_page = self.read()
            stale = self.read("project-a", {"expected_topology_revision": [stale_page["topology_revision"]]})
        self.assertEqual({a["availability"] for a in stale["attachments"]}, {"UNAVAILABLE"})
        self.assertEqual(self.database.read_bytes(), before)

    def test_every_consumer_instance_audience_binding_is_independent(self):
        for field in ("EP-Instance-ID", "EP-Consumer-ID", "Workspace-Instance-ID", "EP-Audience"):
            with self.subTest(field=field):
                self.assert_refused("DISCOVERY_SCOPE_MISMATCH", headers={**self.headers, field: "other"})

    def test_unknown_or_malformed_bearer_never_becomes_authority(self):
        for token in ("x", "x" * 43, "é" * 43, "" , "a/b" * 15):
            with self.subTest(token=token):
                self.assert_refused("UNAUTHENTICATED", token=token)

    def test_ungranted_project_is_denied_not_empty(self):
        revision = self.read()["topology_revision"]
        self.assert_refused("DISCOVERY_PROJECT_DENIED", project="project-b", query={"expected_topology_revision": [revision]})

    def test_issue_replay_does_not_reveal_or_mint_another_secret(self):
        self.assertRaises(discovery.DiscoveryError, self.issue, "grant-a", ["project-a"])
        with storage.sqlite_read_connection(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_workspace_discovery_grants").fetchone()[0], 1)
            self.assertNotIn("credential", discovery.status(connection, grant_id="grant-a"))

    def test_issuer_rejects_unknown_projects_fields_and_nonfinite_lifetimes(self):
        for kwargs in ({"projects": ["unknown"]}, {"projects": ["project-a", "project-a"]},
                {"projects": ["project-a"], "fields": ["admin"]},
                {"projects": ["project-a"], "ttl_seconds": 0},
                {"projects": ["project-a"], "ttl_seconds": 86401},
                {"projects": ["project-a"], "ttl_seconds": True}):
            arguments = dict(kwargs)
            projects = arguments.pop("projects")
            with self.subTest(kwargs=kwargs):
                self.assertRaises(discovery.DiscoveryError, self.issue, "bad", projects, **arguments)

    def test_revoked_grant_and_idempotent_exact_revocation(self):
        with storage.sqlite_connection(self.database) as connection:
            discovery.revoke(connection, grant_id="grant-a", operation_id="revoke-a")
            self.assertEqual(discovery.revoke(connection, grant_id="grant-a", operation_id="revoke-a")["status"], "REVOKED")
            self.assertRaises(discovery.DiscoveryError, discovery.revoke, connection, grant_id="grant-a", operation_id="different")
        before = self.database.read_bytes()
        self.assert_refused("DISCOVERY_INACTIVE")
        self.assertEqual(self.database.read_bytes(), before)

    def test_revocation_between_snapshots_prevents_any_publication(self):
        def revoke():
            with storage.sqlite_connection(self.database) as connection:
                discovery.revoke(connection, grant_id="grant-a", operation_id="late-revoke")
        with self.after_first_read_close(revoke):
            self.assert_refused("DISCOVERY_INACTIVE")

    def test_wal_cannot_publish_old_authority_after_owner_revocation_commits(self):
        with storage.sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        original = discovery._authenticate
        calls = 0
        publications = []
        committed = []
        def late_revocation(connection, token, headers):
            nonlocal calls
            scope = original(connection, token, headers)
            calls += 1
            if calls == 3:
                with storage.sqlite_connection(self.database) as writer:
                    discovery.revoke(writer, grant_id="grant-a", operation_id="wal-late-revoke")
                committed.append(True)
            return scope
        with patch.object(discovery, "_authenticate", side_effect=late_revocation):
            with self.assertRaises(discovery.DiscoveryError) as failure:
                discovery.read(self.root, token=self.token, headers=self.headers, query={}, project_id=None,
                    publish=lambda *args: publications.append(args))
        self.assertEqual(failure.exception.code, "DISCOVERY_JOURNAL_UNSUPPORTED")
        self.assertEqual(publications, [])
        self.assertEqual(committed, [])
        with storage.sqlite_read_connection(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_expiry_between_snapshots_prevents_any_publication(self):
        clock = discovery._now()
        with self.after_first_read_close(lambda: None), patch.object(discovery, "_now", side_effect=[clock, clock, clock + discovery.timedelta(days=1)]):
            self.assert_refused("DISCOVERY_INACTIVE")

    def test_missing_database_never_creates_or_initializes_one(self):
        saved = self.database.with_suffix(".saved")
        self.database.rename(saved)
        with self.assertRaises(FileNotFoundError):
            self.read()
        self.assertFalse(self.database.exists())
        self.assertTrue(saved.exists())

    def test_source_loss_between_snapshots_never_recreates_database(self):
        saved = self.database.with_suffix(".saved")
        with self.after_first_read_close(lambda: self.database.rename(saved)):
            with self.assertRaises(sqlite3.OperationalError) as context:
                self.read()
            self.assertEqual(context.exception.sqlite_errorcode, sqlite3.SQLITE_CANTOPEN)
        self.assertFalse(self.database.exists())
        self.assertTrue(saved.exists())

    def test_topology_change_after_selection_conflicts(self):
        revision = self.read()["topology_revision"]
        self.register("project-a", "another-child", "child")
        self.assert_refused("DISCOVERY_SNAPSHOT_CONFLICT", project="project-a", query={"expected_topology_revision": [revision]})

    def test_topology_change_between_read_and_publication_conflicts(self):
        with self.after_first_read_close(lambda: self.register("project-a", "later-child", "child")):
            self.assert_refused("DISCOVERY_SNAPSHOT_CONFLICT")

    def test_cursor_pages_only_the_exact_authorized_snapshot_and_limit(self):
        issued = self.issue("both", ["project-a", "project-b"])
        first = self.read(query={"limit": ["1"]}, token=issued["credential"])
        second = self.read(query={"limit": ["1"], "cursor": [first["next_cursor"]]}, token=issued["credential"])
        self.assertEqual(first["projects"][0]["project_id"], "project-a")
        self.assertEqual(second["projects"][0]["project_id"], "project-b")
        self.assertIsNone(second["next_cursor"])
        self.assert_refused("DISCOVERY_CURSOR_INVALID", query={"limit": ["2"], "cursor": [first["next_cursor"]]}, token=issued["credential"])
        self.assert_refused("DISCOVERY_CURSOR_INVALID", query={"limit": ["1"], "cursor": [first["next_cursor"] + "a"]}, token=issued["credential"])
        self.assert_refused("DISCOVERY_CURSOR_INVALID", query={"limit": ["1"], "cursor": [first["next_cursor"]]})

    def test_cursor_cannot_survive_topology_change(self):
        issued = self.issue("both", ["project-a", "project-b"])
        first = self.read(query={"limit": ["1"]}, token=issued["credential"])
        self.register("project-a", "changed-child", "child")
        self.assert_refused("DISCOVERY_SNAPSHOT_CONFLICT", query={"limit": ["1"], "cursor": [first["next_cursor"]]}, token=issued["credential"])

    def test_bearer_holder_cannot_forge_cursor_or_read_server_signing_key(self):
        issued = self.issue("both", ["project-a", "project-b"])
        page = self.read(query={"limit": ["1"]}, token=issued["credential"])
        self.assertNotIn("cursor_key", issued)
        self.assertNotIn("cursor_key", page["scope"])
        forged = discovery._cursor(issued["credential"].encode("ascii"),
            dict(snapshot=page["snapshot_id"], offset=1, limit=1))
        self.assert_refused("DISCOVERY_CURSOR_INVALID",
            query={"limit": ["1"], "cursor": [forged]}, token=issued["credential"])
        with storage.sqlite_read_connection(self.database) as connection:
            key = bytes(connection.execute("SELECT cursor_key FROM ep_workspace_discovery_grants WHERE grant_id='both'").fetchone()[0])
        self.assertEqual(len(key), 32)
        self.assertNotIn(key.hex(), json.dumps(page))

    def test_source_refault_requires_new_snapshot_and_never_uses_cached_success(self):
        page = self.read()
        saved = self.database.with_suffix(".saved")
        self.database.rename(saved)
        with self.assertRaises(FileNotFoundError):
            self.read()
        self.assertFalse(self.database.exists())
        saved.rename(self.database)
        restored = self.read("project-a", {"expected_topology_revision": [page["topology_revision"]]})
        self.assertEqual(restored["project"]["project_id"], "project-a")
        self.database.rename(saved)
        with self.assertRaises(FileNotFoundError):
            self.read()
        self.assertFalse(self.database.exists())

    def test_unknown_duplicate_malformed_queries_and_contracts_refuse(self):
        for query in ({"limit": ["0"]}, {"limit": ["101"]}, {"limit": ["x"]},
                      {"limit": ["1", "2"]}, {"wildcard": ["true"]}):
            with self.subTest(query=query):
                with self.assertRaises(discovery.DiscoveryError):
                    self.read(query=query)
        self.assert_refused("DISCOVERY_CONTRACT_UNSUPPORTED", headers={**self.headers, "EP-Discovery-Contract": "2.0"})
        self.assert_refused("DISCOVERY_REVISION_REQUIRED", project="project-a")


class ProjectDiscoveryMigrationTests(_DiscoveryFixture, unittest.TestCase):
    def prepare_schema74(self):
        # Populate retained pre75 ledgers through their owning primitives.
        with storage.sqlite_connection(self.database) as connection:
            agent = "migration-agent"
            code = agent_trust.create_pairing_code(connection, agent)["pairing_code"]
            self.agent_credential = agent_trust.pair(connection, {"protocol_version": "1.0",
                "agent_id": agent, "pairing_code": code})["credential"]
            agent_trust.heartbeat(connection, {"protocol_version": "1.0", "agent_id": agent}, self.agent_credential)
            agent_trust.register_attachment(connection, {"protocol_version": "1.0", "agent_id": agent,
                "availability": "AVAILABLE", "attachment": {"schema_version": "1.0",
                "project": {"id": "project-a", "authority_repository_id": "core-a"},
                "repository": {"id": "child-a", "role": "child"}, "validation": {"kind": "none"}}}, self.agent_credential)
            installation_pairing.register(connection, binding_id="migration-pairing", ep_instance_id=self.instance,
                forge_instance_id="migration-forge", consumer_id="forge", operation_id="migration-register")
            self.pairing_credential = installation_pairing.issue(connection,
                binding_id="migration-pairing", operation_id="migration-pairing-issue").credential
        with storage.sqlite_connection(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            connection.execute("DROP TABLE ep_workspace_discovery_grants")
            connection.execute("DROP TABLE ep_execution_progress_grants")
            connection.execute("ALTER TABLE ep_installations RENAME TO fixture_installations75")
            connection.execute("CREATE TABLE ep_installations (instance_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 41 AND 74))")
            connection.execute("INSERT INTO ep_installations SELECT instance_id,created_at,74 FROM fixture_installations75")
            connection.execute("DROP TABLE fixture_installations75")
            connection.execute("DELETE FROM engineering_schema_migrations WHERE version>=75")
            connection.execute("INSERT INTO engineering_schema_migrations(version) VALUES(74)")
            connection.execute("UPDATE engineering_metadata SET value='74' WHERE key='installation.schema_version'")

    def registered_data(self):
        with storage.sqlite_read_connection(self.database) as connection:
            return {table: connection.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
                for table in ("ep_project_registrations", "ep_repository_registrations",
                    "ep_agent_registrations", "ep_agent_repository_attachments",
                    "ep_installation_pairings", "ep_installation_pairing_credentials")}

    def test_populated74_upgrade_preserves_registered_data_identity_and_foreign_targets(self):
        self.prepare_schema74()
        data = self.registered_data()
        self.assertTrue(all(data.values()))
        self.assertEqual(server.initialize(self.root).instance_id, self.instance)
        self.assertEqual(self.registered_data(), data)
        with storage.sqlite_read_connection(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            discovery.validate_schema(connection)
            self.assertEqual(connection.execute("SELECT schema_version FROM ep_installations").fetchall(), [(server.SERVER_STORE_SCHEMA_VERSION,)])
            self.assertEqual(connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone(), (server.SERVER_STORE_SCHEMA_VERSION,))
            self.assertIsNotNone(installation_pairing.authenticate(connection, self.pairing_credential))
            agent_trust.authenticate(connection, "migration-agent", self.agent_credential)
            self.assertNotIn(self.pairing_credential.encode(), self.database.read_bytes())
            self.assertNotIn(self.agent_credential.encode(), self.database.read_bytes())
        before = self.database.read_bytes()
        self.assertEqual(server.initialize(self.root).instance_id, self.instance)
        self.assertEqual(self.database.read_bytes(), before)
        issued = self.issue("after-upgrade", ["project-a"])
        self.assertEqual(issued["status"], "ACTIVE")

    def test_failed75_migration_rolls_back_original_database_bytes_and_data(self):
        self.prepare_schema74()
        data = self.registered_data()
        before = self.database.read_bytes()
        with patch.object(discovery, "install_schema", side_effect=sqlite3.OperationalError("synthetic migration boundary failure")):
            self.assertRaises(server.ServerConfigurationError, server.initialize, self.root)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.registered_data(), data)
        with storage.sqlite_read_connection(self.database) as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone(), (74,))
            self.assertEqual(connection.execute("SELECT schema_version FROM ep_installations").fetchall(), [(74,)])
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='ep_workspace_discovery_grants'").fetchone())


class ProjectDiscoveryHTTPTests(_DiscoveryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.listener_stopped = False
        self.listener = server._ConsoleHTTPServer(("127.0.0.1", 0), server._HealthHandler)
        self.listener.daemon_threads = False
        self.listener.data_root = self.root
        self.thread = threading.Thread(target=self.listener.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_listener)

    def stop_listener(self):
        self.listener.shutdown()
        self.listener.server_close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.listener_stopped = True

    def test_active_handler_is_joined_before_store_cleanup(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        original = server._HealthHandler._send
        responses, errors = [], []
        def hold(handler, *args, **kwargs):
            if args[0] == 200 and isinstance(args[1], dict) and "contract_version" in args[1]:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("bounded own handler hold expired")
            return original(handler, *args, **kwargs)
        def client():
            try:
                responses.append(self.request())
            except BaseException as error:
                errors.append(error)
        def stopper():
            try:
                self.stop_listener()
            except BaseException as error:
                errors.append(error)
            finally:
                stopped.set()
        with patch.object(server._HealthHandler, "_send", hold):
            request = threading.Thread(target=client)
            stop = threading.Thread(target=stopper)
            request.start()
            try:
                self.assertTrue(entered.wait(3))
                stop.start()
                self.assertFalse(stopped.wait(0.1))
                self.cleanup_store()
                self.assertTrue(self.database.exists())
            finally:
                release.set()
                request.join(5)
                if stop.ident is not None:
                    stop.join(5)
        self.assertFalse(request.is_alive())
        self.assertFalse(stop.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses[0][0], 200)
        self.assertTrue(self.listener_stopped)

    def test_failed_listener_stop_preserves_store_until_real_stop(self):
        before = self.database.read_bytes()
        with patch.object(self.listener, "server_close", side_effect=RuntimeError("synthetic unknown stop")):
            self.assertRaisesRegex(RuntimeError, "synthetic unknown stop", self.stop_listener)
        self.assertFalse(self.listener_stopped)
        self.cleanup_store()
        self.assertTrue(self.database.exists())
        self.assertEqual(self.database.read_bytes(), before)
        self.stop_listener()
        self.assertTrue(self.listener_stopped)

    def test_supported_journal_holds_owner_revocation_commit_until_publication_finishes(self):
        entered, release, staged, committed = (threading.Event() for _ in range(4))
        original = server._HealthHandler._send
        responses, errors, publication_times, commit_times = [], [], [], []
        def hold(handler, *args, **kwargs):
            if args[0] == 200 and isinstance(args[1], dict) and "contract_version" in args[1]:
                entered.set()
                if not release.wait(4):
                    raise TimeoutError("bounded own publication hold expired")
                result = original(handler, *args, **kwargs)
                publication_times.append(time.monotonic())
                return result
            return original(handler, *args, **kwargs)
        def client():
            try:
                responses.append(self.request())
            except BaseException as error:
                errors.append(error)
        def revoke():
            try:
                with storage.sqlite_connection(self.database, timeout=3) as writer:
                    discovery.revoke(writer, grant_id="grant-a", operation_id="held-publication-revoke")
                    staged.set()
                commit_times.append(time.monotonic())
                committed.set()
            except BaseException as error:
                errors.append(error)
        request, writer = threading.Thread(target=client), threading.Thread(target=revoke)
        with patch.object(server._HealthHandler, "_send", hold):
            request.start()
            try:
                self.assertTrue(entered.wait(2))
                writer.start()
                self.assertTrue(staged.wait(1))
                self.assertFalse(committed.wait(0.1))
            finally:
                release.set()
                request.join(5)
                if writer.ident is not None:
                    writer.join(5)
        self.assertFalse(request.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(committed.is_set())
        self.assertLess(publication_times[0], commit_times[0])
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(self.request()[0], 401)

    def request(self, path="/v1/onboarding/projects", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.listener.server_address[1], timeout=5)
        try:
            connection.request("GET", path, body=body, headers={"Authorization": "Bearer " + self.token,
                **(self.headers if headers is None else headers)})
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            return response.status, payload
        finally:
            connection.close()

    def test_actual_http_discovery_selected_topology_and_no_database_changes(self):
        before = self.database.read_bytes()
        code, page = self.request()
        self.assertEqual(code, 200)
        code, detail = self.request("/v1/onboarding/projects/project-a/topology?expected_topology_revision=" + page["topology_revision"])
        self.assertEqual(code, 200)
        self.assertEqual(detail["project"]["authority_repository_id"], "core-a")
        self.assertEqual(len(detail["repositories"]), 2)
        self.assertEqual(self.database.read_bytes(), before)

    def test_actual_http_responses_validate_against_packaged_closed_schema(self):
        schema = json.loads((Path(discovery.__file__).parent / "schemas" /
            "workspace-project-discovery-v1.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        code, page = self.request()
        self.assertEqual(code, 200)
        validator.validate(page)
        code, detail = self.request("/v1/onboarding/projects/project-a/topology?expected_topology_revision=" + page["topology_revision"])
        self.assertEqual(code, 200)
        validator.validate(detail)
        self.assertTrue(list(validator.iter_errors({**page, "unauthorized_extra_field": "x"})))

    def test_actual_http_revocation_wrong_audience_body_and_unsupported(self):
        self.assertEqual(self.request(headers={**self.headers, "EP-Audience": "other"})[0], 403)
        self.assertEqual(self.request(headers={**self.headers, "EP-Discovery-Contract": "2.0"})[0], 422)
        self.assertEqual(self.request(body="not-allowed")[0], 400)
        with storage.sqlite_connection(self.database) as connection:
            discovery.revoke(connection, grant_id="grant-a", operation_id="http-revoke")
        self.assertEqual(self.request()[0], 401)

    def test_actual_http_missing_source_is503_and_never_creates_database(self):
        self.database.rename(self.database.with_suffix(".saved"))
        self.assertEqual(self.request(), (503, {"error": "CENTRAL_UNAVAILABLE"}))
        self.assertFalse(self.database.exists())

    def test_actual_http_corruption_stays_red_not_a_healthy_empty_projection(self):
        self.database.rename(self.database.with_suffix(".saved"))
        self.database.write_bytes(b"corrupt synthetic qualification database")
        corrupted = self.database.read_bytes()
        self.assertEqual(self.request(), (500, {"error": "CENTRAL_CORRUPT"}))
        self.assertEqual(self.database.read_bytes(), corrupted)

    def test_actual_http_source_loss_during_response_encoding_is503_before_headers(self):
        original = json.dumps
        saved = self.database.with_suffix(".saved")
        def disappear(payload, *args, **kwargs):
            if isinstance(payload, dict) and payload.get("contract_version") == "1.0":
                self.database.rename(saved)
            return original(payload, *args, **kwargs)
        with patch.object(server.json, "dumps", side_effect=disappear):
            self.assertEqual(self.request(), (503, {"error": "CENTRAL_UNAVAILABLE"}))
        self.assertFalse(self.database.exists())
        self.assertTrue(saved.exists())

    def test_actual_http_source_loss_during_final_authority_check_is503(self):
        original = discovery._authenticate
        calls = 0
        saved = self.database.with_suffix(".saved")
        def lose_source(connection, token, headers):
            nonlocal calls
            result = original(connection, token, headers)
            calls += 1
            if calls == 5:
                self.database.rename(saved)
            return result
        with patch.object(discovery, "_authenticate", side_effect=lose_source):
            self.assertEqual(self.request(), (503, {"error": "CENTRAL_UNAVAILABLE"}))
        self.assertEqual(calls, 5)
        self.assertFalse(self.database.exists())
        self.assertTrue(saved.exists())

    def test_actual_http_different_instance_source_replacement_cannot_publish_old_success(self):
        replacement = self.root / "replacement"
        replacement_instance = server.initialize(replacement).instance_id
        self.assertNotEqual(replacement_instance, self.instance)
        replacement_database = replacement / server.SERVER_DATABASE_FILENAME
        expected = replacement_database.read_bytes()
        original = discovery._authenticate
        calls = 0
        saved = self.database.with_suffix(".saved")
        def replace_source(connection, token, headers):
            nonlocal calls
            result = original(connection, token, headers)
            calls += 1
            if calls == 5:
                self.database.rename(saved)
                replacement_database.rename(self.database)
            return result
        with patch.object(discovery, "_authenticate", side_effect=replace_source):
            self.assertEqual(self.request(), (409, {"error": "DISCOVERY_SNAPSHOT_CONFLICT"}))
        self.assertEqual(calls, 5)
        self.assertEqual(self.database.read_bytes(), expected)
        self.assertTrue(saved.exists())


if __name__ == "__main__":
    unittest.main()
