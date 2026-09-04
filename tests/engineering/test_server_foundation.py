from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engineering_platform import file_inbox, local_repository_binding, project_topology, providers, server
from engineering_platform.platform_components import PLATFORM_COMPONENT_IDS


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
        self.assertEqual(report["schema_version"], server.SERVER_STORE_SCHEMA_VERSION)
        self.assertEqual(report["operational_state"], "empty-valid")
        self.assertFalse(report["running"])
        self.assertFalse((self.root / ".engineering").exists())

    def test_server_import_does_not_load_retired_inbox_watcher_runtime(self) -> None:
        """The Server Console must not resurrect the retired watcher by import."""
        repository = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(repository / "src")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import engineering_platform.server; "
                    "assert 'engineering_platform.inbox_watcher' not in sys.modules"
                ),
            ],
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_component_projection_is_exactly_the_canonical_model(self) -> None:
        """Cards, details and logs cannot gain an independent component identity."""
        server.initialize(self.root)
        projection = server.status(self.root)
        model = projection["component_model"]
        self.assertEqual({item["id"] for item in model}, PLATFORM_COMPONENT_IDS)
        self.assertEqual(set(projection["components"]), PLATFORM_COMPONENT_IDS)
        self.assertTrue(all(item["id"] == item["log_component"] for item in model))
        self.assertTrue(all({"name_key", "kind", "group", "restart_supported"} <= item.keys() for item in model))

    def test_console_document_has_one_central_log_table_and_no_legacy_inbox_configuration(self) -> None:
        """Historical dashboard markup cannot re-enable retired Console controls."""
        server.initialize(self.root)
        document = server._no_project_console_document([], self.root)
        component_logs = re.search(
            br'<details class="technical-details" id="componentLogs">(.*?)</details>', document, re.DOTALL,
        )
        self.assertIsNotNone(component_logs)
        self.assertEqual(component_logs.group(1).count(b'<table class="log-table"'), 1)  # type: ignore[union-attr]
        self.assertIn(b'platformComponentLog', component_logs.group(1))  # type: ignore[union-attr]
        self.assertNotIn(b'dashboardComponentLog', document)
        self.assertNotIn(b'configurationInboxOpen', document)
        self.assertNotIn(b'configurationInboxModal', document)

    def test_server_console_uses_canonical_log_markup_without_a_runtime_transform(self) -> None:
        """The one log table is authored once, not replaced after rendering."""
        server.initialize(self.root)
        with patch("engineering_platform.server._centralize_component_log_surface") as compatibility:
            no_project = server._no_project_console_document([], self.root)
            selected = server._selected_project_console_document("project-a", [], self.root)
        compatibility.assert_not_called()
        for document in (no_project, selected):
            section = re.search(br'<details class="technical-details" id="componentLogs">(.*?)</details>', document, re.DOTALL)
            self.assertIsNotNone(section)
            self.assertEqual(section.group(1).count(b'<table class="log-table"'), 1)  # type: ignore[union-attr]

    def test_server_surfaces_missing_managed_runtime_without_degrading_central(self) -> None:
        identity = server.initialize(self.root)
        report = server.status(self.root)
        self.assertEqual(report["instance_id"], identity.instance_id)
        self.assertIn(report["managed_codex_runtime"]["state"], {"MISSING", "BROKEN", "READY"})
        self.assertEqual(server.operations_projection(self.root)["managed_codex_runtime"], report["managed_codex_runtime"])

    @patch("engineering_platform.server.managed_codex_runtime.inspect")
    def test_runtime_state_is_observational_and_never_changes_server_readiness(self, inspect: object) -> None:
        inspect.return_value = {"state": "MISSING", "path": "/isolated/codex", "remediation_available": True}
        server.initialize(self.root)
        report = server.status(self.root)
        self.assertEqual(report["managed_codex_runtime"]["state"], "MISSING")
        self.assertEqual(report["store"], "ready")

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
        with urlopen(f"http://127.0.0.1:{port}/readyz") as response:
            readiness = json.loads(response.read().decode("utf-8"))
        self.assertEqual(readiness["lifecycle_worker"]["state"], "RUNNING")
        stopped = server.stop(self.root)
        self.assertFalse(stopped["running"])

    def test_configuration_fails_closed(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "server.json").write_text(json.dumps({"version": 2}), encoding="utf-8")
        with self.assertRaises(server.ServerConfigurationError):
            server.initialize(self.root)

    def test_server_upgrades_home_derived_runtime_configuration_once(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "server.json").write_text(
            json.dumps({"version": 1, "bind_host": "127.0.0.1", "bind_port": 8765}), encoding="utf-8"
        )
        prefix = Path("/Users/canonical/.local/share/engineering-platform/codex-cli")
        with patch("engineering_platform.server.default_engineering_platform_codex_cli_prefix", return_value=prefix):
            server.initialize(self.root)
        configuration = json.loads((self.root / "server.json").read_text(encoding="utf-8"))
        self.assertEqual(configuration["version"], 2)
        self.assertEqual(configuration["managed_codex_cli_prefix"], str(prefix))

    def test_managed_cli_prefix_is_not_derived_from_a_worker_home(self) -> None:
        prefix = "/Users/canonical/.local/share/engineering-platform/codex-cli"
        with patch.dict(os.environ, {
            "HOME": "/private/var/folders/example/tmp/home",
            providers.MANAGED_CODEX_CLI_PREFIX_ENVIRONMENT: prefix,
        }, clear=False):
            self.assertEqual(providers.engineering_platform_codex_cli_prefix(), Path(prefix))

    def test_unauthenticated_foundation_refuses_a_non_loopback_bind(self) -> None:
        with self.assertRaises(server.ServerConfigurationError):
            server.initialize(self.root, bind_host="0.0.0.0")

    def test_agent_extension_point_remains_transport_neutral(self) -> None:
        request = server.AgentRegistrationRequest("future-agent", "project-agent", ("execute",))
        self.assertEqual(request.agent_id, "future-agent")
        self.assertFalse(hasattr(request, "credential"))

    def test_fresh_store_is_official_schema_47_with_empty_operational_state(self) -> None:
        identity = server.initialize(self.root)
        report = server.validate_store(self.root, identity)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            self.assertEqual(
                connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0],
                server.SERVER_STORE_SCHEMA_VERSION,
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
        with self.assertRaisesRegex(
            server.ServerConfigurationError, f"schema-{server.SERVER_STORE_SCHEMA_VERSION}"
        ):
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
        self.assertEqual(report["schema_version"], server.SERVER_STORE_SCHEMA_VERSION)

    def test_operations_console_projection_is_central_owned_and_empty_safe(self) -> None:
        identity = server.initialize(self.root)
        projection = server.operations_projection(self.root)
        self.assertEqual(projection["installation_id"], identity.instance_id)
        self.assertEqual(projection["schema_version"], server.SERVER_STORE_SCHEMA_VERSION)
        self.assertEqual(projection["projects"], [])
        self.assertIn(b"/v1/operations/projects", server._operations_console_document())

    def test_no_project_console_never_resolves_a_checkout_for_shell_assets_or_platform_data(self) -> None:
        """`<geen>` is a real CENTRAL/platform projection, not first-root fallback."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=port)
        server.start(self.root)

        for path in ("/", "/assets/dashboard.css", "/assets/dashboard.js", "/api/platform-status", "/api/configuration", "/api/logs/all"):
            with urlopen(f"http://127.0.0.1:{port}{path}") as response:
                self.assertEqual(response.status, 200, path)
                if path in {"/api/platform-status", "/api/logs/all"}:
                    projection = json.loads(response.read())
                    self.assertEqual(projection["scope"], "PLATFORM")
        with urlopen(f"http://127.0.0.1:{port}/") as response:
            document = response.read().decode("utf-8")
        self.assertIn('data-project-id="none"', document)
        self.assertIn('id="noProjectSelected"', document)
        self.assertIn('data-i18n="central.no_project_selected_title"', document)
        self.assertIn('data-i18n="central.no_project_selected_body"', document)
        self.assertNotIn('Geen project gekozen', document)

    def test_central_log_route_filters_sorts_and_paginates_before_responding(self) -> None:
        """The Console must receive a filtered CENTRAL page, never a sampled log tail."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=port)
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            for event, level, diagnostic, created_at in (
                ("zeta", "INFO", "unrelated", "2026-02-01T10:00:00+00:00"),
                ("alpha", "WARNING", "needle one", "2026-02-02T10:00:00+00:00"),
                ("beta", "ERROR", "needle two", "2026-02-03T10:00:00+00:00"),
                ("gamma", "ERROR", "outside range", "2026-02-04T10:00:00+00:00"),
            ):
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                    ("operations_console", json.dumps({"event": event, "level": level, "diagnostic": diagnostic, "run_id": event}), created_at),
                )
        server.start(self.root)
        query = (
            "format=json&page=1&page_size=1&start=2026-02-02T00%3A00%3A00%2B00%3A00"
            "&end=2026-02-04T00%3A00%3A00%2B00%3A00&level=WARNING&search=needle"
            "&sort=event&direction=asc"
        )
        with urlopen(f"http://127.0.0.1:{port}/api/logs/operations_console?{query}") as response:
            filtered = json.loads(response.read())
        self.assertEqual(filtered["total"], 2)
        self.assertEqual(filtered["page"], 1)
        self.assertEqual(filtered["page_size"], 1)
        self.assertEqual([entry["event"] for entry in filtered["entries"]], ["alpha"])
        self.assertEqual(filtered["entries"][0]["component"], "operations_console")
        self.assertEqual(set(filtered["events"]), {"alpha", "beta"})
        with urlopen(f"http://127.0.0.1:{port}/api/logs/operations_console?{query.replace('page=1', 'page=2', 1)}") as response:
            second_page = json.loads(response.read())
        self.assertEqual([entry["event"] for entry in second_page["entries"]], ["beta"])
        with urlopen(f"http://127.0.0.1:{port}/api/logs/operations_console?format=ndjson&search=needle") as response:
            exported = [json.loads(line) for line in response.read().decode().splitlines()]
        self.assertEqual({entry["event"] for entry in exported}, {"alpha", "beta"})
        for invalid in ("page=0", "page_size=201", "level=TRACE", "sort=component", "direction=sideways"):
            with self.assertRaises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{port}/api/logs/operations_console?{invalid}")
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(json.loads(error.exception.read())["error"], "LOG_QUERY_INVALID")

    @patch("engineering_platform.server.dashboard._start_provider_login")
    @patch("engineering_platform.server._central_provider_readiness")
    def test_provider_login_repair_is_central_and_keeps_no_project_readiness(
        self, readiness: object, start_login: object,
    ) -> None:
        """A host-wide sign-in cannot fall through to a checkout route."""
        readiness.return_value = {
            "codex": {"state": "AUTH_REQUIRED", "executable": "/managed/codex", "version": "1", "scope": "PLATFORM"},
            "github": {"state": "AUTH_REQUIRED", "executable": "/managed/gh", "version": "1", "scope": "PLATFORM"},
        }
        server._central_provider_repair(self.root, {"provider": "CODEX", "action": "login"})
        start_login.assert_called_once_with(self.root, "CODEX")
        self.assertEqual(readiness()["codex"]["state"], "AUTH_REQUIRED")

    @patch("engineering_platform.server.dashboard._logout_provider")
    @patch("engineering_platform.server._central_provider_readiness")
    def test_provider_logout_is_central_and_never_requires_a_project(
        self, readiness: object, logout: object,
    ) -> None:
        """A host-wide sign-out must work from the installed ``<geen>`` Console."""
        readiness.return_value = {
            "codex": {"state": "READY", "scope": "PLATFORM"},
            "github": {"state": "READY", "scope": "PLATFORM"},
        }
        server._central_provider_logout(self.root, {"provider": "GITHUB"})
        logout.assert_called_once_with(self.root, "GITHUB")
        with self.assertRaises(ValueError):
            server._central_provider_logout(self.root, {"provider": "INVALID"})

    @patch("engineering_platform.server.provider_readiness.runtime_details")
    @patch("engineering_platform.server.provider_readiness.host_status")
    def test_central_provider_readiness_uses_host_authentication_not_repository_access(
        self, host_status: object, runtime_details: object,
    ) -> None:
        host_status.return_value = {
            "codex": {"provider": "CODEX", "state": "READY"},
            "github": {"provider": "GITHUB", "state": "READY"},
        }
        runtime_details.return_value = {
            "codex": {"executable": "/managed/codex", "version": "1"},
            "github": {"executable": "/managed/gh", "version": "2"},
        }

        readiness = server._central_provider_readiness(self.root)

        self.assertEqual(readiness["codex"], {
            "provider": "CODEX", "state": "READY", "executable": "/managed/codex", "version": "1", "scope": "PLATFORM",
        })
        self.assertEqual(readiness["github"]["state"], "READY")
        host_status.assert_called_once_with(self.root)
        runtime_details.assert_called_once_with(self.root)

    def test_transport_components_are_platform_scoped_and_secret_free(self) -> None:
        server.initialize(self.root)
        components = server.status(self.root)["components"]
        self.assertEqual(set(components), {"ep_server", "platform_database", "lifecycle_worker", "operations_console", "dashboard_relay", "http_ingress", "cli_ingress", "file_inbox_ingress"})
        self.assertEqual(components["ep_server"]["status_code"], "EP_SERVER_UNAVAILABLE")
        self.assertEqual(components["platform_database"]["status_code"], "PLATFORM_DATABASE_HEALTHY")
        self.assertEqual(components["http_ingress"]["status_code"], "HTTP_INGRESS_DOWN")
        self.assertEqual(components["cli_ingress"]["status_code"], "CLI_INGRESS_DEGRADED")
        self.assertEqual(components["file_inbox_ingress"]["status_code"], "FILE_INGRESS_STOPPED")
        self.assertNotIn("credential", repr(components).lower())

    def test_live_file_inbox_with_quarantine_is_degraded_without_execution_state(self) -> None:
        server.initialize(self.root)
        inbox = self.root / server.FILE_INBOX_DIRECTORY
        inbox.mkdir(parents=True)
        (inbox / file_inbox.HEARTBEAT_FILENAME).write_text(json.dumps({
            "state": "READY", "readiness": "SUBMISSION_CAPABLE",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "watched_location": str(inbox), "delivery_retry": "NONE",
            "quarantine_count": 1, "recent_error": "MALFORMED_FILE",
        }), encoding="utf-8")
        with patch("engineering_platform.server._runtime", return_value={"pid": 1}), patch(
            "engineering_platform.server._alive", return_value=True,
        ):
            component = server.status(self.root)["components"]["file_inbox_ingress"]
        self.assertEqual(component["status_code"], "FILE_INGRESS_DEGRADED")
        self.assertEqual(component["detail_code"], "FILE_INBOX_HEARTBEAT")
        self.assertEqual(component["quarantine_count"], 1)
        self.assertEqual(component["reason_code"], "FILE_INBOX_DIAGNOSTIC")

    def test_console_workspace_identity_is_overridden_by_the_selected_central_project(self) -> None:
        historical = (
            b'<details id="workspaceCard"><span class="label" data-workspace-label="workspace.name" '
            b'data-i18n="workspace.name"></span><span>djconnect</span></details>'
        )
        projected = server._centralize_workspace_identity(historical, "alpha")
        self.assertIn(b'<span>alpha</span>', projected)
        self.assertNotIn(b'djconnect', projected)

    def test_central_database_controls_read_and_back_up_only_the_server_store(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=port)
        server.start(self.root)
        with urlopen(f"http://127.0.0.1:{port}/api/central-database/download") as response:
            backup = response.read()
            self.assertEqual(response.headers.get_content_type(), "application/vnd.sqlite3")
        with tempfile.NamedTemporaryFile(suffix=".db") as file:
            file.write(backup); file.flush()
            with sqlite3.connect(file.name) as connection:
                self.assertEqual(connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()[0], server.SERVER_STORE_SCHEMA_VERSION)
        request = Request(
            f"http://127.0.0.1:{port}/api/central-database/configuration",
            data=b'{"interval_seconds":86400}', method="POST", headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            self.assertEqual(json.loads(response.read()), {"previous": 3600, "interval_seconds": 86400})
        self.assertEqual(server.central_database.maintenance_configuration(self.root), {"interval_seconds": 86400})

    def test_every_console_setting_persists_through_the_central_configuration_api(self) -> None:
        """Every visible Console setting has one CENTRAL-owned save path."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
        server.initialize(self.root, bind_port=port)
        server.start(self.root)
        expected = {
            "log_retention_days": 180,
            "telemetry_retention_days": 180,
            "log_level": "DEBUG",
            "inbox_scan_interval_seconds": 15,
            "open_pr_check_interval_seconds": 60,
            "dashboard_stream_interval_seconds": 5,
            "provider_readiness_refresh_seconds": 600,
            "platform_health_refresh_seconds": 15,
            "component_details_refresh_seconds": 60,
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                request = Request(
                    f"http://127.0.0.1:{port}/api/configuration",
                    data=json.dumps({"key": key, "value": value, "previous": None}).encode(),
                    method="POST", headers={"Content-Type": "application/json"},
                )
                with urlopen(request) as response:
                    result = json.loads(response.read())
                self.assertEqual(result["key"], key)
                self.assertEqual(result["value"], value)
        with urlopen(f"http://127.0.0.1:{port}/api/configuration") as response:
            persisted = json.loads(response.read())
        self.assertEqual({key: persisted[key] for key in expected}, expected)

    def test_download_attachment_headers_reject_request_control_characters(self) -> None:
        self.assertEqual(
            server._report_content_disposition("dj-run-01"),
            'attachment; filename="engineering-report-dj-run-01.md"',
        )
        self.assertEqual(
            server._attachment_content_disposition("engineering-platform-central-20260903T000000Z.db"),
            'attachment; filename="engineering-platform-central-20260903T000000Z.db"',
        )
        for report_id in (
            "dj-run\rX-Injected: yes",
            "dj-run\nX-Injected: yes",
            "dj-run\r\nX-Injected: yes",
            "dj-run%0dX-Injected%3A%20yes",
            "dj-run%0aX-Injected%3A%20yes",
            "dj-run%0d%0aX-Injected%3A%20yes",
            "../dj-run",
        ):
            with self.subTest(report_id=report_id), self.assertRaises(ValueError):
                server._report_content_disposition(report_id)
        for filename in ("report\r.md", "report\n.md", "report\r\nX: yes.md", "report name.md", "report%0d.md"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                server._attachment_content_disposition(filename)

    def test_ep_database_panel_is_framed_and_maintenance_selection_recovers_safely(self) -> None:
        server.initialize(self.root)
        panel = server._central_database_section(self.root)
        script = server._central_database_script()

        self.assertIn('class="configuration-central-database"', panel)
        self.assertIn('data-i18n="configuration.ep_database"', panel)
        self.assertIn('EP-database', panel)
        self.assertNotIn('CENTRAL database', panel)
        self.assertIn('class="configuration-central-database__maintenance"', panel)
        self.assertIn('data-saved-value="3600"', panel)
        self.assertIn('id="centralDatabaseLocation"', panel)
        self.assertIn('configuration-central-database__location-link', panel)
        self.assertIn('configuration.ep_database_open_folder', panel)
        self.assertIn('aria-describedby="centralDatabaseMaintenanceHelp centralDatabaseMaintenanceStatus"', panel)
        self.assertIn("maintenance.dataset.savedValue", script)
        self.assertIn("maintenance.value=previous", script)
        self.assertIn("Number(result.interval_seconds)!==requested", script)

    @patch("engineering_platform.server.sys.platform", "darwin")
    @patch("engineering_platform.server.LocalProcessProvider")
    def test_ep_database_location_opens_only_the_central_owning_directory(self, process: MagicMock) -> None:
        server.initialize(self.root)
        process.return_value.execute.return_value = __import__("subprocess").CompletedProcess(("open",), 0, "", "")

        opened = server._open_central_database_directory(self.root)

        self.assertEqual(opened, {"opened_directory": str(self.root.resolve())})
        process.return_value.execute.assert_called_once_with(self.root.resolve(), ("open", str(self.root.resolve())))

    def test_runtime_directory_route_is_retired_in_the_central_console(self) -> None:
        identity = server.initialize(self.root)
        self.assertIsNotNone(identity.instance_id)
        self.assertFalse(hasattr(server, "_open_runtime_directory"))

    def test_root_reuses_historical_console_with_request_scoped_project_selection(self) -> None:
        """Two requests retain distinct CENTRAL identities and local bindings."""
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
        with urlopen(f"http://127.0.0.1:{port}/") as response:
            unscoped = response.read().decode("utf-8")
        # Empty project selection retains read-only, host-wide operations.
        # It must never turn into an implicit first-project projection.
        for path in (
            "/health",
            "/api/configuration",
            "/api/provider-login-status",
            "/api/execution-runtime-status",
            "/api/logs/all",
            "/api/logs/inbox",
            "/api/logs/dashboard",
        ):
            try:
                with urlopen(f"http://127.0.0.1:{port}{path}") as response:
                    self.assertEqual(response.status, 200, path)
            except HTTPError as error:
                # A minimal fresh host may correctly report itself unhealthy
                # (503), but an empty project selection must never reject a
                # host-wide endpoint as missing project scope.
                self.assertNotEqual(error.code, 409, path)
        with urlopen(f"http://127.0.0.1:{port}/api/dashboard-snapshot") as response:
            snapshot = json.loads(response.read())
        self.assertEqual(snapshot["scope"], "PLATFORM")
        self.assertRegex(snapshot["status"]["platform_version"], r"^\d+\.\d+\.\d+")
        self.assertEqual(snapshot["status"]["queue_depth"], 0)
        self.assertEqual(snapshot["runs"], [])
        with urlopen(f"http://127.0.0.1:{port}/api/events", timeout=2) as response:
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            self.assertEqual(response.headers["EP-Console-Route-Owner"], "PLATFORM")
            event = "".join(response.readline().decode("utf-8") for _ in range(4))
        self.assertIn('"scope":"PLATFORM"', event)
        self.assertNotIn('"runs":[{', event)
        for path in ("/api/prompt-history",):
            with self.assertRaises(HTTPError) as blocked:
                urlopen(f"http://127.0.0.1:{port}{path}")
            self.assertEqual(blocked.exception.code, 409, path)
        with self.assertRaises(HTTPError) as mutation:
            urlopen(Request(
                f"http://127.0.0.1:{port}/api/configuration",
                data=b'{"key":"log_level","value":"DEBUG"}', method="POST",
                headers={"Content-Type": "application/json"},
            ))
        self.assertEqual(mutation.exception.code, 409)
        with urlopen(f"http://127.0.0.1:{port}/?project=djconnect") as response:
            first = response.read().decode("utf-8")
        with urlopen(f"http://127.0.0.1:{port}/?project=engineering-platform") as response:
            second = response.read().decode("utf-8")
        # Platform runtime readiness must retain its CENTRAL source when the
        # browser selects a project and adds its request-scoped header.
        with urlopen(Request(
            f"http://127.0.0.1:{port}/api/execution-runtime-status",
            headers={"X-Engineering-Platform-Project": "djconnect"},
        )) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["EP-Console-Route-Owner"], "PLATFORM")
            self.assertIn(json.loads(response.read())["state"], {"READY", "UNAVAILABLE"})
        # The browser's two context modes must retain the same authority for
        # every Platform route.  Status payload presentation can differ, but
        # the health path must never become a project/check-out delegate.
        for path in (
            "/health", "/api/platform-status", "/api/dashboard-snapshot",
            "/api/status", "/api/provider-login-status",
            "/api/execution-runtime-status",
            "/api/components/ep_server/details", "/api/components/platform_database/details",
            "/api/components/lifecycle_worker/details", "/api/components/operations_console/details",
            "/api/components/dashboard_relay/details", "/api/components/file_inbox_ingress/details", "/api/configuration",
        ):
            for headers in ({}, {"X-Engineering-Platform-Project": "djconnect"}):
                try:
                    with urlopen(Request(f"http://127.0.0.1:{port}{path}", headers=headers)) as response:
                        self.assertEqual(response.headers["EP-Console-Route-Owner"], "PLATFORM", path)
                except HTTPError as error:
                    self.assertNotEqual(error.code, 409, path)
                    self.assertEqual(error.headers["EP-Console-Route-Owner"], "PLATFORM", path)
        # Retired routes are handled before project selection too, but retain
        # their own explicit historical owner rather than masquerading as a
        # supported platform component projection.
        for headers in ({}, {"X-Engineering-Platform-Project": "djconnect"}):
            with self.assertRaises(HTTPError) as retired:
                urlopen(Request(f"http://127.0.0.1:{port}/api/logs/inbox", headers=headers))
            self.assertEqual(retired.exception.code, 410)
            self.assertEqual(retired.exception.headers["EP-Console-Route-Owner"], "HISTORICAL_UNREACHABLE")
        # Actions are just as host-wide as their status cards. Invalid bodies
        # make these probes non-mutating while still exercising dispatch.
        for path, body in (
            ("/api/provider-login/repair", b"{}"),
            ("/api/execution-runtime/repair", b"{}"),
            ("/api/configuration", b"{}"),
        ):
            for headers in ({}, {"X-Engineering-Platform-Project": "djconnect"}):
                request_headers = {"Content-Type": "application/json", **headers}
                try:
                    with urlopen(Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST", headers=request_headers)) as response:
                        self.assertEqual(response.headers["EP-Console-Route-Owner"], "PLATFORM", path)
                except HTTPError as error:
                    self.assertNotEqual(error.code, 404, path)
                    self.assertEqual(error.headers["EP-Console-Route-Owner"], "PLATFORM", path)
        # EventSource supplies the CENTRAL scope as a query value because it
        # cannot set the dashboard fetch header. Both bound projects must
        # reach the preserved live route, not leak scope into a 404 path.
        for identifier in ("djconnect", "engineering-platform"):
            with urlopen(f"http://127.0.0.1:{port}/api/events?project={identifier}", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "text/event-stream")
                self.assertEqual(response.headers["EP-Console-Route-Owner"], "PLATFORM")
                event = "".join(response.readline().decode("utf-8") for _ in range(4))
                self.assertIn('"platform_version":"2.0.0"', event)
        # Browser module and stylesheet requests precede the document's
        # project-aware fetch wrapper, so neutral package assets must remain
        # available without a scope header.
        with urlopen(f"http://127.0.0.1:{port}/assets/dashboard.js") as response:
            asset = response.read()
        self.assertNotIn('id="consoleProject"', first)
        self.assertNotIn('id="consoleProjectBoundary"', first)
        self.assertIn("getElementById('dashboardProject')", first)
        self.assertIn("dashboard-select-options-changed", first)
        self.assertIn('const project = "djconnect"', first)
        self.assertIn('const project = "engineering-platform"', second)
        self.assertIn('data-project-id="djconnect" data-project-name="djconnect"', first)
        self.assertIn('data-project-id="engineering-platform" data-project-name="engineering-platform"', second)
        self.assertNotIn(str(roots[0]), first)
        self.assertNotIn("Project-scoped local workspace", first)
        selector = server._console_document_transform(
            "djconnect", [{"project_id": "djconnect", "repository_id": "djconnect"}], roots[0], self.root,
        )(b"<main></main>").decode("utf-8")
        no_project = server._no_project_console_document(
            [{"project_id": "djconnect", "repository_id": "djconnect"}],
            self.root,
        ).decode("utf-8")
        self.assertIn('&lt;geen&gt;</option>', selector)
        self.assertIn("dashboard-select-options-changed", selector)
        self.assertIn('>djconnect</option>', selector)
        self.assertNotIn('>DJConnect</option>', selector)
        self.assertIn('data-project-id="none" data-project-name="&lt;geen&gt;"', no_project)
        self.assertIn('id="noProjectSelected"', no_project)
        self.assertIn('data-i18n="central.no_project_selected_title"', no_project)
        self.assertIn('data-i18n="central.no_project_selected_body"', no_project)
        self.assertIn('data-i18n="project.label"', no_project)
        self.assertNotIn('Geen project gekozen', no_project)
        self.assertIn('dashboard-status-banner--no-project', no_project)
        self.assertLess(no_project.index('id="noProjectSelected"'), no_project.index('<main class="dashboard-grid"'))
        self.assertNotIn(str(roots[0]), no_project)
        self.assertIn('/assets/dashboard.js', no_project)
        self.assertIn('id="componentLogs"', no_project)
        self.assertIn('id="configuration"', no_project)
        self.assertNotIn('id="configurationInboxOpen"', no_project)
        self.assertNotIn('id="configurationInboxModal"', no_project)
        self.assertIn('id="configurationServerSettings"', no_project)
        self.assertIn('configurationInboxScanInterval', no_project)
        self.assertIn('configurationOpenPrInterval', no_project)
        self.assertLess(
            no_project.index('id="configurationServerSettings"'),
            no_project.index('configurationOpenPrInterval'),
        )
        self.assertIn('configuration-file-inbox-readonly', no_project)
        self.assertIn('id="centralDatabaseHeading"', no_project)
        self.assertIn('/api/central-database/download', no_project)
        self.assertNotIn('workspace-database-section', no_project)
        for hidden_project_section in ("#queueItems", "#promptHistory", "#currentRun", "#technicalDetails", "#workspaceCard"):
            self.assertIn(f'body[data-project-id="none"] {hidden_project_section}', no_project)
        self.assertIn('ENGINEERING_PLATFORM_NO_PROJECT=true', no_project)
        self.assertIn('id="noProjectSelected"', unscoped)
        self.assertNotIn('data-project-id="djconnect"', unscoped)
        self.assertNotIn(str(roots[0]), unscoped)
        self.assertEqual(server._historical_dashboard_path(server.urlsplit("/api/events?project=djconnect")), "/api/events")
        self.assertEqual(
            server._historical_dashboard_path(server.urlsplit("/api/prompt-history/run/report?project=djconnect&audit=download")),
            "/api/prompt-history/run/report?audit=download",
        )
        self.assertIn('/assets/dashboard.js', first)
        self.assertIn(b"fetch", asset)
        self.assertNotIn(str(roots[0]), first)
        self.assertNotIn(str(roots[1]), first)
        self.assertNotIn(str(roots[1]), second)
        self.assertNotIn(str(roots[0]), second)

        # Slice B is a CENTRAL read projection: history and queue remain
        # available after the physical checkout used by the legacy shell is
        # gone, and a selected project never receives another project's run.
        with sqlite3.connect(self.root / server.SERVER_DATABASE_FILENAME) as connection:
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("dj-run", "djconnect", "COMPLETE", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"),
            )
            connection.execute(
                "INSERT INTO ep_execution_runs(run_id,project_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("ep-run", "engineering-platform", "COMPLETE", "2026-01-02T00:00:00+00:00", "2026-01-02T00:01:00+00:00"),
            )
            connection.execute(
                """INSERT INTO ep_submissions(submission_id,project_id,repository_id,producer_id,producer_type,transport,prompt,prompt_digest,constraints,state,admission,created_at)
                   VALUES(?,?,?,?,?,'HTTP','telemetry','digest','{}','QUEUED','ADMITTED',?)""",
                ("dj-submission", "djconnect", "djconnect", "test", "HUMAN", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO ep_parity_lifecycle_dispatches(submission_id,project_id,repository_id,run_id,state,prompt_path,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("dj-submission", "djconnect", "djconnect", "dj-run", "COMPLETE", "CENTRAL:prompt", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"),
            )
            connection.execute(
                """INSERT INTO execution_runs(run_id,execution_date,arrived_at,execution_started_at,execution_finished_at,queue_wait_seconds,execution_seconds,terminal_state,input_tokens,output_tokens,total_tokens,execution_mode,workspace,repository,execution_host_version,total_execution_seconds)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("dj-run", "2026-01-01", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", "2026-01-01T00:00:04+00:00", 1.0, 3.0, "COMPLETE", 2, 3, 5, "MANAGED", "djconnect", "djconnect", "test", 4.0),
            )
            connection.execute(
                "INSERT INTO prompt_execution_history(run_id,terminal_state,prompt_title,executed_at,report_path,updated_at) VALUES(?,?,?,?,?,?)",
                ("dj-run", "COMPLETE", "CENTRAL report", "2026-01-01T00:00:04+00:00", "CENTRAL:reports/dj-run.md", "2026-01-01T00:00:04+00:00"),
            )
            connection.execute(
                "INSERT INTO execution_chat_messages(run_id,role,content,model,created_at) VALUES(?,?,?,?,?)",
                ("dj-run", "assistant", "CENTRAL transcript", "test-model", "2026-01-01T00:00:04+00:00"),
            )
            connection.execute(
                "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                ("operations_console", '{"event":"central_console_test","level":"INFO"}', "2026-01-01T00:00:04+00:00"),
            )
        artifact = self.root / "artifacts" / "reports" / "dj-run.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# CENTRAL report\n", encoding="utf-8")
        roots[0].rename(self.root.parent / "deleted-djconnect-checkout")
        with urlopen(f"http://127.0.0.1:{port}/api/dashboard-snapshot?project=djconnect") as response:
            snapshot = json.loads(response.read())
        with urlopen(f"http://127.0.0.1:{port}/api/prompt-history?project=djconnect") as response:
            history = json.loads(response.read())
        self.assertEqual(snapshot["status"]["project_id"], "djconnect")
        self.assertEqual([run["run_id"] for run in snapshot["runs"]], ["dj-run"])
        self.assertEqual([run["run_id"] for run in history], ["dj-run"])
        self.assertEqual(snapshot["telemetry"][0]["total_tokens"], 5)
        with urlopen(f"http://127.0.0.1:{port}/api/telemetry/2026-01-01?project=djconnect") as response:
            telemetry_detail = json.loads(response.read())
        self.assertEqual(telemetry_detail["runs"][0]["run_id"], "dj-run")
        with urlopen(f"http://127.0.0.1:{port}/api/prompt-history/dj-run/report?project=djconnect") as response:
            self.assertEqual(
                response.headers["Content-Disposition"],
                'attachment; filename="engineering-report-dj-run.md"',
            )
            self.assertEqual(response.read(), b"# CENTRAL report\n")
        with urlopen(f"http://127.0.0.1:{port}/api/prompt-history/dj-run/chat?project=djconnect") as response:
            self.assertEqual(json.loads(response.read())["messages"][0]["content"], "CENTRAL transcript")
        with urlopen(f"http://127.0.0.1:{port}/api/logs/all?project=djconnect&page_size=200") as response:
            logs = json.loads(response.read())
        self.assertEqual(logs["scope"], "PLATFORM")
        central_record = next(entry for entry in logs["entries"] if entry["event"] == "central_console_test")
        self.assertEqual(central_record["component"], "operations_console")
        self.assertIn("ep_server_started", [entry["event"] for entry in logs["entries"]])
        ndjson_request = Request(
            f"http://127.0.0.1:{port}/api/logs/operations_console?format=ndjson",
        )
        with urlopen(ndjson_request) as response:
            self.assertEqual(response.headers["Content-Type"], "application/x-ndjson; charset=utf-8")
            exported = [json.loads(line) for line in response.read().decode("utf-8").splitlines()]
        exported_record = next(entry for entry in exported if entry["event"] == "central_console_test")
        self.assertEqual(exported_record["component"], "operations_console")
        self.assertIn("timestamp", exported_record)
        delete_request = Request(
            f"http://127.0.0.1:{port}/api/logs/all",
            data=b'{"component":"operations_console"}', method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(delete_request) as response:
            deleted = json.loads(response.read())
        self.assertGreaterEqual(deleted["deleted"], 1)
        with urlopen(f"http://127.0.0.1:{port}/api/logs/operations_console") as response:
            remaining = json.loads(response.read())
        self.assertNotIn("central_console_test", [entry["event"] for entry in remaining["entries"]])
