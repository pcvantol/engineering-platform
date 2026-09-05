"""High-value boundary tests for the supported installation runtime.

These tests deliberately exercise denial, persistence and lifecycle paths at
their public module boundaries.  They are not line-execution fixtures.
"""
from __future__ import annotations

import json
import io
import runpy
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import socket
from contextlib import redirect_stdout

from engineering_platform import agent_trust, central_database, project_topology, server, server_relay
from engineering_platform import providers
from engineering_platform import codex_capacity
from engineering_platform import pr_check_repair
from engineering_platform import emergency_recovery
from engineering_platform.agent_state import StateError, StateStore, TransactionState
from engineering_platform import local_api_keychain
from engineering_platform import dependabot_producer
from engineering_platform import provider_recovery
from engineering_platform import prompt_history
from engineering_platform import investigation_ledger, legacy_inbox_migration, provider_process_identity, provider_readiness, resources, validation_identity, worktree_tooling
from engineering_platform.local_api import valid_port
from engineering_platform.local_api_credentials import (
    CredentialAuthority,
    disable_consumer,
    issue_credential,
    register_consumer,
    revoke_consumer,
)
from engineering_platform.project_topology import TopologyRegistrationError, register_server_local_topology


class AgentTrustSecurityBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        agent_trust.install_schema(self.connection)
        self.agent_id = "agent-secure-1"

    def tearDown(self) -> None:
        self.connection.close()

    def _pair(self) -> str:
        code = agent_trust.create_pairing_code(self.connection, self.agent_id)["pairing_code"]
        return agent_trust.pair(self.connection, {
            "protocol_version": agent_trust.PROTOCOL_VERSION,
            "agent_id": self.agent_id,
            "pairing_code": code,
        })["credential"]

    def test_pairing_is_single_use_and_credentials_are_agent_scoped(self) -> None:
        code = agent_trust.create_pairing_code(self.connection, self.agent_id)["pairing_code"]
        paired = agent_trust.pair(self.connection, {
            "protocol_version": agent_trust.PROTOCOL_VERSION,
            "agent_id": self.agent_id,
            "pairing_code": code,
        })
        with self.assertRaisesRegex(agent_trust.AgentTrustError, "not approved"):
            agent_trust.pair(self.connection, {"protocol_version": "1.0", "agent_id": self.agent_id, "pairing_code": code})
        agent_trust.authenticate(self.connection, self.agent_id, paired["credential"])
        with self.assertRaisesRegex(agent_trust.AgentTrustError, "invalid credential"):
            agent_trust.authenticate(self.connection, self.agent_id, "wrong-token")

    def test_registration_rejects_cross_shape_metadata_then_tracks_liveness(self) -> None:
        token = self._pair()
        malformed = {"protocol_version": "1.0", "agent_id": self.agent_id, "host": {}, "capabilities": {}, "repositories": []}
        with self.assertRaisesRegex(agent_trust.AgentTrustError, "host metadata"):
            agent_trust.register(self.connection, malformed, token)
        body = {
            "protocol_version": "1.0", "agent_id": self.agent_id,
            "host": {"hostname": "host", "os_user": "operator", "operating_system": "macOS", "architecture": "arm64"},
            "capabilities": {"read": True}, "repositories": [{"attachment": {"project_id": "project-a"}}],
        }
        self.assertEqual(agent_trust.register(self.connection, body, token)["state"], "REGISTERED")
        self.assertEqual(agent_trust.heartbeat(self.connection, {"protocol_version": "1.0", "agent_id": self.agent_id}, token)["state"], "ONLINE")
        self.assertEqual(agent_trust.registration_status(self.connection, self.agent_id)["liveness"], "ONLINE")
        self.assertTrue(agent_trust.revoke(self.connection, self.agent_id))
        with self.assertRaisesRegex(agent_trust.AgentTrustError, "revoked"):
            agent_trust.authenticate(self.connection, self.agent_id, token)
        self.assertTrue(agent_trust.reset(self.connection, self.agent_id))


class CentralAuthorityBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "server"
        server.initialize(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_configuration_is_bounded_and_persisted_only_in_central_store(self) -> None:
        self.assertEqual(central_database.update_maintenance_configuration(self.root, 60)["interval_seconds"], 60)
        self.assertEqual(central_database.update_capacity_configuration(self.root, 25)["codex_capacity_reserve_percent"], 25)
        changed = central_database.update_console_interval_configuration(self.root, "log_level", "DEBUG")
        self.assertEqual(changed["previous"], "INFO")
        self.assertEqual(central_database.console_interval_configuration(self.root)["log_level"], "DEBUG")
        for call in (
            lambda: central_database.update_maintenance_configuration(self.root, True),
            lambda: central_database.update_capacity_configuration(self.root, 99),
            lambda: central_database.update_console_interval_configuration(self.root, "unknown", 1),
        ):
            with self.assertRaises(ValueError):
                call()

    def test_capacity_history_rejects_invalid_samples_and_bounds_history(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertEqual(central_database.record_provider_capacity(self.root, provider="", remaining_percent=20), [])
        self.assertEqual(central_database.record_provider_capacity(self.root, provider="codex", remaining_percent=101), [])
        first = central_database.record_provider_capacity(self.root, provider="codex", remaining_percent=80, observed_at=now)
        second = central_database.record_provider_capacity(self.root, provider="codex", remaining_percent=20, observed_at=now)
        self.assertEqual(first[0]["remaining_percent"], 80.0)
        self.assertEqual(second[0]["remaining_percent"], 20.0)
        self.assertEqual(central_database.provider_capacity_history(self.root, provider="codex", hours=0), [])

    def test_maintenance_never_compacts_while_central_execution_is_active(self) -> None:
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("INSERT INTO ep_project_registrations(project_id,attachment_contract,status,created_at,updated_at) VALUES(?,?,?,?,?)", ("project-a", "DECLARATION", "ACTIVE", "now", "now"))
            connection.execute("INSERT INTO ep_repository_registrations(repository_id,project_id,authority_repository_id,role,attachment_contract,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", ("repo-a", "project-a", "repo-a", "authority", "DECLARATION", "now", "now"))
            connection.execute("INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)", ("run-a", "project-a", "RUNNING", "now", "now"))
            connection.execute("INSERT INTO ep_submissions(submission_id,project_id,repository_id,producer_id,producer_type,transport,prompt,prompt_digest,constraints,state,admission,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("sub-a", "project-a", "repo-a", "test", "TEST", "HTTP", "p", "d", "{}", "QUEUED", "ADMITTED", "now"))
            connection.execute("INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("sub-a", "project-a", "repo-a", "run-a", "RUNNING", "CENTRAL:p", "now", "now"))
        self.assertEqual(central_database.run_periodic_maintenance(self.root)["state"], "SKIPPED_ACTIVE_RUN")


class InstallationBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "server"
        server.initialize(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_attachment_headers_fail_closed_and_log_queries_stay_canonical(self) -> None:
        self.assertEqual(server._attachment_content_disposition("safe-report.md"), 'attachment; filename="safe-report.md"')
        for value in ("bad\r\nheader", "../../secrets", 7):
            with self.assertRaises(ValueError):
                server._attachment_content_disposition(value)
        with self.assertRaises(ValueError):
            server._report_content_disposition("UpperCase")
        self.assertIsNone(server._central_log_components("dashboard"))
        self.assertIsNotNone(server._central_log_components("all"))
        with self.assertRaises(ValueError):
            server._parse_central_log_query({"page": ["0"]})

    def test_queue_overlay_preserves_invalid_payload_and_records_central_capacity(self) -> None:
        self.assertEqual(server._with_console_queue(b"not-json", queue={}, data_root=self.root), b"not-json")
        payload = json.dumps({"runs": [{"run_id": "r1"}], "status": {}, "rate_limits": {"provider": "codex", "windows": [{"used_percent": 70}]}}).encode()
        result = json.loads(server._with_console_queue(payload, queue={"operator_handling": {"r1": "DISMISSED"}}, data_root=self.root))
        self.assertTrue(result["runs"][0]["dismissed"])
        self.assertEqual(result["capacity_scope"], "EP")

    def test_local_credentials_enforce_consumer_and_project_authority(self) -> None:
        register_consumer(self.root, consumer_id="consumer-a", project_id="project-a")
        issued = issue_credential(self.root, consumer_id="consumer-a", project_id="project-a")
        authority = CredentialAuthority(self.root)
        scope = authority.authenticate(issued.credential)
        self.assertIsNotNone(scope)
        self.assertTrue(authority.authorized(scope))
        self.assertTrue(disable_consumer(self.root, consumer_id="consumer-a", project_id="project-a"))
        self.assertFalse(authority.authorized(scope))
        # A disabled consumer cannot be silently promoted to revoked: stale
        # operator actions fail closed instead of rewriting audit state.
        with self.assertRaisesRegex(ValueError, "state conflicts"):
            revoke_consumer(self.root, consumer_id="consumer-a", project_id="project-a")
        # Authentication returns a credential scope, but that scope has no
        # capability once its registration is disabled.
        self.assertFalse(authority.authorized(authority.authenticate(issued.credential)))
        for value in (True, 80, 1023, 65536):
            with self.assertRaises(ValueError):
                valid_port(value)

    def test_relay_lifecycle_is_server_owned_and_build_failures_are_explicit(self) -> None:
        with patch("engineering_platform.server_relay.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Swift compiler"):
                server_relay.build_relay(self.root)
        binary = self.root / "runtime" / "relay"
        with patch("engineering_platform.server_relay.build_relay", return_value=binary), patch("engineering_platform.server_relay.render_launch_agent", return_value=Path("/tmp/relay.plist")), patch("engineering_platform.server_relay.LaunchdProvider") as launchd:
            result = server_relay.install(self.root)
        self.assertEqual(result["component"], "dashboard_relay")
        launchd.return_value.install.assert_called_once()

    def test_topology_rejects_malformed_server_declarations(self) -> None:
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            with self.assertRaises(TopologyRegistrationError):
                register_server_local_topology(connection, declaration={"project_id": "wrong"})

    def test_console_platform_routes_preserve_owner_and_fail_closed_mutations(self) -> None:
        """Exercise installed Console routes without a selected checkout."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # This instance is deliberately separate because ``server.start``
        # binds the configured port once and owns its lifecycle worker.
        live_root = self.root.parent / "live-server"
        server.initialize(live_root, bind_port=port)
        server.start(live_root)
        try:
            for route in (
                "/api/platform-status", "/api/dashboard-snapshot", "/api/configuration",
                "/api/execution-runtime-status", "/api/github-rate-limit",
                "/api/host-admin/diagnostics", "/api/provider-login-status",
                "/api/process-metrics", "/api/usage", "/api/logs/all",
            ):
                with urlopen(f"http://127.0.0.1:{port}{route}") as response:
                    self.assertEqual(
                        response.headers["EP-Console-Route-Owner"],
                        "HOST_ADMIN" if route == "/api/host-admin/diagnostics" else "PLATFORM",
                    )
                    self.assertIsInstance(json.loads(response.read()), dict)
            for route, body, expected in (
                ("/api/configuration/inbox-location", b"{}", 410),
                ("/api/runtime-directory/open", b"{}", 410),
                ("/api/central-database/configuration", b"{}", 400),
                ("/api/logs/all", b'{"component":"dashboard"}', 400),
                ("/api/provider-capacity", b"{}", 405),
            ):
                request = Request(f"http://127.0.0.1:{port}{route}", data=body, method="POST", headers={"Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(request)
                self.assertEqual(error.exception.code, expected)
                self.assertEqual(json.loads(error.exception.read())["error"], {
                    410: "INBOX_WATCHER_CONFIGURATION_RETIRED" if route.endswith("inbox-location") else "RUNTIME_DIRECTORY_RETIRED",
                    400: "CENTRAL_DATABASE_MAINTENANCE_INTERVAL_INVALID" if "central" in route else "LOG_COMPONENT_INVALID",
                    405: "METHOD_NOT_ALLOWED",
                }[expected])
        finally:
            server.stop(live_root)

    def _in_process_console_handler(self, path: str, *, body: bytes = b"", headers: dict[str, str] | None = None):
        """Exercise the installed handler in-process, where branch coverage runs."""
        handler = object.__new__(server._HealthHandler)
        handler.path = path
        handler.server = SimpleNamespace(data_root=self.root)
        message = Message()
        for key, value in (headers or {}).items():
            message[key] = value
        handler.headers = message
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        responses: list[tuple[int, dict[str, object]]] = []
        handler._send = lambda status, payload, instance_id=None: responses.append((status, payload))
        handler._send_ndjson = lambda entries: responses.append((200, {"entries": entries, "ndjson": True}))
        handler._send_console_asset = lambda request: False
        return handler, responses

    def test_in_process_console_dispatch_covers_platform_success_and_denial_contracts(self) -> None:
        """The routes are Server-owned even when no project is selected."""
        for path in (
            "/api/logs/dashboard", "/health", "/api/host-admin/diagnostics",
            "/api/components/operations_console/details", "/api/provider-capacity",
            "/api/provider-login-status", "/api/execution-runtime-status",
            "/api/provider-capacity/configuration", "/api/configuration", "/",
            "/not-a-console-route",
        ):
            handler, responses = self._in_process_console_handler(path)
            handler._delegate_dashboard("do_GET")
            self.assertTrue(responses or path == "/", path)
        for path, body in (
            ("/api/provider-login/repair", b"{}"),
            ("/api/provider-login/logout", b"{}"),
            ("/api/execution-runtime/repair", b"not-json"),
            ("/api/provider-capacity/configuration", b"{}"),
            ("/api/configuration", b"{}"),
            ("/api/components/operations_console/restart", b"bad"),
            ("/api/execution-dismiss", b"{}"),
            ("/api/dashboard-translate", b"{}"),
        ):
            handler, responses = self._in_process_console_handler(path, body=body, headers={"Content-Length": str(len(body))})
            handler._delegate_dashboard("do_POST")
            self.assertTrue(responses, path)
            self.assertGreaterEqual(responses[-1][0], 400)

    def test_selected_project_dispatch_never_delegates_to_checkout_runtime(self) -> None:
        """Project read routes retain CENTRAL data and unavailable mutations fail closed."""
        projects = [{"project_id": "project-a"}]
        snapshot = {"runs": [], "status": {"project_id": "project-a"}}
        with patch("engineering_platform.server._console_projects", return_value=projects), patch(
            "engineering_platform.server._central_console_project_snapshot", return_value=snapshot
        ), patch("engineering_platform.server._central_console_component_logs", return_value={"entries": []}), patch(
            "engineering_platform.server._central_console_chat_history", return_value=[]
        ), patch("engineering_platform.server._central_console_run_detail", return_value={"run_id": "run-a"}), patch(
            "engineering_platform.server._central_console_telemetry_detail", return_value={"runs": []}
        ):
            headers = {"X-Engineering-Platform-Project": "project-a"}
            for path in (
                "/api/configuration", "/api/logs/all", "/api/dashboard-snapshot",
                "/api/status", "/api/prompt-history", "/api/prompt-history/run-a/chat",
                "/api/prompt-history/run-a/details", "/api/telemetry/2026-01-01",
                "/api/unsupported",
            ):
                handler, responses = self._in_process_console_handler(path, headers=headers)
                if path == "/api/prompt-history":
                    handler.send_response = lambda *args: None
                    handler.send_header = lambda *args: None
                    handler.end_headers = lambda: None
                    handler.wfile = io.BytesIO()
                handler._delegate_dashboard("do_GET")
                self.assertTrue(responses or path == "/api/prompt-history", path)
            handler, responses = self._in_process_console_handler("/api/configuration", body=b"{}", headers={**headers, "Content-Length": "2"})
            handler._delegate_dashboard("do_POST")
            self.assertEqual(responses[-1], (409, {"error": "CONSOLE_CONFIGURATION_INVALID"}))

    def test_console_dispatch_success_paths_require_server_services_not_checkout_state(self) -> None:
        """Successes are explicit Server service calls, not legacy fallthrough."""
        projects = [{"project_id": "project-a"}]
        with patch("engineering_platform.server._console_projects", return_value=projects), patch(
            "engineering_platform.server._platform_component_detail", return_value={"component": "operations_console"}
        ), patch("engineering_platform.server._restart_platform_component", return_value={"restarted": True}), patch(
            "engineering_platform.server._central_provider_repair"
        ) as repair, patch("engineering_platform.server._central_provider_logout") as logout, patch(
            "engineering_platform.server._provider_capacity_projection", return_value={"rate_limits": {"windows": [{"used_percent": 50}]}}
        ):
            # Explicit platform mutations are bounded and report their result.
            cases = (
                ("/api/components/operations_console/restart", b"{}"),
                ("/api/execution-runtime/repair", b"{}"),
                ("/api/provider-login/repair", b'{"provider":"CODEX","action":"login"}'),
                ("/api/provider-login/logout", b'{"provider":"CODEX"}'),
                ("/api/provider-capacity/configuration", b'{"codex_capacity_reserve_percent":0}'),
                ("/api/configuration", b'{"key":"log_level","value":"DEBUG","previous":"INFO"}'),
            )
            for path, body in cases:
                handler, responses = self._in_process_console_handler(path, body=body, headers={"Content-Length": str(len(body))})
                handler._delegate_dashboard("do_POST")
                self.assertIn(responses[-1][0], {200, 202}, path)
            self.assertTrue(repair.called)
            self.assertTrue(logout.called)
            # Project data routes use CENTRAL projections even if their detail
            # is absent; no physical project root is consulted.
            with patch("engineering_platform.server._central_console_report", return_value=b"report"), patch(
                "engineering_platform.server._central_console_chat_history", return_value=[]
            ), patch("engineering_platform.server._central_console_run_detail", return_value={"run_id": "run-a"}), patch(
                "engineering_platform.server._central_console_telemetry_detail", return_value={"runs": []}
            ), patch("engineering_platform.server._central_console_component_logs", return_value={"entries": []}):
                headers = {"X-Engineering-Platform-Project": "project-a"}
                for path in ("/", "/api/logs/all", "/api/prompt-history/run-a/report", "/api/prompt-history/run-a/chat", "/api/prompt-history/run-a/details", "/api/telemetry/2026-01-01"):
                    handler, responses = self._in_process_console_handler(path, headers=headers)
                    handler._delegate_dashboard("do_GET")
                    self.assertTrue(responses or handler.wfile.getvalue(), path)

    def test_agent_http_transport_rejects_malformed_and_accepts_one_bounded_pairing(self) -> None:
        """Agent HTTP parsing is a Server boundary, not a direct database API."""
        malformed, responses = self._in_process_console_handler(
            "/v1/agent/pair", body=b"{}", headers={"Content-Length": "2"}
        )
        malformed.do_POST()
        self.assertEqual(responses[-1][0], 400)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            code = agent_trust.create_pairing_code(connection, "agent-http-1")["pairing_code"]
        body = json.dumps({"protocol_version": "1.0", "agent_id": "agent-http-1", "pairing_code": code}).encode()
        paired, responses = self._in_process_console_handler(
            "/v1/agent/pair", body=body, headers={"Content-Length": str(len(body)), "Content-Type": "application/json"}
        )
        paired.do_POST()
        self.assertEqual(responses[-1][0], 200)
        unknown, responses = self._in_process_console_handler("/v1/unknown", body=b"{}", headers={"Content-Length": "2"})
        unknown.send_error = lambda code: responses.append((code, {"error": "not found"}))
        unknown.do_POST()
        self.assertEqual(responses[-1][0], 404)

    def test_server_cli_core_diagnostics_are_installation_scoped(self) -> None:
        """CLI read commands exercise the same Server-owned data root."""
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(server.main(["init", "--data-root", str(self.root.parent / "cli")]), 0)
            self.assertEqual(server.main(["status", "--data-root", str(self.root.parent / "cli")]), 0)
            self.assertEqual(server.main(["health", "--data-root", str(self.root.parent / "cli")]), 1)
            self.assertEqual(server.main(["topology", "--data-root", str(self.root.parent / "cli")]), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(records[0]["initialized"])
        self.assertIn("operational_state", records[1])
        self.assertIn("healthy", records[2])
        self.assertIn("projects", records[3])

    def test_central_log_query_filters_only_canonical_central_events(self) -> None:
        """Log filtering handles malformed storage and rejects unsafe query input."""
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)", ("operations_console", '{"event":"ready","level":"INFO"}', "2026-01-01T00:00:00+00:00"))
        page = server._central_console_component_logs(self.root, "operations_console", {"level": ["INFO"], "event": ["ready"], "sort": ["event"], "direction": ["asc"]})
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["entries"][0]["event"], "ready")
        self.assertIsNone(server._central_console_component_logs(self.root, "dashboard"))
        for query in ({"search": ["x" * 161]}, {"sort": ["DROP"]}, {"level": ["TRACE"]}):
            with self.assertRaises(ValueError):
                server._central_console_component_logs(self.root, "all", query)

    def test_cli_retained_lifecycle_commands_expose_only_real_server_owners(self) -> None:
        output = io.StringIO()
        cli_root = self.root.parent / "cli-lifecycle"
        with patch("engineering_platform.server.server_relay.install", return_value={"component": "dashboard_relay"}), patch(
            "engineering_platform.server.server_relay.uninstall", return_value={"component": "dashboard_relay"}
        ), redirect_stdout(output):
            self.assertEqual(server.main(["relay-install", "--data-root", str(cli_root)]), 0)
            self.assertEqual(server.main(["relay-uninstall", "--data-root", str(cli_root)]), 0)
            self.assertEqual(server.main(["pairing-create", "--data-root", str(cli_root), "--agent-id", "agent-cli-1"]), 0)
            self.assertEqual(server.main(["agent-revoke", "--data-root", str(cli_root), "--agent-id", "agent-cli-1"]), 0)
        self.assertEqual([json.loads(line)["result"] for line in output.getvalue().splitlines()[:2]], ["INSTALLED", "UNINSTALLED"])

    def test_server_owned_file_inbox_admission_preserves_transport_provenance(self) -> None:
        """File Inbox can enter CENTRAL, but cannot select an execution owner."""
        envelope = {"project_id": "project-a", "submission": {"repository_id": "repo-a", "prompt": "test"}}
        request = Mock()
        result = Mock(); result.to_dict.return_value = {"submission_id": "sub-a"}
        with patch("engineering_platform.server.submission_service.request_from_mapping", return_value=request) as parse, patch(
            "engineering_platform.server.submission_service.submit", return_value=result
        ):
            self.assertEqual(server._admit_server_owned_file_inbox(self.root, envelope, "receipt-a", "2026-01-01T00:00:00+00:00"), {"submission_id": "sub-a"})
        payload = parse.call_args.args[1]
        self.assertEqual(payload["idempotency_key"], "receipt-a")
        self.assertEqual(payload["constraints"]["transport_principal"], "FILE_INBOX")
        with self.assertRaises(Exception):
            server._admit_server_owned_file_inbox(self.root, {}, "receipt-a", "now")

    def test_provider_installation_refuses_active_execution_and_verifies_result(self) -> None:
        with patch("engineering_platform.server._central_execution_active", return_value=True):
            with self.assertRaisesRegex(ValueError, "execution is active"):
                server._install_provider(self.root, "CODEX")
        with patch("engineering_platform.server._central_execution_active", return_value=False), patch(
            "engineering_platform.server.managed_codex_runtime.provision"
        ), patch("engineering_platform.server._central_provider_readiness", return_value={"codex": {"state": "READY"}}):
            server._install_provider(self.root, "CODEX")

    def test_console_streams_emit_central_snapshot_then_close_cleanly(self) -> None:
        """Broken client sockets terminate streams without changing CENTRAL state."""
        handler, _ = self._in_process_console_handler("/api/events")
        handler.wfile = Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        with patch("engineering_platform.server.central_database.console_interval_configuration", return_value={"dashboard_stream_interval_seconds": 1}):
            handler._stream_no_project_console_events()
            handler._stream_project_console_events("project-a")

    def test_serve_composes_only_server_owned_children_and_cleans_runtime_receipt(self) -> None:
        """The installed process starts/stops its worker, inbox and relay-independent producer."""
        fake_http = Mock()
        fake_worker = Mock(); fake_worker.wait_until_running.return_value = True
        fake_inbox = Mock(); fake_producer = Mock()
        with patch("engineering_platform.server.http.server.ThreadingHTTPServer", return_value=fake_http), patch(
            "engineering_platform.lifecycle_worker.LifecycleWorker", return_value=fake_worker
        ), patch("engineering_platform.server.file_inbox.FileInboxService", return_value=fake_inbox), patch(
            "engineering_platform.server.dependabot_producer.DependabotService", return_value=fake_producer
        ), patch("engineering_platform.server.signal.signal"), patch("engineering_platform.server.log_event"):
            self.assertEqual(server.serve(self.root), 0)
        fake_worker.start.assert_called_once(); fake_worker.stop.assert_called_once()
        fake_inbox.start.assert_called_once(); fake_inbox.stop.assert_called_once()
        self.assertFalse((self.root / server.SERVER_RUNTIME_FILENAME).exists())

    def test_cli_bootstrap_and_credential_commands_are_central_only(self) -> None:
        root = self.root.parent / "cli-authority"
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(server.main(["bootstrap-topology", "--data-root", str(root), "--project-id", "project-a", "--repository-id", "repo-a"]), 0)
            self.assertEqual(server.main(["issue-consumer-credential", "--data-root", str(root), "--project-id", "project-a", "--consumer-id", "consumer-a"]), 0)
            self.assertEqual(server.main(["bind-repository", "--data-root", str(root), "--project-id", "project-a", "--repository-id", "repo-a"]), 2)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0]["project_id"], "project-a")
        self.assertEqual(records[1]["project_id"], "project-a")
        self.assertIn("--path", records[2]["error"])

    def test_provider_and_rate_limit_helpers_fail_closed_without_credentials(self) -> None:
        with patch("engineering_platform.server.GitHubProvider") as github:
            github.return_value.github.return_value = json.dumps({"resources": {"core": {"remaining": 0, "reset": 99}}})
            self.assertEqual(server._github_rate_limit_status(), {"limited": True, "reset_at": 99})
            github.return_value.github.side_effect = OSError("rate limit unavailable")
            self.assertTrue(server._github_rate_limit_status()["limited"])
        self.assertEqual(server._normalize_rate_limits({"rateLimits": {"primary": {"usedPercent": 120, "windowDurationMins": 300, "resetsAt": 1}}})["windows"][0]["used_percent"], 100)
        self.assertEqual(server._rate_limit_window_label(1440), "1-daags venster")
        with self.assertRaises(ValueError):
            server._central_provider_repair(self.root, {"provider": "UNKNOWN", "action": "login"})
        with self.assertRaises(ValueError):
            server._central_provider_logout(self.root, {"provider": "UNKNOWN"})

    def test_agent_http_register_and_heartbeat_reject_missing_authentication(self) -> None:
        for path, payload in (
            ("/v1/agent/register", {"protocol_version": "1.0", "agent_id": "agent-http-1"}),
            ("/v1/agent/heartbeat", {"protocol_version": "1.0", "agent_id": "agent-http-1"}),
            ("/v1/agent/attachment", {"protocol_version": "1.0", "agent_id": "agent-http-1"}),
        ):
            body = json.dumps(payload).encode()
            handler, responses = self._in_process_console_handler(path, body=body, headers={"Content-Length": str(len(body))})
            handler.do_POST()
            self.assertEqual(responses[-1][0], 401)

    def test_server_http_entrypoints_reject_unknown_and_unauthenticated_ingress(self) -> None:
        handler, responses = self._in_process_console_handler("/unknown")
        handler.send_error = lambda status: responses.append((status, {"error": "not found"}))
        handler.do_GET()
        self.assertEqual(responses[-1][0], 404)
        handler, responses = self._in_process_console_handler("/healthz")
        handler._status = lambda: {"instance_id": "instance", "store": "ready"}
        handler.do_GET()
        self.assertEqual(responses[-1][0], 200)
        body = b"{}"
        handler, responses = self._in_process_console_handler(
            "/v1/projects/project-a/submissions", body=body, headers={"Content-Length": str(len(body)), "Content-Type": "text/plain"}
        )
        handler.do_POST()
        self.assertEqual(responses[-1], (415, {"error": "UNSUPPORTED_MEDIA_TYPE"}))

    def test_console_http_response_and_asset_contracts_are_installation_scoped(self) -> None:
        """Console responses carry route ownership and serve only installed assets."""
        handler = object.__new__(server._HealthHandler)
        handler.wfile = io.BytesIO()
        headers: list[tuple[str, str]] = []
        status_codes: list[int] = []
        handler.send_response = status_codes.append
        handler.send_header = lambda key, value: headers.append((key, value))
        handler.end_headers = lambda: None
        handler._console_route = SimpleNamespace(owner="PLATFORM")
        handler._send(200, {"ok": True}, "instance-a")
        self.assertEqual(status_codes, [200])
        self.assertIn(("EP-Console-Route-Owner", "PLATFORM"), headers)
        self.assertEqual(json.loads(handler.wfile.getvalue()), {"ok": True})
        handler.wfile = io.BytesIO(); headers.clear(); status_codes.clear()
        handler._send_ndjson([{"event": "one"}])
        self.assertEqual(handler.wfile.getvalue(), b'{"event": "one"}\n')
        self.assertIn(("Content-Type", "application/x-ndjson; charset=utf-8"), headers)
        handler.send_error = lambda code: status_codes.append(code)
        self.assertFalse(handler._send_console_asset(server.urlsplit("/assets/not-present.js")))
        with tempfile.TemporaryDirectory() as directory, patch.object(
            server.console_presentation, "ASSET_DIRECTORY", Path(directory)
        ):
            Path(directory, "dashboard.js").write_bytes(b"console")
            handler.wfile = io.BytesIO(); headers.clear(); status_codes.clear()
            self.assertTrue(handler._send_console_asset(server.urlsplit("/assets/dashboard.js")))
        self.assertEqual(handler.wfile.getvalue(), b"console")
        self.assertIn(("X-Content-Type-Options", "nosniff"), headers)

    def test_central_database_console_routes_validate_and_never_use_project_state(self) -> None:
        handler, responses = self._in_process_console_handler("/api/central-database/configuration")
        with patch("engineering_platform.server.central_database.maintenance_configuration", return_value={"interval_seconds": 60}):
            self.assertTrue(handler._central_database_configuration("do_GET"))
        self.assertEqual(responses[-1], (200, {"interval_seconds": 60}))
        invalid, invalid_responses = self._in_process_console_handler(
            "/api/central-database/configuration", body=b"not-json", headers={"Content-Length": "8"}
        )
        self.assertTrue(invalid._central_database_configuration("do_POST"))
        self.assertEqual(invalid_responses[-1], (400, {"error": "CENTRAL_DATABASE_MAINTENANCE_INTERVAL_INVALID"}))
        update, update_responses = self._in_process_console_handler(
            "/api/central-database/configuration", body=b'{"interval_seconds":120}', headers={"Content-Length": "24"}
        )
        with patch("engineering_platform.server.central_database.update_maintenance_configuration", return_value={"interval_seconds": 120}) as changed:
            self.assertTrue(update._central_database_configuration("do_POST"))
        changed.assert_called_once_with(self.root, 120)
        self.assertEqual(update_responses[-1], (200, {"interval_seconds": 120}))
        unavailable, unavailable_responses = self._in_process_console_handler("/api/central-database/download")
        with patch("engineering_platform.server.central_database.snapshot", return_value=None):
            self.assertTrue(unavailable._central_database_configuration("do_GET"))
        self.assertEqual(unavailable_responses[-1], (503, {"error": "CENTRAL_DATABASE_UNAVAILABLE"}))

    def test_console_event_streams_are_central_and_disconnect_safely(self) -> None:
        for name, args in (
            ("_stream_no_project_console_events", ()),
            ("_stream_project_console_events", ("project-a",)),
            ("_stream_console_events", (self.root, "project-a")),
        ):
            handler = object.__new__(server._HealthHandler)
            handler.server = SimpleNamespace(data_root=self.root)
            handler.wfile = Mock()
            handler.wfile.write.side_effect = BrokenPipeError
            handler.send_response = lambda _code: None
            handler.send_header = lambda _key, _value: None
            handler.end_headers = lambda: None
            with patch("engineering_platform.server.central_database.console_interval_configuration", return_value={"dashboard_stream_interval_seconds": 1}):
                getattr(handler, name)(*args)

    def test_console_platform_routes_are_explicit_before_project_selection(self) -> None:
        """Host-wide routes neither inspect nor infer a checkout/project."""
        with patch("engineering_platform.server._provider_capacity_projection", return_value={"rate_limits": {}}), patch(
            "engineering_platform.server._central_provider_readiness", return_value={"codex": {"state": "READY"}}
        ), patch("engineering_platform.server._execution_runtime_status", return_value={"state": "READY"}), patch(
            "engineering_platform.server._platform_component_detail", return_value={"component": "operations_console"}
        ), patch(
            "engineering_platform.server.host_admin.diagnostics", return_value={"scope": "INSTALLATION"}
        ), patch("engineering_platform.server._console_projects", return_value=[]):
            for path in (
                "/api/host-admin/diagnostics", "/api/components/operations_console/details",
                "/api/provider-capacity", "/api/provider-login-status", "/api/execution-runtime-status",
            ):
                handler, responses = self._in_process_console_handler(path)
                handler._central_database_configuration = lambda _method: False
                handler._delegate_dashboard("do_GET")
                self.assertEqual(responses[-1][0], 200, path)
            bad_component, denied = self._in_process_console_handler("/api/components/dashboard/details")
            bad_component._central_database_configuration = lambda _method: False
            bad_component._delegate_dashboard("do_GET")
            self.assertEqual(denied[-1], (409, {"error": "CONSOLE_PROJECT_UNAVAILABLE"}))

    def test_console_mutation_routes_fail_closed_and_accept_real_platform_actions(self) -> None:
        body = b"{}"
        with patch("engineering_platform.server._console_projects", return_value=[]), patch(
            "engineering_platform.server._restart_platform_component", return_value={"restarted": True}
        ), patch("engineering_platform.server._execution_runtime_status", return_value={"state": "READY"}), patch(
            "engineering_platform.server._central_provider_repair"
        ) as repair, patch("engineering_platform.server._central_provider_logout") as logout:
            restart, responses = self._in_process_console_handler(
                "/api/components/operations_console/restart", body=body, headers={"Content-Length": "2"}
            )
            restart._central_database_configuration = lambda _method: False
            restart._delegate_dashboard("do_POST")
            self.assertEqual(responses[-1][0], 202)
            runtime, responses = self._in_process_console_handler(
                "/api/execution-runtime/repair", body=body, headers={"Content-Length": "2"}
            )
            runtime._central_database_configuration = lambda _method: False
            runtime._delegate_dashboard("do_POST")
            self.assertEqual(responses[-1][0], 200)
            for path, action, expected in (
                ("/api/provider-login/repair", repair, 202),
                ("/api/provider-login/logout", logout, 200),
            ):
                payload = b'{"provider":"CODEX","action":"login"}'
                handler, responses = self._in_process_console_handler(path, body=payload, headers={"Content-Length": str(len(payload))})
                handler._central_database_configuration = lambda _method: False
                handler._delegate_dashboard("do_POST")
                self.assertEqual(responses[-1][0], expected)
                self.assertTrue(action.called)
            retired, responses = self._in_process_console_handler("/api/runtime-directory/open", body=body, headers={"Content-Length": "2"})
            retired._central_database_configuration = lambda _method: False
            retired._delegate_dashboard("do_POST")
            self.assertEqual(responses[-1], (410, {"error": "RUNTIME_DIRECTORY_RETIRED"}))

    def test_selected_project_console_reads_remain_central_when_checkout_is_absent(self) -> None:
        """Every selected-project read uses CENTRAL projections, never a bound root."""
        projects = [{"project_id": "project-a", "repository_id": "repo-a"}]
        snapshots = {"runs": [{"run_id": "run-a"}]}
        routes = (
            "/", "/api/configuration", "/api/dashboard-snapshot", "/api/status",
            "/api/prompt-history", "/api/prompt-history/run-a/report",
            "/api/prompt-history/run-a/chat", "/api/prompt-history/run-a/details",
            "/api/telemetry/2026-01-01",
        )
        with patch("engineering_platform.server._console_projects", return_value=projects), patch(
            "engineering_platform.server._selected_project_console_document", return_value=b"<main>central</main>"
        ), patch("engineering_platform.server._central_console_configuration", return_value={"scope": "CENTRAL"}), patch(
            "engineering_platform.server._central_console_project_snapshot", return_value=snapshots
        ), patch("engineering_platform.server._central_console_report", return_value=b"# central report"), patch(
            "engineering_platform.server._central_console_chat_history", return_value=[{"role": "user"}]
        ), patch("engineering_platform.server._central_console_run_detail", return_value={"run_id": "run-a"}), patch(
            "engineering_platform.server._central_console_telemetry_detail", return_value={"date": "2026-01-01"}
        ):
            for path in routes:
                handler, responses = self._in_process_console_handler(path, headers={"X-Engineering-Platform-Project": "project-a"})
                handler._central_database_configuration = lambda _method: False
                handler._delegate_dashboard("do_GET")
                self.assertTrue(responses or handler.wfile.getvalue(), path)
        missing, responses = self._in_process_console_handler(
            "/api/prompt-history/run-a/report", headers={"X-Engineering-Platform-Project": "project-a"}
        )
        missing._central_database_configuration = lambda _method: False
        with patch("engineering_platform.server._console_projects", return_value=projects), patch(
            "engineering_platform.server._central_console_report", return_value=None
        ):
            missing._delegate_dashboard("do_GET")
        self.assertEqual(responses[-1], (404, {"error": "REPORT_NOT_FOUND"}))

    def test_console_log_routes_are_central_and_mutations_reject_bad_origins(self) -> None:
        with patch("engineering_platform.server._central_console_component_logs", return_value={"entries": [{"event": "ready"}]}), patch(
            "engineering_platform.server._console_projects", return_value=[]
        ):
            logs, responses = self._in_process_console_handler("/api/logs/operations_console?format=ndjson")
            logs._central_database_configuration = lambda _method: False
            logs._delegate_dashboard("do_GET")
            self.assertTrue(responses[-1][1]["ndjson"])
        legacy, responses = self._in_process_console_handler("/api/logs/dashboard")
        legacy._central_database_configuration = lambda _method: False
        legacy._delegate_dashboard("do_GET")
        self.assertEqual(responses[-1], (410, {"error": "LEGACY_COMPONENT_LOG_ROUTE_RETIRED"}))
        bad, responses = self._in_process_console_handler(
            "/api/logs/operations_console", body=b"{}", headers={"Content-Length": "2", "Origin": "https://untrusted"}
        )
        bad._central_database_configuration = lambda _method: False
        bad._delegate_dashboard("do_POST")
        self.assertEqual(responses[-1], (400, {"error": "LOG_COMPONENT_INVALID"}))

    def test_server_cli_admin_commands_keep_state_in_the_installation(self) -> None:
        root = self.root.parent / "cli-admin"
        output = io.StringIO()
        with patch("engineering_platform.server.agent_trust.registration_status", return_value={"agent_id": "agent-cli-2", "state": "PENDING"}), patch(
            "engineering_platform.server.agent_trust.reset", return_value=True
        ), redirect_stdout(output):
            self.assertEqual(server.main(["bootstrap-topology", "--data-root", str(root), "--project-id", "project-cli", "--repository-id", "repo-cli"]), 0)
            self.assertEqual(server.main(["list-producer-bindings", "--data-root", str(root)]), 0)
            self.assertEqual(server.main(["pairing-create", "--data-root", str(root), "--agent-id", "agent-cli-2"]), 0)
            self.assertEqual(server.main(["agent-status", "--data-root", str(root), "--agent-id", "agent-cli-2"]), 0)
            self.assertEqual(server.main(["agent-reset", "--data-root", str(root), "--agent-id", "agent-cli-2"]), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0]["result"], "REGISTERED")
        self.assertEqual(records[1], {"bindings": []})
        self.assertEqual(records[3]["agent_id"], "agent-cli-2")
        with patch("engineering_platform.server.start", return_value={"running": True}) as started, redirect_stdout(io.StringIO()):
            self.assertEqual(server.main(["start", "--data-root", str(root), "--bind-port", "8899"]), 0)
        started.assert_called_once_with(root)
        with patch("engineering_platform.server.stop", return_value={"running": False}), redirect_stdout(io.StringIO()):
            self.assertEqual(server.main(["stop", "--data-root", str(root)]), 0)

    def test_no_project_console_projection_and_central_backup_are_platform_only(self) -> None:
        handler, responses = self._in_process_console_handler("/api/platform-status")
        with patch("engineering_platform.server._no_project_platform_projection", return_value={"scope": "PLATFORM"}):
            self.assertTrue(handler._no_project_platform_route("do_GET", server.urlsplit(handler.path)))
        self.assertEqual(responses[-1], (200, {"scope": "PLATFORM"}))
        for path, target, value in (
            ("/api/dashboard-snapshot", "_no_project_console_snapshot", {"projects": []}),
            ("/api/configuration", "_central_console_configuration", {"scope": "CENTRAL"}),
            ("/api/execution-runtime-status", "_execution_runtime_status", {"state": "READY"}),
            ("/api/github-rate-limit", "_github_rate_limit_status", {"limited": False}),
            ("/api/provider-login-status", "_central_provider_readiness", {"codex": {}}),
        ):
            route, route_responses = self._in_process_console_handler(path)
            with patch(f"engineering_platform.server.{target}", return_value=value):
                self.assertTrue(route._no_project_platform_route("do_GET", server.urlsplit(path)))
            self.assertEqual(route_responses[-1][0], 200)
        backup = object.__new__(server._HealthHandler)
        backup.server = SimpleNamespace(data_root=self.root)
        backup.wfile = io.BytesIO(); recorded = []
        backup.send_response = recorded.append; backup.send_header = lambda *_args: None; backup.end_headers = lambda: None
        with patch("engineering_platform.server.central_database.snapshot", return_value=b"sqlite-central"):
            backup._send_central_database_backup()
        self.assertEqual(recorded, [200]); self.assertEqual(backup.wfile.getvalue(), b"sqlite-central")

    def test_emergency_recovery_helpers_fail_closed_before_any_mutation(self) -> None:
        """Recovery accepts only an exact live, leased, managed execution."""
        completed = SimpleNamespace(returncode=0, stdout="main\n")
        with patch("engineering_platform.emergency_recovery.GitProvider") as git:
            git.return_value.execute.return_value = completed
            self.assertEqual(emergency_recovery._git(self.root, "branch", "--show-current"), "main")
            git.return_value.execute.return_value = SimpleNamespace(returncode=1, stdout="")
            with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "Git-werkmap"):
                emergency_recovery._git(self.root, "status")
        with patch("engineering_platform.emergency_recovery.load_projection", return_value={"run_id": "other"}):
            with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "huidige uitvoering"):
                emergency_recovery._live(self.root, "inbox-abcdef")
        with patch("engineering_platform.emergency_recovery.load_projection", return_value={"run_id": "inbox-abcdef", "execution_mode": "CLI"}), patch(
            "engineering_platform.emergency_recovery.liveness", return_value={"state": "LIVE"}
        ):
            with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "beheerde"):
                emergency_recovery._live(self.root, "inbox-abcdef")
        status = self.root / ".engineering" / "status"; status.mkdir(parents=True)
        runner = status / "runner_process.json"
        runner.write_text(json.dumps({"run_id": "inbox-abcdef", "pid": 0, "process_group": 2}), encoding="utf-8")
        with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "ongeldig"):
            emergency_recovery._runner(self.root, "inbox-abcdef")
        runner.write_text(json.dumps({"run_id": "other", "pid": 2, "process_group": 2}), encoding="utf-8")
        self.assertIsNone(emergency_recovery._runner(self.root, "inbox-abcdef"))

    def test_emergency_recovery_central_ownership_and_lease_release_are_exact(self) -> None:
        database = self.root / "recovery-central.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE ep_execution_runs(run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL)")
            connection.execute("INSERT INTO ep_execution_runs VALUES('inbox-abcdef','project-a')")
        emergency_recovery._require_central_project_ownership(database, "project-a", "inbox-abcdef")
        with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "behoort"):
            emergency_recovery._require_central_project_ownership(database, "project-b", "inbox-abcdef")
        connection = Mock(); connection.execute.return_value.fetchone.return_value = None
        with patch("engineering_platform.emergency_recovery.open_storage", return_value=connection):
            emergency_recovery._release_lease(self.root, "inbox-abcdef")
        connection.close.assert_called_once()
        lease_row = ("lease-a", "host", "instance", "a", "b", "c", "ACTIVE")
        connection = Mock(); connection.execute.return_value.fetchone.return_value = lease_row
        with patch("engineering_platform.emergency_recovery.open_storage", return_value=connection), patch(
            "engineering_platform.emergency_recovery.release"
        ) as release:
            emergency_recovery._release_lease(self.root, "inbox-abcdef")
        self.assertEqual(release.call_args.args[1].lease_id, "lease-a")

    def test_emergency_recovery_rejects_missing_host_and_invalid_plan_before_stop(self) -> None:
        connection = Mock(); connection.execute.return_value.fetchone.return_value = None
        with patch("engineering_platform.emergency_recovery.open_storage", return_value=connection):
            with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "Execution Host"):
                emergency_recovery._host_pid(self.root, "inbox-abcdef")
        connection.execute.return_value.fetchone.return_value = (42,)
        with patch("engineering_platform.emergency_recovery.open_storage", return_value=connection):
            self.assertEqual(emergency_recovery._host_pid(self.root, "inbox-abcdef"), 42)
        with patch("engineering_platform.emergency_recovery.LocalProcessProvider") as process:
            process.return_value.execute.return_value = SimpleNamespace(returncode=1, stdout="ignored")
            self.assertEqual(emergency_recovery._process_command(self.root, 42), "")
        with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "run-ID"):
            emergency_recovery._plan(self.root, "bad")

    def test_cli_mutation_commands_fail_closed_when_required_authority_is_absent(self) -> None:
        root = self.root.parent / "cli-denials"
        output = io.StringIO()
        commands = (
            ["submission-diagnose", "--data-root", str(root)],
            ["register-topology", "--data-root", str(root)],
            ["bootstrap-topology", "--data-root", str(root)],
            ["issue-consumer-credential", "--data-root", str(root)],
            ["register-producer-binding", "--data-root", str(root)],
            ["deactivate-producer-binding", "--data-root", str(root)],
            ["provision-declaration", "--data-root", str(root)],
            ["pairing-create", "--data-root", str(root)],
        )
        with redirect_stdout(output):
            for command in commands:
                self.assertEqual(server.main(command), 2)
        self.assertEqual(len(output.getvalue().splitlines()), len(commands))

    def test_provider_process_lifecycle_is_bounded_and_reconciled(self) -> None:
        process = Mock(); process.stdin = Mock(); process.stdout = Mock(); process.wait.side_effect = [None]
        provider = providers.CodexCliProvider.__new__(providers.CodexCliProvider); provider._executable = "/managed/codex"
        with patch("engineering_platform.providers.subprocess.Popen", return_value=process):
            opened = provider.app_server()
        self.assertIs(opened, process)
        provider.close_app_server(process)
        process.terminate.assert_called_once(); process.stdin.close.assert_called_once()
        with patch("engineering_platform.providers.shutil.which", return_value=None):
            self.assertEqual(providers.LaunchdProvider().runtime_status("com.example.service").qualified, False)

    def test_launchd_and_tailscale_diagnostics_distinguish_loaded_from_live(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="  active count = 0\n")
        with patch("engineering_platform.providers.shutil.which", return_value="/bin/launchctl"), patch(
            "engineering_platform.providers.subprocess.run", return_value=completed
        ):
            self.assertFalse(providers.LaunchdProvider().runtime_status("com.example").qualified)
        completed.stdout = "pid = 42\n"
        with patch("engineering_platform.providers.shutil.which", return_value="/bin/launchctl"), patch(
            "engineering_platform.providers.subprocess.run", return_value=completed
        ):
            self.assertTrue(providers.LaunchdProvider().runtime_status("com.example").qualified)
        tail = SimpleNamespace(returncode=0, stdout="127.0.0.1\n100.64.1.2\n")
        with patch("engineering_platform.providers.shutil.which", return_value="/bin/tailscale"), patch(
            "engineering_platform.providers.subprocess.run", return_value=tail
        ):
            self.assertEqual(providers.TailscaleProvider().ipv4_address(), "100.64.1.2")

    def test_capacity_protocol_uses_only_derived_quota_and_closes_provider(self) -> None:
        process = Mock(); process.stdin = Mock(); process.stdout = Mock()
        process.stdout.readline.side_effect = [
            json.dumps({"id": 1}),
            json.dumps({"id": 2, "result": {"rateLimits": {"primary": {"usedPercent": 35}}}}),
        ]
        provider = Mock(); provider.app_server.return_value = process
        with patch("engineering_platform.codex_capacity.CodexCliProvider", return_value=provider), patch(
            "engineering_platform.codex_capacity.select.select", return_value=([process.stdout], [], [])
        ):
            self.assertEqual(codex_capacity.read_remaining_percent(), 65.0)
        provider.close_app_server.assert_called_once_with(process)
        process.stdin.flush.assert_called()

    def test_repair_admission_requires_terminal_evidence_provider_and_capacity(self) -> None:
        root = self.root.parent / "repair"
        evidence = {"eligible": True, "head_sha": "a" * 40, "branch": "feature/a", "failed_checks": ["tests"]}
        with patch("engineering_platform.pr_check_repair.current_evidence", return_value=evidence), patch(
            "engineering_platform.pr_check_repair.provider_readiness_failures", return_value=["github"]
        ):
            with self.assertRaisesRegex(pr_check_repair.PullRequestCheckRepairError, "provider_not_ready"):
                pr_check_repair.admit(root, 1)
        with patch("engineering_platform.pr_check_repair.current_evidence", return_value=evidence), patch(
            "engineering_platform.pr_check_repair.provider_readiness_failures", return_value=[]
        ), patch("engineering_platform.pr_check_repair.read_remaining_percent", return_value=20), patch(
            "engineering_platform.pr_check_repair.capacity_reserve_from_environment", return_value=10
        ):
            admitted = pr_check_repair.admit(root, 1)
        self.assertEqual(admitted["head_sha"], "a" * 40)
        self.assertEqual(pr_check_repair.repair_state(root, 1, "a" * 40), "QUEUED")
        self.assertEqual(pr_check_repair.check_summary([{"name": "tests", "status": "IN_PROGRESS"}]), ([], False))

    def test_repair_run_rejects_stale_or_unreserved_heads_before_worktree_mutation(self) -> None:
        root, sha = self.root.parent / "repair-run", "a" * 40
        root.mkdir()
        with self.assertRaisesRegex(pr_check_repair.PullRequestCheckRepairError, "not_eligible"):
            pr_check_repair.run(root, 1, sha)
        pr_check_repair._write_state(root, 1, sha, {"status": "QUEUED", "branch": "feature/a", "failed_checks": ["tests"]})
        with patch("engineering_platform.pr_check_repair.current_evidence", return_value={"head_sha": "b" * 40, "branch": "feature/a", "checks_terminal": True, "failed_checks": ["tests"]}), patch(
            "engineering_platform.pr_check_repair._command"
        ) as command:
            with self.assertRaisesRegex(pr_check_repair.PullRequestCheckRepairError, "stale"):
                pr_check_repair.run(root, 1, sha)
        command.assert_not_called()

    def test_repair_run_submits_only_the_reserved_clean_head(self) -> None:
        root, sha, commit = self.root.parent / "repair-success", "a" * 40, "b" * 40
        (root / ".engineering").mkdir(parents=True)
        pr_check_repair._write_state(root, 7, sha, {"status": "QUEUED", "branch": "feature/repair", "failed_checks": ["tests"], "head_sha": sha})
        evidence = {"head_sha": sha, "branch": "feature/repair", "checks_terminal": True, "failed_checks": ["tests"]}
        command_results = iter(["", "", sha, "M repair.py", "", "", "", commit, "", ""])
        completed = SimpleNamespace(returncode=0)
        with patch("engineering_platform.pr_check_repair.current_evidence", return_value=evidence), patch(
            "engineering_platform.pr_check_repair._command", side_effect=lambda *args: next(command_results)
        ) as command, patch("engineering_platform.pr_check_repair._prepare_worktree_tooling"), patch(
            "engineering_platform.pr_check_repair.CodexCliProvider"
        ) as codex:
            codex.return_value.invoke.return_value = completed
            pr_check_repair.run(root, 7, sha)
        self.assertEqual(pr_check_repair.repair_state(root, 7, sha), "SUBMITTED")
        self.assertEqual(json.loads(pr_check_repair._state_path(root, 7, sha).read_text())["commit_sha"], commit)
        self.assertTrue(any("--force-with-lease=refs/heads/feature/repair:" + sha in call.args for call in command.call_args_list))

    def test_package_entrypoint_delegates_only_to_execution_host(self) -> None:
        with patch("engineering_platform.execution_host.main", return_value=7):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_module("engineering_platform.__main__", run_name="__main__")
        self.assertEqual(stopped.exception.code, 7)

    def test_emergency_stop_requires_host_exit_before_any_rollback(self) -> None:
        plan = emergency_recovery.RecoveryPlan("inbox-abcdef", "main", "main", "a" * 40, None, 123)
        with patch("engineering_platform.emergency_recovery.os.kill"), patch(
            "engineering_platform.emergency_recovery._process_command", side_effect=["execution_host", ""]
        ), patch("engineering_platform.emergency_recovery.time.sleep"):
            emergency_recovery._stop(plan, self.root)
        with patch("engineering_platform.emergency_recovery.os.kill", side_effect=ProcessLookupError), patch(
            "engineering_platform.emergency_recovery._process_command"
        ):
            with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "stopte al"):
                emergency_recovery._stop(plan, self.root)

    def test_emergency_preview_never_exposes_invalid_or_unplannable_run(self) -> None:
        self.assertEqual(emergency_recovery.preview(self.root, 9), {"available": False})
        with patch("engineering_platform.emergency_recovery._plan", side_effect=emergency_recovery.EmergencyRecoveryError("unsafe")):
            self.assertEqual(emergency_recovery.preview(self.root, "inbox-abcdef"), {"available": False})

    def test_emergency_execute_records_terminal_recovery_only_after_clean_postcondition(self) -> None:
        run_id, head = "inbox-abcdef", "a" * 40
        plan = emergency_recovery.RecoveryPlan(run_id, "codex/fix", "main", head, None, 4)
        state = TransactionState(run_id=run_id, repository="repo", prompt_path="prompt.md", phase="EXECUTE_AGENT")
        store = Mock(); store.load.return_value = state
        connection = Mock()
        with patch("engineering_platform.emergency_recovery._plan", return_value=plan), patch(
            "engineering_platform.emergency_recovery._stop"
        ), patch("engineering_platform.emergency_recovery._release_lease"), patch(
            "engineering_platform.emergency_recovery._git", side_effect=["", "", "", "", head, ""]
        ), patch("engineering_platform.emergency_recovery.StateStore", return_value=store), patch(
            "engineering_platform.emergency_recovery.open_storage", return_value=connection
        ), patch("engineering_platform.emergency_recovery.write_runner_process"), patch(
            "engineering_platform.emergency_recovery.complete_active_phase"
        ), patch("engineering_platform.emergency_recovery.record_prompt_execution"), patch(
            "engineering_platform.emergency_recovery.record_execution_dismissal"
        ), patch("engineering_platform.emergency_recovery.record_emergency_recovery"), patch(
            "engineering_platform.emergency_recovery.store_projection"
        ):
            outcome = emergency_recovery.execute(self.root, run_id)
        self.assertTrue(outcome["rolled_back"])
        self.assertEqual(outcome["removed_branch"], "codex/fix")
        store.save.assert_called_once()

    def test_keychain_adapter_has_no_plaintext_fallback_and_redacts_failures(self) -> None:
        store = local_api_keychain.MacOSKeychainCredentialStore()
        completed = SimpleNamespace(returncode=0, stdout="secret\n")
        with patch("engineering_platform.local_api_keychain.subprocess.run", return_value=completed) as run:
            store.put_credential("consumer", "project", "secret")
            self.assertEqual(store.get_credential("consumer", "project"), "secret")
            store.delete_credential("consumer", "project")
        self.assertIn("consumer:project", run.call_args_list[0].args[0])
        with patch("engineering_platform.local_api_keychain.subprocess.run", side_effect=OSError):
            self.assertFalse(store.credential_present("consumer", "project"))
        with patch("engineering_platform.local_api_keychain.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="sensitive")):
            with self.assertRaisesRegex(local_api_keychain.KeychainError, "could not store"):
                store.put_credential("consumer", "project", "secret")

    def test_relay_render_and_uninstall_touch_only_the_canonical_launch_agent(self) -> None:
        relay_home = self.root.parent / "relay-home"
        binary = self.root / "runtime" / "engineering-dashboard-relay"
        with patch("engineering_platform.server_relay.Path.home", return_value=relay_home):
            plist = server_relay.render_launch_agent(binary)
            self.assertIn("dashboard", plist.read_text(encoding="utf-8"))
            with patch("engineering_platform.server_relay.LaunchdProvider") as launchd:
                result = server_relay.uninstall()
        self.assertEqual(result["component"], "dashboard_relay")
        launchd.return_value.uninstall.assert_called_once_with(plist)
        self.assertFalse(plist.exists())

    def test_dependabot_discovery_accepts_only_verified_bot_metadata(self) -> None:
        provider = Mock()
        provider.github.return_value = json.dumps([
            {"number": 2, "title": " update ", "html_url": "https://github.com/org/repo/pull/2", "user": {"login": "dependabot[bot]"}, "head": {"ref": "deps", "sha": "a" * 40}},
            {"number": 1, "title": "bad", "html_url": "https://github.com/org/repo/pull/1", "user": {"login": "person"}, "head": {"ref": "x", "sha": "a" * 40}},
            {"number": 3, "title": "wrong url", "html_url": "https://invalid", "user": {"login": "app/dependabot"}, "head": {"ref": "x", "sha": "a" * 40}},
        ])
        discovered = dependabot_producer.discover_open_pull_requests("org/repo", provider)
        self.assertEqual([item.number for item in discovered], [2])
        with self.assertRaisesRegex(dependabot_producer.DependabotProducerError, "INVALID_SOURCE_METADATA"):
            dependabot_producer._validate_pull_request("org/repo", object())

    def test_pr_check_repair_evidence_admission_and_state_are_fail_closed(self) -> None:
        sha = "a" * 40
        self.assertEqual(pr_check_repair.failed_check_names({}), [])
        self.assertEqual(pr_check_repair.check_summary([{ "name": "pending", "status": "IN_PROGRESS" }]), ([], False))
        pr_check_repair._write_state(self.root, 4, sha, {"status": "QUEUED", "commit_sha": "b" * 40})
        self.assertTrue(pr_check_repair.attempted(self.root, 4, sha))
        self.assertEqual(pr_check_repair.repair_state(self.root, 4, "b" * 40), "QUEUED")
        with patch("engineering_platform.pr_check_repair.current_evidence", return_value={"eligible": False}):
            with self.assertRaisesRegex(pr_check_repair.PullRequestCheckRepairError, "not_eligible"):
                pr_check_repair.admit(self.root, 4)
        evidence = {"eligible": True, "head_sha": sha, "branch": "feature/repair", "failed_checks": ["tests"]}
        with patch("engineering_platform.pr_check_repair.current_evidence", return_value=evidence), patch(
            "engineering_platform.pr_check_repair.provider_readiness_failures", return_value=["github"]
        ):
            with self.assertRaisesRegex(pr_check_repair.PullRequestCheckRepairError, "provider_not_ready"):
                pr_check_repair.admit(self.root, 4)
        with patch("engineering_platform.pr_check_repair.current_evidence", return_value=evidence), patch(
            "engineering_platform.pr_check_repair.provider_readiness_failures", return_value=[]
        ), patch("engineering_platform.pr_check_repair.read_remaining_percent", return_value=75), patch(
            "engineering_platform.pr_check_repair.capacity_reserve_from_environment", return_value=5
        ):
            admitted = pr_check_repair.admit(self.root, 5)
        self.assertEqual(admitted["head_sha"], sha)
        pr_check_repair.mark_dispatch_failed(self.root, 5, sha)
        self.assertEqual(pr_check_repair.repair_state(self.root, 5, sha), "FAILED")

    def test_pr_check_repair_run_records_stale_and_agent_failures_without_push(self) -> None:
        sha = "c" * 40
        pr_check_repair._write_state(self.root, 7, sha, {"status": "QUEUED", "branch": "feature/repair", "failed_checks": ["tests"]})
        with patch("engineering_platform.pr_check_repair.current_evidence", return_value={"head_sha": "d" * 40, "branch": "feature/repair", "checks_terminal": True, "failed_checks": ["tests"]}), patch(
            "engineering_platform.pr_check_repair._command"
        ) as command:
            with self.assertRaisesRegex(pr_check_repair.PullRequestCheckRepairError, "stale"):
                pr_check_repair.run(self.root, 7, sha)
        command.assert_not_called()
        self.assertEqual(pr_check_repair.repair_state(self.root, 7, sha), "QUEUED")

    def test_pr_check_repair_reads_only_same_repository_terminal_github_evidence(self) -> None:
        sha = "e" * 40
        payload = {"number": 9, "state": "OPEN", "isDraft": False, "headRefOid": sha, "headRefName": "fix/check", "headRepository": {"nameWithOwner": "org/repo"}, "statusCheckRollup": [{"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}]}
        with patch("engineering_platform.pr_check_repair._repository", return_value="org/repo"), patch(
            "engineering_platform.pr_check_repair.GitHubProvider"
        ) as github:
            github.return_value.github.return_value = json.dumps(payload)
            evidence = pr_check_repair.current_evidence(self.root, 9)
        self.assertTrue(evidence["eligible"]); self.assertEqual(evidence["failed_checks"], ["tests"])
        payload["headRepository"] = {"nameWithOwner": "fork/repo"}
        with patch("engineering_platform.pr_check_repair._repository", return_value="org/repo"), patch(
            "engineering_platform.pr_check_repair.GitHubProvider"
        ) as github:
            github.return_value.github.return_value = json.dumps(payload)
            self.assertFalse(pr_check_repair.current_evidence(self.root, 9)["eligible"])

    def test_pr_check_repair_cli_dispatches_the_explicit_root_and_head(self) -> None:
        with patch("engineering_platform.pr_check_repair.run") as run, patch("sys.argv", ["repair", "--root", str(self.root), "--pull-request", "11", "--head-sha", "f" * 40]):
            self.assertEqual(pr_check_repair.main(), 0)
        run.assert_called_once_with(self.root.resolve(), 11, "f" * 40)

    def test_dependabot_qualification_fixture_is_explicit_and_discovery_fails_closed(self) -> None:
        fixture = self.root / "dependabot-fixture.json"
        fixture.write_text(json.dumps({"org/repo": []}), encoding="utf-8")
        with patch.dict("os.environ", {dependabot_producer.QUALIFICATION_FIXTURE_ENVIRONMENT: str(fixture)}, clear=False):
            with self.assertRaisesRegex(dependabot_producer.DependabotProducerError, "FORBIDDEN"):
                dependabot_producer._qualification_provider()
        with patch.dict("os.environ", {
            dependabot_producer.QUALIFICATION_FIXTURE_ENVIRONMENT: str(fixture),
            "EP_QUALIFICATION_INITIALIZE_ONLY": "1",
        }, clear=False):
            provider = dependabot_producer._qualification_provider()
        self.assertEqual(json.loads(provider.github("api", "repos/org/repo/pulls?state=open&per_page=100")), [])
        with self.assertRaises(dependabot_producer.DependabotProducerError):
            dependabot_producer.discover_open_pull_requests("org/repo", SimpleNamespace(github=lambda *_args: "{}"))
        with self.assertRaises(dependabot_producer.DependabotProducerError):
            dependabot_producer.discover_open_pull_requests("org/repo", SimpleNamespace(github=lambda *_args: (_ for _ in ()).throw(RuntimeError("offline"))))

    def test_dependabot_service_writes_only_observational_heartbeat_and_degrades_safely(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        service = dependabot_producer.DependabotService(self.root, provider=Mock(), interval_seconds=0, event=lambda name, context: events.append((name, context)))
        service._last_discovery = "now"; service._last_submission = "submission-a"
        service._write_heartbeat(ready=True)
        self.assertEqual(dependabot_producer.read_heartbeat(self.root)["state"], "READY")
        service._stop = Mock(); service._stop.is_set.side_effect = [False, True]
        service._stop.wait = Mock()
        with patch.object(service, "tick", side_effect=dependabot_producer.DependabotProducerError("offline")):
            service._run()
        self.assertEqual(dependabot_producer.read_heartbeat(self.root)["state"], "DEGRADED")
        self.assertEqual(events[-1][0], "dependabot_discovery_degraded")
        thread = Mock(); service._thread = thread
        service.stop()
        thread.join.assert_called_once()
        with patch("engineering_platform.dependabot_producer.threading.Thread", return_value=thread):
            service.start()
        thread.start.assert_called_once()

    def test_server_topology_rejects_repository_identity_drift(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "repository_attachment" / "python-authority.json"
        declaration = json.loads(fixture.read_text(encoding="utf-8"))
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            first = project_topology.register_server_local_topology(connection, declaration=declaration)
            self.assertEqual(first["result"], "REGISTERED")
            conflict = json.loads(json.dumps(declaration))
            conflict["project"]["id"] = "different-project"
            with self.assertRaisesRegex(TopologyRegistrationError, "REPOSITORY_IDENTITY_CONFLICT"):
                project_topology.register_server_local_topology(connection, declaration=conflict)

    def test_local_api_contract_rejects_non_json_unknown_and_write_methods(self) -> None:
        from engineering_platform.local_api import LocalApiHandler, _error
        handler = object.__new__(LocalApiHandler)
        handler.wfile = io.BytesIO(); handler.send_response = lambda *args: None; handler.send_header = lambda *args: None; handler.end_headers = lambda: None
        # The handler itself advertises no write surface; every write verb is
        # a contract-level 405 rather than an accidental local authority.
        responses = []
        handler.send_response = lambda status: responses.append(status)
        for method in (handler.do_PUT, handler.do_DELETE, handler.do_PATCH, handler.do_HEAD, handler.do_OPTIONS):
            method()
        self.assertEqual(responses, [405] * 5)

    def test_local_api_cli_doctor_and_launch_agent_are_loopback_bounded(self) -> None:
        from engineering_platform import local_api
        home = self.root.parent / "api-home"; output = io.StringIO()
        with patch("engineering_platform.local_api.Path.home", return_value=home), patch(
            "engineering_platform.local_api.readiness", return_value=True
        ), redirect_stdout(output):
            self.assertEqual(local_api.main(["doctor", "--repo", str(self.root), "--port", "8766"]), 0)
            self.assertEqual(local_api.main(["doctor", "--repo", str(self.root), "--port", "1"]), 1)
        self.assertEqual(json.loads(output.getvalue().splitlines()[0])["bind"], "127.0.0.1")
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        with patch("engineering_platform.local_api.Path.home", return_value=home):
            plist = local_api.launch_agent(self.root, 8766)
        self.assertIn("--port</string><string>8766", plist.read_text(encoding="utf-8"))

    def test_controlled_provider_interruption_is_run_bound_single_use_and_redacted(self) -> None:
        root = self.root.parent / "recovery-control"
        state = SimpleNamespace(terminal=False, phase="EXECUTE_AGENT")
        with patch("engineering_platform.provider_recovery.StateStore") as states:
            states.return_value.load.return_value = state
            armed = provider_recovery.arm_controlled_interruption(root, run_id="run-a", phase="QUALITY_CONTROL_AGENT", armed_by="user\nsecret", reason="reason\nsecret")
        self.assertEqual(armed["state"], "ARMED")
        self.assertEqual(provider_recovery.controlled_interruption_status(root, run_id="run-a", phase="QUALITY_CONTROL_AGENT"), "ARMED")
        self.assertEqual(provider_recovery.disarm_controlled_interruption(root, run_id="run-a", phase="QUALITY_CONTROL_AGENT"), "DISARMED")
        self.assertEqual(provider_recovery.disarm_controlled_interruption(root, run_id="run-a", phase="QUALITY_CONTROL_AGENT"), "NOT_ARMED")
        with patch("engineering_platform.provider_recovery.StateStore") as states:
            states.return_value.load.return_value = SimpleNamespace(terminal=True, phase="EXECUTE_AGENT")
            with self.assertRaisesRegex(provider_recovery.ControlledInterruptionControlError, "terminal"):
                provider_recovery.arm_controlled_interruption(root, run_id="run-b", phase="QUALITY_CONTROL_AGENT")

    def test_recovery_reconciliation_fails_closed_for_absent_or_ambiguous_receipts(self) -> None:
        with patch("engineering_platform.provider_recovery.load_recovery_state", return_value=None):
            self.assertEqual(provider_recovery.reconcile_recovery(self.root, run_id="run-a"), "NOT_APPLICABLE")
        recovery = {"state": "RECOVERY_STARTING", "replacement_invocation_id": "invoke-a"}
        with patch("engineering_platform.provider_recovery.load_recovery_state", return_value=recovery), patch(
            "engineering_platform.provider_recovery._receipts", return_value=[{"launch_state": "CLAIMED", "outcome": None}, {"launch_state": "CLAIMED", "outcome": None}]
        ), patch("engineering_platform.provider_recovery.mark_ambiguous") as marked:
            self.assertEqual(provider_recovery.reconcile_recovery(self.root, run_id="run-a"), "AMBIGUOUS")
        marked.assert_called_once()

    def test_recovery_reconciliation_accepts_only_matching_session_bound_process(self) -> None:
        recovery = {"state": "RECOVERY_IN_PROGRESS", "replacement_invocation_id": "invoke-a", "provider_session_id": "session-a"}
        receipt = {"launch_state": "PROCESS_STARTED", "outcome": None, "provider_session_id": "session-a", "process_pid": 7, "process_group": 7, "process_start_fingerprint": "birth", "process_executable_identity": "codex"}
        with patch("engineering_platform.provider_recovery.load_recovery_state", return_value=recovery), patch(
            "engineering_platform.provider_recovery._receipts", return_value=[receipt]
        ):
            self.assertEqual(provider_recovery.reconcile_recovery(self.root, run_id="run-a", verifier=lambda identity: "MATCH"), "SAME_PROVIDER_STILL_ACTIVE")

    def test_provider_recovery_reconciliation_exhausts_or_recovers_only_terminal_receipts(self) -> None:
        base = {"state": "RECOVERY_IN_PROGRESS", "replacement_invocation_id": "invoke-a", "provider_session_id": "session-a"}
        start = {"launch_state": "PROCESS_STARTED", "provider_session_id": "session-a", "process_pid": 4, "process_group": 4, "process_start_fingerprint": "birth", "process_executable_identity": "codex"}
        for outcome, evidence, expected in (("SUCCESS", "artifact:result", "RECOVERED"), ("INTERRUPTED", None, "EXHAUSTED")):
            terminal = {"launch_state": "TERMINAL", "outcome": outcome, "result_evidence_ref": evidence}
            with patch("engineering_platform.provider_recovery.load_recovery_state", return_value=base), patch(
                "engineering_platform.provider_recovery._receipts", return_value=[start, terminal]
            ), patch("engineering_platform.provider_recovery.transition_recovery_state") as transition:
                self.assertEqual(provider_recovery.reconcile_recovery(self.root, run_id="run-a"), expected)
            self.assertEqual(transition.call_args.kwargs["target"], expected)
        with patch("engineering_platform.provider_recovery.load_recovery_state", return_value={"state": "RECOVERY_STARTING", "replacement_invocation_id": "invoke-a", "diagnostic_code": "LAUNCH_NOT_STARTED:offline"}), patch(
            "engineering_platform.provider_recovery._receipts", return_value=[{"launch_state": "CLAIMED", "outcome": None}]
        ):
            self.assertEqual(provider_recovery.reconcile_recovery(self.root, run_id="run-a"), "LAUNCH_CLAIMED_PREEXEC_FAILURE")

    def test_provider_recovery_watcher_and_control_cli_never_create_unowned_work(self) -> None:
        with patch("engineering_platform.provider_recovery.load_recovery_state", return_value={"state": "RECOVERY_AVAILABLE"}), patch(
            "engineering_platform.provider_recovery.lease_liveness", return_value={"state": "LIVE"}
        ):
            self.assertIsNone(provider_recovery.watcher_resume_action(self.root, "run-a"))
        for state, expected in (("RECOVERY_AVAILABLE", "RESUME_AVAILABLE"), ("RECOVERY_STARTING", "RECONCILE_STARTING"), ("EXHAUSTED", None)):
            with patch("engineering_platform.provider_recovery.load_recovery_state", return_value={"state": state}), patch(
                "engineering_platform.provider_recovery.lease_liveness", return_value={"state": "STALE"}
            ):
                self.assertEqual(provider_recovery.watcher_resume_action(self.root, "run-a"), expected)
        output = io.StringIO()
        with patch("engineering_platform.provider_recovery.arm_controlled_interruption", return_value={"run_id": "run-a"}), patch(
            "engineering_platform.provider_recovery.controlled_interruption_status", return_value="ARMED"), patch(
            "engineering_platform.provider_recovery.disarm_controlled_interruption", return_value="DISARMED"
        ), redirect_stdout(output):
            self.assertEqual(provider_recovery.main(["arm-controlled-interruption", "--repo", str(self.root), "--run-id", "run-a", "--phase", "QUALITY_CONTROL_AGENT"]), 0)
            self.assertEqual(provider_recovery.main(["controlled-interruption-status", "--repo", str(self.root), "--run-id", "run-a", "--phase", "QUALITY_CONTROL_AGENT"]), 0)
            self.assertEqual(provider_recovery.main(["disarm-controlled-interruption", "--repo", str(self.root), "--run-id", "run-a", "--phase", "QUALITY_CONTROL_AGENT"]), 0)
        self.assertIn("DISARMED", output.getvalue())

    def test_controlled_interruption_hook_consumes_once_and_records_artifact(self) -> None:
        with patch.dict("os.environ", {"DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE": "run-a:QUALITY_CONTROL_AGENT"}, clear=False), patch(
            "engineering_platform.provider_recovery.load_recovery_state", return_value=None
        ), patch("engineering_platform.provider_recovery.record_artifact") as recorded:
            self.assertTrue(provider_recovery.consume_controlled_interruption_hook(self.root, run_id="run-a", phase="QUALITY_CONTROL_AGENT"))
        self.assertTrue(recorded.called)
        self.assertFalse(provider_recovery.consume_controlled_interruption_hook(self.root, run_id="run-a", phase="QUALITY_CONTROL_AGENT"))

    def test_provider_recovery_transition_is_compare_and_swap_and_rejects_unknown_states(self) -> None:
        with self.assertRaises(ValueError):
            provider_recovery.transition_recovery_state(self.root, run_id="run-a", expected="UNKNOWN", target="RECOVERED")
        connection = Mock(); connection.execute.return_value.rowcount = 1
        with patch("engineering_platform.provider_recovery._connection", return_value=connection):
            self.assertTrue(provider_recovery.transition_recovery_state(self.root, run_id="run-a", expected="RECOVERY_AVAILABLE", target="PRECHECK_FAILED", diagnostic_code="checked"))
        connection.close.assert_called_once()

    def test_prompt_history_path_and_retry_filters_are_bounded(self) -> None:
        reports = self.root / ".engineering" / "reports"; reports.mkdir(parents=True)
        report = reports / "run-a.md"; report.write_text("# report", encoding="utf-8")
        self.assertEqual(prompt_history._relative_report(self.root, report), "run-a.md")
        self.assertIsNone(prompt_history._relative_report(self.root, self.root / "outside.md"))
        children = [{"retry_of": "parent-a", "run_id": "run-a", "status": "QUEUED"}, {"retry_of": "parent-a", "run_id": "bad\n", "status": "ACTIVE"}, {"retry_of": "parent-a", "run_id": "run-b", "status": "INVALID"}]
        self.assertEqual(prompt_history._valid_retry_children(children), [{"retry_of": "parent-a", "run_id": "run-a", "status": "QUEUED", "retry_timestamp": None}])

    def test_server_provider_actions_are_host_scoped_and_verify_their_postconditions(self) -> None:
        """Interactive provider actions reject unsuitable hosts and unsafe sessions."""
        completed = SimpleNamespace(returncode=0, stdout="octocat\n", stderr="")
        with self.assertRaisesRegex(ValueError, "Unsupported provider"):
            server._start_provider_login(self.root, "OTHER")
        codex = Mock(); codex.return_value._executable = "/opt/ep/bin/codex"; codex.return_value.status.return_value = SimpleNamespace(qualified=True)
        with patch("engineering_platform.server.CodexCliProvider", codex), patch.object(server.sys, "platform", "darwin"), patch(
            "engineering_platform.server.LocalProcessProvider"
        ) as process:
            process.return_value.execute.return_value = completed
            server._start_provider_login(self.root, "CODEX")
        self.assertEqual(process.return_value.execute.call_args.args[1][0], "/usr/bin/osascript")
        with patch.object(server.sys, "platform", "linux"), patch("engineering_platform.server.CodexCliProvider", codex):
            with self.assertRaisesRegex(ValueError, "macOS"):
                server._start_provider_login(self.root, "CODEX")
        with patch("engineering_platform.server.CodexCliProvider", codex):
            codex.return_value.command.return_value = completed
            server._logout_provider(self.root, "CODEX")
            codex.return_value.command.return_value = SimpleNamespace(returncode=1)
            with self.assertRaisesRegex(ValueError, "did not complete"):
                server._logout_provider(self.root, "CODEX")
        with patch("engineering_platform.server.LocalProcessProvider") as process:
            process.return_value.execute.side_effect = [completed, completed]
            server._logout_provider(self.root, "GITHUB")
            process.return_value.execute.side_effect = [SimpleNamespace(returncode=0, stdout="bad user!", stderr="")]
            with self.assertRaisesRegex(ValueError, "safely identified"):
                server._logout_provider(self.root, "GITHUB")
        with patch("engineering_platform.server._central_execution_active", return_value=False), patch(
            "engineering_platform.server.shutil.which", return_value="/opt/homebrew/bin/brew"
        ), patch("engineering_platform.server.LocalProcessProvider") as process, patch(
            "engineering_platform.server._central_provider_readiness", return_value={"github": {"state": "READY"}}
        ):
            process.return_value.execute.return_value = completed
            server._install_provider(self.root, "GITHUB")
        with self.assertRaisesRegex(ValueError, "Unsupported provider"):
            server._install_provider(self.root, "OTHER")

    def test_console_handler_covers_supported_mutation_denials_and_project_lifecycle(self) -> None:
        """Console mutations are explicit CENTRAL actions with bounded denial states."""
        projects = [{"project_id": "project-a", "repository_id": "repo-a"}]
        with patch("engineering_platform.server._console_projects", return_value=projects), patch(
            "engineering_platform.server._provider_capacity_projection", return_value={"rate_limits": {"windows": [{"used_percent": 10}]}}
        ), patch("engineering_platform.server.central_database.update_capacity_configuration", return_value={"codex_capacity_reserve_percent": 5}), patch(
            "engineering_platform.server.central_database.update_console_interval_configuration", return_value={"key": "log_level"}
        ), patch("engineering_platform.server._clear_central_console_component_logs", return_value={"deleted": 1}), patch(
            "engineering_platform.server._execution_runtime_status", return_value={"state": "NOT_READY"}
        ):
            cases = (
                ("/api/logs/operations_console", b'{"component":"operations_console"}', 200),
                ("/api/provider-capacity/configuration", b'{"codex_capacity_reserve_percent":5}', 200),
                ("/api/configuration", b'{"key":"log_level","value":"DEBUG","previous":"INFO"}', 200),
                ("/api/execution-runtime/repair", b"{}", 409),
                ("/api/components/operations_console/restart", b"{}", 409),
                ("/api/configuration", b"{}", 409),
            )
            for path, body, expected in cases:
                handler, responses = self._in_process_console_handler(path, body=body, headers={"Content-Length": str(len(body))})
                handler._central_database_configuration = lambda _method: False
                handler._delegate_dashboard("do_POST")
                self.assertEqual(responses[-1][0], expected, path)
            for path, body, expected in (
                ("/api/execution-dismiss", b'{"run_id":"run-a"}', 403),
                ("/api/execution-dismiss", b"{}", 400),
                ("/api/configuration", b"{}", 409),
            ):
                headers = {"Content-Length": str(len(body)), "X-Engineering-Platform-Project": "project-a"}
                if path == "/api/execution-dismiss" and body != b"{}":
                    headers["Origin"] = "https://invalid"
                handler, responses = self._in_process_console_handler(path, body=body, headers=headers)
                handler._central_database_configuration = lambda _method: False
                handler._delegate_dashboard("do_POST")
                self.assertEqual(responses[-1][0], expected, path)

    def test_http_dispatch_exposes_only_authenticated_transport_and_health_contracts(self) -> None:
        """The public handler keeps diagnostics, health and ingress failure modes distinct."""
        handler, responses = self._in_process_console_handler("/diagnostics/topology")
        with patch("engineering_platform.server.operations_projection", return_value={"scope": "PLATFORM"}), patch(
            "engineering_platform.server.initialize", return_value=SimpleNamespace(instance_id="instance-a")
        ):
            handler.do_GET()
        self.assertEqual(responses[-1], (200, {"scope": "PLATFORM"}))
        unavailable, denied = self._in_process_console_handler("/v1/operations/projects")
        with patch("engineering_platform.server.operations_projection", side_effect=server.ServerConfigurationError("missing")):
            unavailable.do_GET()
        self.assertEqual(denied[-1][0], 503)
        health, health_responses = self._in_process_console_handler("/readyz")
        health._status = lambda: {"instance_id": "instance-a", "store": "ready"}
        health.do_GET()
        self.assertEqual(health_responses[-1][0], 200)
        ingress, ingress_responses = self._in_process_console_handler(
            "/v1/projects/project-a/submissions", body=b"{}", headers={"Content-Length": "2", "Content-Type": "application/json"}
        )
        with patch("engineering_platform.server._authenticated_consumer", return_value=None):
            ingress.do_POST()
        self.assertEqual(ingress_responses[-1], (401, {"error": "UNAUTHENTICATED"}))

    def test_server_cli_routes_registration_and_local_binding_through_explicit_contracts(self) -> None:
        """Administrative CLI commands require complete identities and preserve CENTRAL routing."""
        root = self.root.parent / "cli-coverage"
        output = io.StringIO()
        binding = SimpleNamespace(binding_id="binding-a", project_id="project-a", repository_id="repo-a", version=1)
        local = SimpleNamespace(project_id="project-a", repository_id="repo-a", state="BOUND")
        with redirect_stdout(output), patch(
            "engineering_platform.server.external_producer_binding.register", return_value=binding
        ), patch("engineering_platform.server.external_producer_binding.list_bindings", return_value=[{"binding_id": "binding-a"}]), patch(
            "engineering_platform.server.external_producer_binding.deactivate", return_value=binding
        ), patch("engineering_platform.server.local_repository_binding.bind_local_repository", return_value=local), patch(
            "engineering_platform.server.local_repository_binding.resolve_execution_repository", return_value=local), patch(
            "engineering_platform.server.local_repository_binding.unbind_local_repository"
        ), patch(
            "engineering_platform.submission_service.issue_consumer_credential", return_value={"consumer_id": "consumer-a"}
        ):
            self.assertEqual(server.main(["register-producer-binding", "--data-root", str(root), "--producer-type", "GITHUB", "--external-resource-type", "PULL_REQUEST", "--external-resource-identity", "pr-1", "--project-id", "project-a", "--repository-id", "repo-a", "--reason", "approved"]), 0)
            self.assertEqual(server.main(["list-producer-bindings", "--data-root", str(root)]), 0)
            self.assertEqual(server.main(["deactivate-producer-binding", "--data-root", str(root), "--binding-id", "binding-a", "--reason", "retired"]), 0)
            self.assertEqual(server.main(["issue-consumer-credential", "--data-root", str(root), "--project-id", "project-a", "--consumer-id", "consumer-a"]), 0)
            self.assertEqual(server.main(["bind-repository", "--data-root", str(root), "--project-id", "project-a", "--repository-id", "repo-a", "--path", str(self.root)]), 0)
            self.assertEqual(server.main(["resolve-repository", "--data-root", str(root), "--project-id", "project-a", "--repository-id", "repo-a"]), 0)
            self.assertEqual(server.main(["unbind-repository", "--data-root", str(root), "--project-id", "project-a", "--repository-id", "repo-a"]), 0)
            self.assertEqual(server.main(["submission-diagnose", "--data-root", str(root)]), 2)
            self.assertEqual(server.main(["register-producer-binding", "--data-root", str(root)]), 2)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0]["result"], "REGISTERED")
        self.assertEqual(records[1], {"bindings": [{"binding_id": "binding-a"}]})
        self.assertEqual(records[-1]["ready"], False)

    def test_central_console_projection_shapes_are_bounded_and_project_isolated(self) -> None:
        """Read-model shaping consumes CENTRAL rows only and preserves project identity."""
        connection = Mock()
        connection.execute.side_effect = [
            Mock(fetchall=Mock(return_value=[("run-b", "COMPLETE", "created", "updated", "CLI")])),
            Mock(fetchall=Mock(return_value=[("run-b", "COMPLETE")])),
        ]
        database = Mock(); database.__enter__ = Mock(return_value=connection); database.__exit__ = Mock(return_value=False)
        with patch("engineering_platform.server._console_queue_projection", return_value={"queue_depth": 1, "queue_items": [], "operator_handling": {}}), patch(
            "engineering_platform.server._console_platform_version", return_value="2.0"
        ), patch("engineering_platform.server._central_console_telemetry", return_value=[{"date": "2026-09-05"}]), patch(
            "engineering_platform.server.sqlite3.connect", return_value=database
        ):
            snapshot = server._central_console_project_snapshot(self.root, "project-a")
        self.assertEqual(snapshot["project_id"], "project-a")
        self.assertEqual(snapshot["status"]["active_run"], None)
        self.assertEqual(snapshot["runs"][0]["execution_mode"], "CLI")
        telemetry_connection = Mock()
        telemetry_connection.execute.return_value.fetchall.return_value = [
            ("run-b", "2026-09-05T12:00:00Z", "COMPLETE", 4.2, 0.8, "codex", "model", "high", "HTTP", "repo-a"),
        ]
        telemetry_database = Mock(); telemetry_database.__enter__ = Mock(return_value=telemetry_connection); telemetry_database.__exit__ = Mock(return_value=False)
        with patch("engineering_platform.server.sqlite3.connect", return_value=telemetry_database):
            detail = server._central_console_telemetry_detail(self.root, "project-a", "2026-09-05")
        self.assertEqual(detail["summary"]["completed"], 1)
        self.assertEqual(detail["runs"][0]["project_id"] if "project_id" in detail["runs"][0] else "project-a", "project-a")
        with patch("engineering_platform.server.sqlite3.connect", return_value=telemetry_database):
            self.assertIsNone(server._central_console_telemetry_detail(self.root, "project-a", "not-a-date"))

    def test_console_event_and_report_helpers_reject_unowned_or_unavailable_central_artifacts(self) -> None:
        """Central report/chat helpers cannot be tricked into reading a checkout artifact."""
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = ("checkout:report.md",)
        database = Mock(); database.__enter__ = Mock(return_value=connection); database.__exit__ = Mock(return_value=False)
        with patch("engineering_platform.server.sqlite3.connect", return_value=database):
            self.assertIsNone(server._central_console_report(self.root, "project-a", "run-a"))
        connection.execute.side_effect = [Mock(fetchone=Mock(return_value=(1,))), Mock(fetchall=Mock(return_value=[("user", "hello", "model", "at")]))]
        with patch("engineering_platform.server.sqlite3.connect", return_value=database):
            self.assertEqual(server._central_console_chat_history(self.root, "project-a", "run-a"), [{"role": "user", "content": "hello", "model": "model", "created_at": "at"}])
        connection.execute.side_effect = [Mock(fetchone=Mock(return_value=None))]
        with patch("engineering_platform.server.sqlite3.connect", return_value=database):
            self.assertIsNone(server._central_console_chat_history(self.root, "project-a", "run-a"))

    def test_submission_diagnosis_and_declaration_provisioning_use_only_registered_central_records(self) -> None:
        """Operator CLI evidence is tied to one registered project and CENTRAL submission."""
        root = self.root.parent / "cli-diagnosis"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(server.main(["bootstrap-topology", "--data-root", str(root), "--project-id", "project-a", "--repository-id", "repo-a"]), 0)
        with sqlite3.connect(root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)", ("run-a", "project-a", "RUNNING", "now", "now"))
            connection.execute("INSERT INTO ep_submissions(submission_id,project_id,repository_id,producer_id,producer_type,transport,prompt,prompt_digest,constraints,state,admission,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("sub-a", "project-a", "repo-a", "test", "TEST", "HTTP", "p", "digest", "{}", "QUEUED", "ADMITTED", "now"))
            connection.execute("INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("sub-a", "project-a", "repo-a", "run-a", "RUNNING", "CENTRAL:prompt", "now", "now"))
        evidence = root / "artifacts" / "projects" / "project-a" / "runs" / "run-a"
        evidence.mkdir(parents=True)
        (evidence / "early-runner-failure.json").write_text("not-json", encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(server.main(["submission-diagnose", "--data-root", str(root), "--submission-id", "sub-a"]), 0)
            self.assertEqual(server.main(["submission-diagnose", "--data-root", str(root), "--submission-id", "unknown"]), 2)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0]["worker_eligible"], True)
        self.assertEqual(records[0]["transport_provenance"], "COMPLETE")
        self.assertEqual(records[0]["execution_receipt_provenance"], "UNAVAILABLE")
        with sqlite3.connect(root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("INSERT INTO execution_receipts(run_id,producer_id,producer_type,execution_host,execution_host_version,receipt_timestamp,execution_outcome) VALUES(?,?,?,?,?,?,?)", ("run-a", "test", "TEST", "Engineering Platform", "2.0", "now", "COMPLETE"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(server.main(["submission-diagnose", "--data-root", str(root), "--submission-id", "sub-a"]), 0)
        self.assertEqual(json.loads(output.getvalue())["execution_receipt_provenance"], "PRESENT")
        self.assertEqual(records[0]["early_failure"], {"diagnostic_code": "EARLY_FAILURE_EVIDENCE_UNAVAILABLE"})
        with sqlite3.connect(root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute("INSERT INTO ep_submissions(submission_id,project_id,repository_id,producer_id,producer_type,transport,prompt,prompt_digest,constraints,state,admission,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("sub-incomplete", "project-a", "repo-a", "test", "TEST", "FILE_INBOX", "p", "digest", "{}", "QUEUED", "ADMITTED", "now"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(server.main(["submission-diagnose", "--data-root", str(root), "--submission-id", "sub-incomplete"]), 0)
        self.assertEqual(json.loads(output.getvalue())["transport_provenance"], "INCOMPLETE")
        checkout = root.parent / "declared-checkout"; checkout.mkdir()
        provision = io.StringIO()
        with redirect_stdout(provision):
            self.assertEqual(server.main(["provision-declaration", "--data-root", str(root), "--project-id", "project-a", "--repository-id", "repo-a", "--path", str(checkout)]), 0)
        self.assertEqual(json.loads(provision.getvalue())["result"], "PROVISIONED")

    def test_console_streams_emit_central_payloads_then_stop_on_disconnect(self) -> None:
        """SSE streams emit Server projections and treat a lost browser as non-mutating."""
        def stream(method: str, *args: object) -> bytes:
            handler = object.__new__(server._HealthHandler)
            handler.server = SimpleNamespace(data_root=self.root)
            handler.wfile = io.BytesIO()
            handler.send_response = lambda _code: None
            handler.send_header = lambda _name, _value: None
            handler.end_headers = lambda: None
            with patch("engineering_platform.server.central_database.console_interval_configuration", return_value={"dashboard_stream_interval_seconds": 1}), patch(
                "engineering_platform.server.time.sleep", side_effect=BrokenPipeError
            ), patch("engineering_platform.server._central_console_project_snapshot", return_value={"scope": "PROJECT"}):
                getattr(handler, method)(*args)
            return handler.wfile.getvalue()
        self.assertIn(b"event: dashboard", stream("_stream_no_project_console_events"))
        self.assertIn(b'"scope":"PROJECT"', stream("_stream_project_console_events", "project-a"))
        self.assertIn(b'"scope":"PROJECT"', stream("_stream_console_events", self.root, "project-a"))

    def test_no_project_routes_and_provider_service_reject_cross_scope_requests(self) -> None:
        """The unselected Console exposes platform facts but cannot acquire a project context."""
        for path, expected in (
            ("/api/process-metrics", 200), ("/api/usage", 200), ("/api/logs/not-a-component", 409),
        ):
            handler, responses = self._in_process_console_handler(path)
            handler._no_project_platform_route("do_GET", server.urlsplit(path))
            if responses:
                self.assertEqual(responses[-1][0], expected, path)
        with patch("engineering_platform.server._central_provider_readiness", return_value={"codex": {"state": "AUTH_REQUIRED"}, "github": {"state": "READY"}}), patch(
            "engineering_platform.server._start_provider_login"
        ) as login, patch("engineering_platform.server._logout_provider") as logout:
            server._central_provider_repair(self.root, {"provider": "CODEX", "action": "login"})
            server._central_provider_logout(self.root, {"provider": "GITHUB"})
        login.assert_called_once_with(self.root, "CODEX")
        logout.assert_called_once_with(self.root, "GITHUB")
        with self.assertRaisesRegex(ValueError, "Invalid provider"):
            server._central_provider_repair(self.root, {"provider": "CODEX", "action": "delete"})

    def test_small_installation_helpers_fail_closed_without_root_fallbacks(self) -> None:
        """Package resources, validation commands and tooling remain installation-scoped."""
        self.assertTrue(resources.package_text("ENGINEERING_PLATFORM_VERSION.json"))
        self.assertTrue(resources.package_path("ENGINEERING_PLATFORM_VERSION.json").is_file())
        with self.assertRaises(resources.PackageResourceError):
            resources.package_text("missing-resource.json")
        self.assertEqual(validation_identity.canonical_validation_launcher("/bin/zsh -lc 'npm run test:engineering-dashboard'"), "npm run test:engineering-dashboard")
        self.assertTrue(validation_identity.is_canonical_dashboard_command("npm run test:engineering-dashboard -- --headed"))
        self.assertFalse(validation_identity.is_canonical_dashboard_command("npm run test:engineering-dashboard && rm -rf x"))
        ledger = investigation_ledger.InvocationInvestigationLedger().record("repository_identity", "test_surface")
        self.assertTrue(ledger.reusable("test_surface"))
        self.assertEqual(ledger.invalidate("validation").completed, frozenset({"repository_identity"}))
        with self.assertRaises(ValueError):
            ledger.invalidate("unknown")
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            worktree_tooling.prepare(worktree)
            (worktree / "package-lock.json").write_text("{}")
            (worktree / "playwright.config.mjs").write_text("export default {}")
            (worktree / "node_modules" / "@playwright" / "test").mkdir(parents=True)
            with patch("engineering_platform.worktree_tooling.subprocess.run", return_value=SimpleNamespace(returncode=0)):
                worktree_tooling.prepare(worktree)
            with patch("engineering_platform.worktree_tooling.subprocess.run", return_value=SimpleNamespace(returncode=1)):
                with self.assertRaisesRegex(worktree_tooling.WorktreeToolingError, "unavailable"):
                    worktree_tooling.prepare(worktree)

    def test_process_identity_readiness_and_historical_migration_are_bounded(self) -> None:
        """Host evidence is PID-safe, provider output is redacted, and inbox migration is archival only."""
        completed = SimpleNamespace(returncode=0, stdout="Mon Jan  1 00:00:00 2026 /usr/bin/python3\n", stderr="")
        with patch("engineering_platform.provider_process_identity.os.getpgid", return_value=7), patch(
            "engineering_platform.provider_process_identity.subprocess.run", return_value=completed
        ):
            identity = provider_process_identity.capture_process_identity(12, 7)
        self.assertIsNotNone(identity)
        with patch("engineering_platform.provider_process_identity.capture_process_identity", return_value=None):
            self.assertEqual(provider_process_identity.verify_process_identity(identity), "NOT_ACTIVE")
        self.assertEqual(provider_readiness._classify(SimpleNamespace(returncode=1, stdout="not logged in", stderr="")), "AUTH_REQUIRED")
        self.assertEqual(provider_readiness._repository_classify(SimpleNamespace(returncode=1, stdout="network timeout", stderr="")), "CHECK_FAILED")
        self.assertEqual(provider_readiness._version(SimpleNamespace(returncode=0, stdout="gh version 2.3.4", stderr="")), "2.3.4")
        with tempfile.TemporaryDirectory() as directory:
            repo, legacy = Path(directory) / "repo", Path(directory) / "legacy"; repo.mkdir(); legacy.mkdir()
            source = legacy / "Completed"; source.mkdir(); (source / "result.md").write_text("done")
            (legacy / "status.json").write_text("{}")
            result = legacy_inbox_migration.migrate_icloud_archives(repo, legacy)
            self.assertEqual(result, {"moved": 2, "deleted_duplicates": 0})
            self.assertTrue((repo / ".engineering" / "inbox" / "Completed" / "result.md").is_file())

    def test_provider_adapters_verify_lifecycle_and_tailnet_state_before_reporting_success(self) -> None:
        """Local provider adapters expose only verified host diagnostics/actions."""
        launchd = providers.LaunchdProvider()
        failed = SimpleNamespace(returncode=1, stdout="", stderr="offline")
        active = SimpleNamespace(returncode=0, stdout="active count = 1", stderr="")
        with patch("engineering_platform.providers.shutil.which", return_value=None):
            self.assertFalse(launchd.inspect("label"))
            with self.assertRaisesRegex(OSError, "unavailable"):
                launchd.restart("label")
        with patch("engineering_platform.providers.shutil.which", return_value="launchctl"), patch(
            "engineering_platform.providers.subprocess.run", return_value=failed
        ):
            self.assertFalse(launchd.runtime_status("label").qualified)
            with self.assertRaisesRegex(OSError, "offline"):
                launchd.restart("label")
        with patch("engineering_platform.providers.shutil.which", return_value="launchctl"), patch(
            "engineering_platform.providers.subprocess.run", return_value=active
        ):
            self.assertTrue(launchd.runtime_status("label").qualified)
        tailscale = providers.TailscaleProvider()
        with patch("engineering_platform.providers.shutil.which", return_value="tailscale"), patch(
            "engineering_platform.providers.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="127.0.0.1\n100.101.2.3\n", stderr="")
        ):
            self.assertEqual(tailscale.ipv4_address(), "100.101.2.3")
        with patch("engineering_platform.providers.shutil.which", return_value=None):
            self.assertIsNone(tailscale.ipv4_address())

    def test_package_module_entrypoint_delegates_only_to_the_execution_host_cli(self) -> None:
        """The installed module command adds no alternate runtime or configuration path."""
        with patch("engineering_platform.execution_host.main", return_value=0) as main:
            with self.assertRaises(SystemExit) as exit_code:
                runpy.run_module("engineering_platform.__main__", run_name="__main__")
        self.assertEqual(exit_code.exception.code, 0)
        main.assert_called_once_with()

    def test_transaction_state_rejects_unsafe_checkpoint_shapes_before_recovery_can_use_them(self) -> None:
        """Checkpoint deserialisation is fail-closed for identity and admission evidence."""
        raw = TransactionState("run-a", "owner/repo", "prompt.md", "COMPLETE", terminal=True).to_dict()
        for change in (
            {"schema_version": 0}, {"run_id": "../escape"}, {"owner_authorized": "yes"},
            {"admission_decision": "PASS", "admission_completed_at": None},
            {"provider_recovery_attempts": [{"bad": "shape"}]},
        ):
            with self.assertRaises(StateError):
                TransactionState.from_dict({**raw, **change})

    def test_central_state_store_never_emits_or_deletes_a_checkout_projection(self) -> None:
        database = self.root / server.SERVER_DATABASE_FILENAME
        shadows = self.root / "checkout" / ".engineering" / "engineering-runs"
        store = StateStore(shadows, central_database=database)
        state = TransactionState("run-a", "owner/repo", "prompt.md", "COMPLETE", terminal=True)
        self.assertEqual(store.save(state), shadows / "run-a.json")
        self.assertFalse((shadows / "run-a.json").exists())
        self.assertEqual(store.load("run-a").run_id, "run-a")
        store.remove("run-a")
        with self.assertRaises(StateError): store.load("run-a")
