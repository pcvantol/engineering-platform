from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from engineering_platform import local_repository_binding, project_topology, providers, server


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

        with patch("engineering_platform.server._console_root", side_effect=AssertionError("root must not be read")):
            for path in ("/", "/assets/dashboard.css", "/assets/dashboard.js", "/api/platform-status", "/api/configuration"):
                with urlopen(f"http://127.0.0.1:{port}{path}") as response:
                    self.assertEqual(response.status, 200, path)
                    if path == "/api/platform-status":
                        self.assertEqual(json.loads(response.read())["scope"], "PLATFORM")
        with urlopen(f"http://127.0.0.1:{port}/") as response:
            document = response.read().decode("utf-8")
        self.assertIn('data-project-id="none"', document)
        self.assertIn('id="noProjectSelected"', document)

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

    def test_runtime_directory_opens_only_the_parent_of_a_server_reported_executable(self) -> None:
        executable = self.root / "managed" / "bin" / "codex"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)
        with (
            patch("engineering_platform.server.sys.platform", "darwin"),
            patch("engineering_platform.server.LocalProcessProvider") as process,
            patch("engineering_platform.server._bound_console_projects", return_value=[{"project_id": "alpha"}]),
            patch("engineering_platform.server._console_root", return_value=self.root),
            patch("engineering_platform.server.dashboard._provider_login_status", return_value={"codex": {"executable": str(executable)}}),
        ):
            process.return_value.execute.return_value = __import__("subprocess").CompletedProcess(("open",), 0, "", "")
            opened = server._open_runtime_directory(self.root, "codex")

        self.assertEqual(opened, {"opened_directory": str(executable.parent.resolve())})
        process.return_value.execute.assert_called_once_with(
            executable.parent.resolve(), ("open", str(executable.parent.resolve())),
        )
        with self.assertRaises(ValueError):
            server._open_runtime_directory(self.root, "/untrusted/path")

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
        for path in ("/api/prompt-history", "/api/dashboard-snapshot", "/api/events"):
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
        # EventSource supplies the CENTRAL scope as a query value because it
        # cannot set the dashboard fetch header. Both bound projects must
        # reach the preserved live route, not leak scope into a 404 path.
        for identifier in ("djconnect", "engineering-platform"):
            with urlopen(f"http://127.0.0.1:{port}/api/events?project={identifier}", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "text/event-stream")
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
        self.assertIn(str(roots[0]), first)
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
        self.assertIn('dashboard-status-banner--no-project', no_project)
        self.assertLess(no_project.index('id="noProjectSelected"'), no_project.index('<main class="dashboard-grid"'))
        self.assertNotIn(str(roots[0]), no_project)
        self.assertIn('/assets/dashboard.js', no_project)
        self.assertIn('id="componentLogs"', no_project)
        self.assertIn('id="configuration"', no_project)
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
        self.assertIn(str(roots[0]), first)
        self.assertNotIn(str(roots[1]), first)
        self.assertIn(str(roots[1]), second)
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
            self.assertEqual(response.read(), b"# CENTRAL report\n")
        with urlopen(f"http://127.0.0.1:{port}/api/prompt-history/dj-run/chat?project=djconnect") as response:
            self.assertEqual(json.loads(response.read())["messages"][0]["content"], "CENTRAL transcript")
