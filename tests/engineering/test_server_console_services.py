from __future__ import annotations

import io
import json
import logging
import shutil
import sqlite3
from http.client import HTTPConnection
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from contextlib import ExitStack, contextmanager, nullcontext
from unittest.mock import ANY, MagicMock, call, patch

from engineering_platform import server_console_services as dashboard
from engineering_platform.server_console_services import DASHBOARD_VERSION, LOOPBACK_ADDRESS, _clear_component_log, _codex_cli_installation_path, _codex_process_metrics, _codex_provider_identity, _codex_usage, _codex_usage_for_run, _component_log, _component_log_versions, _completion_commits, _component_uptime_seconds, _current_codex_log, _dashboard_html, _execution_runtime_status, _last_executed_agent_execution, _last_executed_codex_log, _last_executed_commits, _last_executed_runtime_metadata, _latest_codex_log, _normalize_rate_limits, _open_worktree_in_finder, _platform_health, _prompt_history, _prompt_history_detail, _report_analysis_available_for_run, _report_analysis_for_run, _report_analysis_processing_status, _report_for_run, _retry_report_analysis, _reviewer_agents_for_run, _sse_snapshot, _sse_status, _status, _tracked_file_count, _workspace_free_disk_space, _workspace_git_projection, _workspace_worktrees
from engineering_platform.platform_version import EngineeringPlatformManifest
from engineering_platform.resources import package_path
from engineering_platform.prompt_history import record_prompt_execution
from engineering_platform.provider_usage import ProviderInvocation, persist_provider_invocation
from engineering_platform.storage import ENGINEERING_STORAGE_SCHEMA_VERSION, open_storage, store_projection
from engineering_platform.providers import ProviderStatus
from engineering_platform.historical_dashboard_configuration import inbox_root, update_inbox_root
from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.execution_lease import acquire


class DashboardStatusTest(unittest.TestCase):
    def test_rate_limit_capacity_ignores_malformed_windows_and_uses_the_lowest_remaining_value(self) -> None:
        self.assertIsNone(dashboard._remaining_rate_limit_capacity({"windows": "unknown"}))
        self.assertEqual(
            dashboard._remaining_rate_limit_capacity(
                {"windows": [None, {"used_percent": True}, {"used_percent": 120}, {"used_percent": 35}]}
            ),
            0.0,
        )

    def test_managed_codex_installation_path_never_accepts_another_executable(self) -> None:
        with patch("engineering_platform.server_console_services.engineering_platform_codex_cli_prefix", return_value=Path("/managed/codex")):
            self.assertIsNone(_codex_cli_installation_path(None))
            self.assertEqual(
                _codex_cli_installation_path("/managed/codex/bin/codex"), "/managed/codex"
            )
            self.assertIsNone(_codex_cli_installation_path("/usr/local/bin/codex"))

    @patch("engineering_platform.server_console_services.GitHubProvider")
    @patch("engineering_platform.server_console_services.GitProvider")
    def test_pull_request_metrics_are_bound_to_the_verified_repository(
        self, git_provider: object, github_provider: object
    ) -> None:
        git_provider.return_value.execute.return_value = __import__("subprocess").CompletedProcess(
            ("git",), 0, "git@github.com:pcvantol/djconnect.git\n", ""
        )
        github_provider.return_value.github.return_value = json.dumps(
            {
                "number": 981,
                "commits": [{"oid": "one"}, {"oid": "two"}],
                "changedFiles": 3,
                "statusCheckRollup": [
                    {"__typename": "CheckRun", "name": "tests"},
                    {"__typename": "StatusContext", "name": "legacy"},
                    {"__typename": "CheckRun", "name": ""},
                ],
            }
        )

        self.assertEqual(
            dashboard._pull_request_github_metrics(Path("/repository"), "pcvantol/djconnect", 981),
            {"commit_count": 2, "changed_file_count": 3, "check_count": 1},
        )

        github_provider.return_value.github.return_value = "not-json"
        self.assertEqual(
            dashboard._pull_request_github_metrics(Path("/repository"), "pcvantol/djconnect", 981),
            {},
        )
        github_provider.return_value.github.return_value = json.dumps({"number": 123})
        self.assertEqual(
            dashboard._pull_request_github_metrics(Path("/repository"), "pcvantol/djconnect", 981),
            {},
        )

    def test_terminal_diagnostic_rejects_an_invalid_run_identifier(self) -> None:
        self.assertIsNone(dashboard._terminal_run_diagnostic(Path("/repository"), "../outside"))

    @patch("engineering_platform.server_console_services.GitHubProvider")
    def test_github_rate_limit_status_handles_malformed_and_exhausted_responses(
        self, github_provider: object
    ) -> None:
        github_provider.return_value.github.return_value = json.dumps({"resources": "invalid"})
        self.assertEqual(dashboard._github_rate_limit_status(), {"limited": False})
        github_provider.return_value.github.return_value = json.dumps(
            {"resources": {"core": {"remaining": 0, "reset": 42}, "graphql": {"remaining": 2}}}
        )
        self.assertEqual(
            dashboard._github_rate_limit_status(),
            {"limited": True, "reset_at": 42},
        )
        github_provider.return_value.github.side_effect = RuntimeError("rate limit exceeded")
        self.assertEqual(dashboard._github_rate_limit_status(), {"limited": True})

    def test_canonical_checkpoint_rejects_an_invalid_run_identifier(self) -> None:
        self.assertEqual(dashboard._canonical_checkpoint(Path("/repository"), "../outside"), {})

    def test_browser_dashboard_validation_uses_bounded_ci_workers(self) -> None:
        config = (Path(__file__).parents[2] / "playwright.config.mjs").read_text(encoding="utf-8")

        self.assertIn("fullyParallel: true", config)
        self.assertIn("workers: process.env.CI ? 1 : undefined", config)
        self.assertIn("maxFailures: process.env.CI ? 3 : undefined", config)

    def test_workspace_card_shows_free_space_on_its_volume(self) -> None:
        with patch(
            "engineering_platform.server_console_services.shutil.disk_usage",
            return_value=MagicMock(free=12.34 * 1024**3),
        ):
            self.assertEqual(_workspace_free_disk_space(Path("/workspace")), "12.3 GB")

        page = _dashboard_html(
            "Engineering Status", workspace_free_disk_space="12.3 GB"
        ).decode("utf-8")
        self.assertIn('data-i18n="workspace.free_disk_space"', page)
        self.assertIn("12.3 GB", page)
        self.assertIn("Engineering Platform 2.0.0", page)

    def test_configuration_section_is_the_final_dashboard_block(self) -> None:
        page = _dashboard_html(
            "Engineering Status",
            configuration_inbox="/private/engineering/inbox",
        ).decode("utf-8")

        self.assertIn('id="configuration"', page)
        self.assertIn('data-i18n="section.configuration"', page)
        self.assertNotIn("/private/engineering/inbox", page)
        self.assertNotIn('id="configurationInboxModal"', page)
        self.assertNotIn('id="configurationInboxBrowse"', page)
        self.assertIn('id="configurationServerSettings"', page)
        self.assertIn('id="configurationLogRetention"', page)
        self.assertIn('id="configurationLogLevel"', page)
        self.assertIn('id="configurationTimeoutPolicyTitle"', page)
        self.assertNotIn('id="configurationAuditLogging"', page)
        self.assertNotIn('configuration.audit_logging', page)
        self.assertEqual(page.count('class="configuration-info"'), 5)
        self.assertEqual(page.count("data-i18n-title=\"configuration."), 5)
        for control in (
            "configurationInboxScanInterval", "configurationOpenPrInterval",
            "configurationDashboardStreamInterval",
            "configurationPlatformHealthInterval", "configurationComponentDetailsInterval",
        ):
            self.assertIn(f'id="{control}"', page)
        self.assertLess(
            page.index('id="configurationServerSettings"'),
            page.index('id="configurationOpenPrInterval"'),
        )
        for key, value in (
            ("configuration.inbox_scan_interval", "configuration.seconds_15"),
            ("configuration.operator_merge_interval", "configuration.seconds_60"),
            ("configuration.required_checks_interval", "configuration.seconds_15"),
            ("configuration.open_pr_interval", "configuration.seconds_30"),
            ("configuration.dashboard_stream_interval", None),
            ("configuration.platform_health_interval", "configuration.seconds_15"),
            ("configuration.component_details_interval", "configuration.seconds_5"),
            ("configuration.lease_heartbeat_interval", "configuration.seconds_15"),
            ("configuration.lease_timeout", "configuration.seconds_90"),
            ("configuration.github_retry_backoff", "configuration.github_retry_backoff_value"),
            ("configuration.timeout_policy", None),
            ("configuration.timeout.specialist_review", "configuration.minutes_5"),
            ("configuration.timeout.implementation", "configuration.minutes_15"),
            ("configuration.timeout.local_repository_validation", "configuration.minutes_15"),
            ("configuration.timeout.autonomous_quality_control", "configuration.minutes_10"),
            ("configuration.timeout.repair", "configuration.minutes_15"),
            ("configuration.timeout.finalization", "configuration.minutes_15"),
            ("configuration.timeout.end_reconciliation", "configuration.minutes_10"),
        ):
            self.assertIn(f'data-i18n="{key}"', page)
            if value is not None:
                self.assertIn(f'data-i18n="{value}"', page)
        self.assertNotIn('workspace-database-section', page)
        self.assertNotIn('configurationDatabaseMaintenanceInterval', page)
        self.assertNotIn('/api/engineering-database/', page)
        self.assertNotIn('id="dashboardLocale"', page[page.index('id="configuration"'):])
        self.assertNotIn('id="autoRefresh"', page[page.index('id="configuration"'):])
        self.assertLess(
            page.index('id="workspaceCard"'),
            page.index('id="configuration"'),
        )
        self.assertLess(page.index('id="configuration"'), page.index("</main>"))

    @patch("engineering_platform.server_console_services.provider_runtime_details")
    @patch("engineering_platform.server_console_services.provider_readiness_status")
    def test_provider_login_status_is_token_free_and_classifies_auth(
        self, readiness: MagicMock, runtime: MagicMock,
    ) -> None:
        readiness.return_value = {
            "codex": {"provider": "CODEX", "state": "READY"},
            "github": {"provider": "GITHUB", "state": "AUTH_REQUIRED"},
        }
        runtime.return_value = {
            "codex": {"executable": "/ep/codex", "version": "0.152.1"},
            "github": {"executable": "/opt/homebrew/bin/gh", "version": "2.82.1"},
        }

        status = dashboard._provider_login_status(Path("/workspace"))

        self.assertEqual(status, {
            "codex": {"provider": "CODEX", "state": "READY", "executable": "/ep/codex", "version": "0.152.1"},
            "github": {"provider": "GITHUB", "state": "AUTH_REQUIRED", "executable": "/opt/homebrew/bin/gh", "version": "2.82.1"},
        })
        readiness.assert_called_once_with(Path("/workspace"))
        runtime.assert_called_once_with(Path("/workspace"))

    def test_execution_runtime_status_is_token_free(self) -> None:
        status = _execution_runtime_status()

        self.assertEqual(status["state"], "READY")
        self.assertIn("executable", status)
        self.assertRegex(status["version"], r"^\d+\.\d+\.\d+")

    @patch("engineering_platform.server_console_services._provider_login_status", return_value={"codex": {"state": "AUTH_REQUIRED"}})
    @patch("engineering_platform.server_console_services.managed_codex_runtime.provision")
    @patch("engineering_platform.server_console_services._execution_active", return_value=False)
    def test_codex_install_uses_the_installation_owned_runtime_lifecycle(
        self, _active: MagicMock, provision: MagicMock, _status: MagicMock,
    ) -> None:
        dashboard._install_provider(Path("/workspace"), "CODEX")
        provision.assert_called_once_with(Path("/workspace"))

    @patch("engineering_platform.server_console_services.LocalProcessProvider")
    @patch("engineering_platform.server_console_services.CodexCliProvider")
    @patch("engineering_platform.server_console_services.shutil.which", return_value="/usr/local/bin/gh")
    @patch("engineering_platform.server_console_services.sys.platform", "darwin")
    def test_provider_login_allows_a_retry_and_the_other_provider(
        self, _which: MagicMock, codex: MagicMock, process: MagicMock,
    ) -> None:
        completed = __import__("subprocess").CompletedProcess
        codex.return_value.status.return_value.qualified = True
        codex.return_value._executable = "/usr/local/bin/codex"
        process.return_value.execute.return_value = completed(("osascript",), 0, "", "")
        dashboard._start_provider_login(Path("/workspace"), "CODEX")
        dashboard._start_provider_login(Path("/workspace"), "CODEX")
        dashboard._start_provider_login(Path("/workspace"), "GITHUB")
        self.assertEqual(process.return_value.execute.call_count, 3)
        scripts = [call.args[1][2] for call in process.return_value.execute.call_args_list]
        self.assertTrue(all('tell application "Terminal"' in script for script in scripts))
        self.assertTrue(all("activate" in script for script in scripts))
        self.assertIn("codex login --device-auth", scripts[0])
        self.assertIn("codex login --device-auth", scripts[1])
        self.assertIn("gh auth login --hostname github.com --web", scripts[2])

    @patch("engineering_platform.server_console_services.LocalProcessProvider")
    @patch("engineering_platform.server_console_services.CodexCliProvider")
    @patch("engineering_platform.server_console_services.sys.platform", "darwin")
    def test_provider_login_dispatch_failure_does_not_block_a_later_retry(
        self, codex: MagicMock, process: MagicMock,
    ) -> None:
        completed = __import__("subprocess").CompletedProcess
        codex.return_value.status.return_value.qualified = True
        codex.return_value._executable = "/usr/local/bin/codex"
        process.return_value.execute.side_effect = (
            completed(("osascript",), 1, "", "Terminal unavailable"),
            completed(("osascript",), 0, "", ""),
        )

        with self.assertRaisesRegex(ValueError, "window could not be opened"):
            dashboard._start_provider_login(Path("/workspace"), "CODEX")
        dashboard._start_provider_login(Path("/workspace"), "CODEX")

        self.assertEqual(process.return_value.execute.call_count, 2)

    @patch("engineering_platform.server_console_services.GitProvider")
    def test_workspace_git_projection_is_safe_and_sse_ready(self, git_provider: object) -> None:
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "codex/live-branch\n", ""),
            completed(("git",), 0, "123456789abcde\nabcdef12345678\n", ""),
        ]

        projection = _workspace_git_projection(Path("/workspace"))

        self.assertEqual(projection, {
            "branch": "codex/live-branch",
            "commit": "123456789abc",
            "origin_main_commit": "abcdef123456",
            "origin_main_available": True,
            "main_action_available": True,
            "branch_cleanup_available": False,
        })

    @patch("engineering_platform.server_console_services.GitProvider")
    def test_workspace_git_projection_hides_main_action_when_origin_is_unavailable(self, git_provider: object) -> None:
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "main\n", ""),
            completed(("git",), 0, "123456789abcde\n", ""),
        ]

        projection = _workspace_git_projection(Path("/workspace"))

        self.assertEqual(projection, {
            "branch": "main",
            "commit": "123456789abc",
            "origin_main_commit": "Niet beschikbaar",
            "origin_main_available": False,
            "main_action_available": False,
            "branch_cleanup_available": True,
        })

    @patch("engineering_platform.server_console_services.GitProvider")
    def test_workspace_git_projection_is_safe_when_git_cannot_start(self, git_provider: object) -> None:
        git_provider.return_value.execute.side_effect = OSError("Git unavailable")

        projection = _workspace_git_projection(Path("/workspace"))

        self.assertEqual(projection, {
            "branch": "Niet beschikbaar",
            "commit": "Niet beschikbaar",
            "origin_main_commit": "Niet beschikbaar",
            "origin_main_available": False,
            "main_action_available": False,
            "branch_cleanup_available": False,
        })

    @patch("engineering_platform.server_console_services.GitProvider")
    def test_workspace_worktrees_projection_lists_each_local_branch(self, git_provider: object) -> None:
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.return_value = completed(
            ("git",), 0,
            "worktree /workspace\nHEAD 123456789abcde\nbranch refs/heads/main\n\n"
            "worktree /tmp/polish\nHEAD abcdef12345678\nbranch refs/heads/codex/polish\n\n"
            "worktree /tmp/detached\nHEAD ffffff12345678\ndetached\n",
            "",
        )

        projection = _workspace_worktrees(Path("/workspace"))

        self.assertEqual(projection, {"available": True, "worktrees": [
            {"path": "/workspace", "branch": "main", "commit": "123456789abc", "detached": False, "active": True},
        {"path": "/tmp/polish", "branch": "codex/polish", "commit": "abcdef123456", "detached": False, "active": False, "removable": True},
            {"path": "/tmp/detached", "branch": None, "commit": "ffffff123456", "detached": True, "active": False},
        ]})

    @patch("engineering_platform.server_console_services.GitProvider")
    def test_workspace_worktrees_projection_includes_unchecked_out_main(self, git_provider: object) -> None:
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "worktree /workspace\nHEAD 123456789abcde\nbranch refs/heads/codex/feature\n", ""),
            completed(("git",), 0, "abcdef1234567890\n", ""),
        ]

        projection = _workspace_worktrees(Path("/workspace"))

        self.assertEqual(projection, {"available": True, "worktrees": [
            {"path": None, "branch": "main", "commit": "abcdef123456", "detached": False, "checked_out": False},
            {"path": "/workspace", "branch": "codex/feature", "commit": "123456789abc", "detached": False, "active": True, "removable": True},
        ]})

    def test_open_worktree_in_finder_accepts_only_a_current_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            worktree.mkdir()
            completed = __import__("subprocess").CompletedProcess(("open",), 0, "", "")
            with (
                patch("engineering_platform.server_console_services.sys.platform", "darwin"),
                patch("engineering_platform.server_console_services._workspace_worktrees", return_value={"available": True, "worktrees": [{"path": str(worktree)}]}),
                patch("engineering_platform.server_console_services.LocalProcessProvider.execute", return_value=completed) as execute,
            ):
                self.assertEqual(_open_worktree_in_finder(root, str(worktree)), {"opened_worktree": str(worktree.resolve())})
                execute.assert_called_once_with(root, ("open", str(worktree.resolve())))
            with patch("engineering_platform.server_console_services.sys.platform", "darwin"), patch(
                "engineering_platform.server_console_services._workspace_worktrees", return_value={"available": True, "worktrees": []},
            ):
                with self.assertRaises(RuntimeError):
                    _open_worktree_in_finder(root, str(worktree))
            with patch("engineering_platform.server_console_services.sys.platform", "darwin"):
                with self.assertRaisesRegex(RuntimeError, "actuele lokale worktree"):
                    _open_worktree_in_finder(root, str(root / "missing"))
            with (
                patch("engineering_platform.server_console_services.sys.platform", "darwin"),
                patch("engineering_platform.server_console_services._workspace_worktrees", return_value={"available": True, "worktrees": [{"path": str(worktree)}]}),
                patch("engineering_platform.server_console_services.LocalProcessProvider.execute", side_effect=OSError),
            ):
                with self.assertRaisesRegex(RuntimeError, "Finder kon"):
                    _open_worktree_in_finder(root, str(worktree))
            with patch("engineering_platform.server_console_services.sys.platform", "linux"):
                with self.assertRaisesRegex(RuntimeError, "kan niet veilig"):
                    _open_worktree_in_finder(root, str(worktree))
            with (
                patch("engineering_platform.server_console_services.sys.platform", "darwin"),
                patch("engineering_platform.server_console_services._workspace_worktrees", return_value={"available": True, "worktrees": [{"path": str(worktree)}]}),
                patch("engineering_platform.server_console_services.LocalProcessProvider.execute", return_value=__import__("subprocess").CompletedProcess(("open",), 1, "", "")),
            ):
                with self.assertRaisesRegex(RuntimeError, "Finder kon"):
                    _open_worktree_in_finder(root, str(worktree))

    def test_open_local_directory_in_finder_accepts_only_current_dashboard_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            (root / ".engineering").mkdir()
            inbox = Path(temporary) / "Inbox"
            inbox.mkdir()
            configuration = MagicMock()
            configuration.resolver.return_value.resolve_runtime_prompt_transport.return_value.inbox = inbox
            completed = __import__("subprocess").CompletedProcess(("open",), 0, "", "")
            with (
                patch("engineering_platform.server_console_services.sys.platform", "darwin"),
                patch("engineering_platform.server_console_services.PlatformConfiguration.load", return_value=configuration),
                patch("engineering_platform.server_console_services._workspace_worktrees", return_value={"available": True, "worktrees": []}),
                patch("engineering_platform.server_console_services.LocalProcessProvider.execute", return_value=completed) as execute,
            ):
                self.assertEqual(
                    dashboard._open_local_directory_in_finder(root, str(inbox)),
                    {"opened_directory": str(inbox.resolve())},
                )
                execute.assert_called_once_with(root, ("open", str(inbox.resolve())))
                with self.assertRaisesRegex(RuntimeError, "niet beschikbaar"):
                    dashboard._open_local_directory_in_finder(root, str(Path(temporary)))

    def test_dashboard_exposes_the_canonical_five_locale_catalog(self) -> None:
        root = Path(__file__).parents[2]
        catalog = (root / "src/engineering_platform/assets/dashboard_locales.mjs").read_text(encoding="utf-8")
        page = _dashboard_html("Engineering Status").decode("utf-8")

        self.assertIn('id="dashboardLocale"', page)
        self.assertIn('id="executionRuntimeBanner"', page)
        self.assertIn('data-project-id="onbekend" data-project-name="Project"', page)
        self.assertIn('src="/assets/dashboard.js', page)
        for locale in ("en", "nl", "de", "fr", "es"):
            self.assertIn(f"  {locale}: {{", catalog)
            self.assertIn(f'"language.{locale}"', catalog)
        self.assertIn('"retry.details"', catalog)
        self.assertNotIn('"configuration.audit_logging"', catalog)
        for key in (
            "project.label",
            "detail.recommended_next_mission", "detail.recommendation_status", "detail.mission_origin",
            "detail.business_value", "detail.confidence", "detail.dependencies", "detail.alternatives",
            "detail.decision_evidence", "detail.projection_incomplete", "technical.git_lock",
            "technical.git_lock_recovery_action", "detail.execution_diagnostic",
            "lifecycle.detail_quality_evidence", "lifecycle.quality_evidence.test_coverage",
            "section.configuration", "description.configuration", "configuration.inbox_location",
            "configuration.inbox_scan_interval", "configuration.open_pr_interval",
            "configuration.ep_database", "configuration.ep_database_description", "configuration.ep_database_download",
            "configuration.ep_database_maintenance", "configuration.ep_database_maintenance_help",
            "configuration.dashboard_stream_interval", "configuration.seconds_15",
            "configuration.seconds_30", "configuration.second_1",
            "configuration.inbox_location_help", "configuration.inbox_scan_interval_help",
            "configuration.operator_merge_interval", "configuration.operator_merge_interval_help",
            "configuration.required_checks_interval", "configuration.required_checks_interval_help",
            "configuration.open_pr_interval_help", "configuration.dashboard_stream_interval_help",
            "configuration.platform_health_interval", "configuration.platform_health_interval_help",
            "configuration.component_details_interval", "configuration.component_details_interval_help",
            "configuration.provider_readiness_interval", "configuration.provider_readiness_interval_help",
            "configuration.lease_heartbeat_interval", "configuration.lease_heartbeat_interval_help",
            "configuration.lease_timeout", "configuration.lease_timeout_help",
            "configuration.github_retry_backoff", "configuration.github_retry_backoff_help",
            "configuration.seconds_5", "configuration.seconds_60", "configuration.seconds_90",
            "configuration.minute_1", "configuration.minutes_5", "configuration.minutes_10",
            "configuration.github_retry_backoff_value",
            "configuration.timeout_policy", "configuration.timeout_policy_description",
            "configuration.timeout.specialist_review", "configuration.timeout.implementation",
            "configuration.timeout.local_repository_validation", "configuration.timeout.autonomous_quality_control",
            "configuration.timeout.repair", "configuration.timeout.finalization",
            "configuration.timeout.end_reconciliation", "configuration.minutes_15",
            "configuration.inbox_location_open", "configuration.inbox_location_modal_description",
            "configuration.inbox_location_queue_not_empty",
            "configuration.inbox_location_input", "configuration.inbox_location_browse", "configuration.inbox_location_requirement",
            "configuration.inbox_location_save", "configuration.inbox_location_confirm",
            "configuration.inbox_location_saved", "configuration.inbox_location_failed",
            "configuration.safe_settings", "configuration.dashboard_settings", "configuration.log_retention", "configuration.log_level", "configuration.retention_confirm",
            "configuration.telemetry_retention", "configuration.telemetry_retention_help", "configuration.telemetry_retention_confirm",
            "configuration.days",
            "configuration.saved", "configuration.save_failed", "configuration.load_failed",
        ):
            self.assertEqual(catalog.count(f'"{key}"'), 5)
        self.assertNotIn("Retry Execution", (root / "src/engineering_platform/assets/dashboard.js").read_text(encoding="utf-8"))
        dashboard_script = (root / "src/engineering_platform/assets/dashboard.js").read_text(encoding="utf-8")
        self.assertIn("createLocaleService", dashboard_script)
        self.assertNotIn('"nl-NL"', dashboard_script)
        self.assertNotIn("localeCompare(", dashboard_script)
        self.assertIn("initializeDashboardConfiguration", dashboard_script)
        self.assertIn("scheduleProviderReadinessRefresh", dashboard_script)

    def test_component_versions_match_the_canonical_platform_manifest(self) -> None:
        manifest = EngineeringPlatformManifest.load(package_path("ENGINEERING_PLATFORM_VERSION.json"))

        self.assertEqual(DASHBOARD_VERSION, manifest.dashboard_version)


    @patch("engineering_platform.server_console_services.LaunchdProvider")
    def test_launch_agent_health_rejects_a_loaded_agent_without_a_process(self, launchd: object) -> None:
        launchd.return_value.runtime_status.return_value = ProviderStatus(
            "launchd", "configured", False, "LaunchAgent is loaded but has no active process"
        )
        self.assertEqual(
            dashboard._launch_agent_health("com.example.watcher"),
            {
                "healthy": False,
                "state": "not_running",
                "detail": "LaunchAgent is geladen, maar heeft geen actief proces",
            },
        )

    @patch("engineering_platform.server_console_services.LaunchdProvider")
    def test_launch_agent_health_reports_a_real_process_and_unavailable_launchctl(self, launchd: object) -> None:
        launchd.return_value.runtime_status.return_value = ProviderStatus(
            "launchd", "configured", True, "LaunchAgent has an active process"
        )
        self.assertEqual(
            dashboard._launch_agent_health("com.example.watcher"),
            {"healthy": True, "state": "running", "detail": "LaunchAgent-proces is actief"},
        )

        launchd.return_value.runtime_status.return_value = ProviderStatus(
            "launchd", "unavailable", False, "launchctl unavailable"
        )
        self.assertEqual(
            dashboard._launch_agent_health("com.example.watcher"),
            {"healthy": False, "state": "unavailable", "detail": "launchctl ontbreekt"},
        )

    @patch("engineering_platform.server_console_services.LocalProcessProvider")
    @patch("engineering_platform.server_console_services.shutil.which", return_value="/usr/sbin/lsof")
    def test_workspace_git_lock_only_becomes_recoverable_when_lsof_proves_it_stale(
        self, which: object, process_provider: object
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / ".git" / "index.lock"
            lock.parent.mkdir()
            lock.write_text("", encoding="utf-8")
            lock.touch()
            process_provider.return_value.execute.return_value = __import__("subprocess").CompletedProcess(
                ("lsof",), 1, "", ""
            )
            stale = dashboard._workspace_git_lock(
                root, now=lock.stat().st_mtime + dashboard.GIT_INDEX_LOCK_STALE_SECONDS
            )
            self.assertEqual(stale["state"], "stale")
            self.assertTrue(stale["stale"])

            process_provider.return_value.execute.return_value = __import__("subprocess").CompletedProcess(
                ("lsof",), 0, "1234\n", ""
            )
            active = dashboard._workspace_git_lock(
                root, now=lock.stat().st_mtime + dashboard.GIT_INDEX_LOCK_STALE_SECONDS
            )
            self.assertEqual(active["state"], "active")
            self.assertFalse(active["stale"])

            which.return_value = None
            unavailable = dashboard._workspace_git_lock(
                root, now=lock.stat().st_mtime + dashboard.GIT_INDEX_LOCK_STALE_SECONDS * 2
            )
            self.assertEqual(unavailable["state"], "active")
            self.assertFalse(unavailable["stale"])


    @patch("engineering_platform.server_console_services.GitHubProvider")
    @patch("engineering_platform.server_console_services.GitProvider")
    def test_detached_commit_evidence_prefers_a_verified_merged_pull_request(
        self, git_provider: object, github_provider: object
    ) -> None:
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.return_value = completed(("git",), 0, "", "")
        github_provider.return_value.github.return_value = json.dumps([
            {"number": 1, "html_url": "https://example.test/pull/1", "state": "closed", "merged_at": None, "merge_commit_sha": None},
            {"number": 2, "html_url": "https://example.test/pull/2", "state": "closed", "merged_at": "2026-08-27T00:00:00Z", "merge_commit_sha": "merge-head"},
        ])

        self.assertEqual(
            dashboard._github_pull_request_for_detached_commit(
                Path("/repository"), "pcvantol/djconnect", "a0496fea7ef1", "main", git_provider.return_value,
            ),
            {"number": 2, "url": "https://example.test/pull/2", "state": "MERGED", "verified": True},
        )

    @patch("engineering_platform.server_console_services.GitHubProvider")
    @patch("engineering_platform.server_console_services.GitProvider")
    def test_detached_commit_evidence_keeps_an_open_pull_request_as_the_best_blocker(
        self, git_provider: object, github_provider: object
    ) -> None:
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.return_value = completed(("git",), 1, "", "")
        github_provider.return_value.github.return_value = json.dumps([
            {"number": 1, "html_url": "https://example.test/pull/1", "state": "closed", "merged_at": None, "merge_commit_sha": None},
            {"number": 2, "html_url": "https://example.test/pull/2", "state": "open", "merged_at": None, "merge_commit_sha": None},
            {"number": "invalid", "html_url": "https://example.test/pull/3", "state": "open"},
        ])

        self.assertEqual(
            dashboard._github_pull_request_for_detached_commit(
                Path("/repository"), "pcvantol/djconnect", "a0496fea7ef1", "main", git_provider.return_value,
            ),
            {"number": 2, "url": "https://example.test/pull/2", "state": "OPEN", "verified": False},
        )

    @patch("engineering_platform.server_console_services.GitHubProvider")
    def test_detached_commit_evidence_fails_closed_for_an_invalid_github_response(self, github_provider: object) -> None:
        github_provider.return_value.github.return_value = "{}"

        self.assertIsNone(
            dashboard._github_pull_request_for_detached_commit(
                Path("/repository"), "pcvantol/djconnect", "a0496fea7ef1", "main", MagicMock(),
            )
        )


    @patch("engineering_platform.server_console_services._workspace_worktrees")
    def test_registered_worktree_path_rejects_invalid_and_ambiguous_selectors(self, worktrees: object) -> None:
        root = Path("/repository")
        with self.assertRaisesRegex(ValueError, "ongeldig"):
            dashboard._registered_worktree_path(root, None)

        worktrees.return_value = {"worktrees": [
            {"path": "/worktrees/duplicate", "branch": "codex/selected"},
            {"path": "/worktrees/duplicate", "branch": "codex/selected"},
        ]}
        with self.assertRaisesRegex(RuntimeError, "niet beschikbaar"):
            dashboard._registered_worktree_path(root, "/worktrees/duplicate", "codex/selected")

    @patch("engineering_platform.server_console_services.GitHubProvider")
    @patch("engineering_platform.server_console_services.GitProvider")
    def test_workspace_open_pull_requests_are_bounded_display_safe_context(
        self, git_provider: object, github_provider: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.return_value = completed(
            ("git",), 0, "git@github.com:pcvantol/djconnect.git\n", ""
        )
        github_provider.return_value.github.return_value = json.dumps([
            {"number": 849, "title": "Cleanup <safe>", "url": "https://github.com/pcvantol/djconnect/pull/849", "headRefName": "codex/cleanup", "isDraft": False, "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED", "reviews": [{"author": {"login": "pcvantol"}, "state": "APPROVED", "submittedAt": "2026-08-24T12:00:00Z"}], "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]},
            {"number": "invalid", "title": "Ignored", "url": "https://github.com/pcvantol/djconnect/pull/0", "headRefName": "codex/ignored"},
        ])

        pull_requests = dashboard._workspace_open_pull_requests(root)

        self.assertEqual(pull_requests, [{
            "number": 849, "title": "Cleanup <safe>", "url": "https://github.com/pcvantol/djconnect/pull/849", "branch": "codex/cleanup", "status": "ready_to_merge", "owner_approval": "approved",
            "owner_authorization_requested": False,
            "failed_checks": [],
            "check_repair_available": False,
            "check_repair_state": None,
            "check_repair_completed_for_head": False,
        }])
        page = _dashboard_html(
            "Engineering Status", workspace_branch="codex/cleanup", workspace_commit="123456789abc",
            origin_main_commit="abcdef123456", origin_main_available=True,
            workspace_open_pull_requests=pull_requests, workspace_main_action_hidden=False,
            workspace_branch_cleanup_hidden=True,
        ).decode()
        self.assertIn('data-i18n="workspace.open_pull_requests"', page)
        self.assertIn('id="workspaceOpenPullRequestsRefresh"', page)
        self.assertIn('data-i18n-aria-label="workspace.open_pull_requests_refresh"', page)
        self.assertIn('PR #849 — Cleanup &lt;safe&gt;', page)
        self.assertIn("codex/cleanup", page)
        self.assertNotIn('id="workspaceBranchMain" type="button" hidden', page)
        self.assertNotIn('workspaceBranchCleanup', page)

        github_provider.return_value.github.side_effect = RuntimeError("offline")
        self.assertIsNone(dashboard._workspace_open_pull_requests(root))

        github_provider.return_value.github.side_effect = None
        git_provider.return_value.execute.return_value = completed(("git",), 1, "", "")
        self.assertIsNone(dashboard._workspace_open_pull_requests(root))

        git_provider.return_value.execute.return_value = completed(
            ("git",), 0, "https://github.com/pcvantol/djconnect.git\n", ""
        )
        github_provider.return_value.github.return_value = "{}"
        self.assertIsNone(dashboard._workspace_open_pull_requests(root))

    def test_open_pull_request_status_is_fail_closed_and_terminal(self) -> None:
        self.assertEqual(dashboard._open_pull_request_status({}), "waiting_for_checks")
        self.assertEqual(dashboard._open_pull_request_status({"isDraft": True}), "draft")
        self.assertEqual(dashboard._open_pull_request_status({
            "mergeStateStatus": "CLEAN", "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": None}],
        }), "waiting_for_checks")
        self.assertEqual(dashboard._open_pull_request_status({
            "mergeStateStatus": "CLEAN", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
        }), "issues")
        self.assertEqual(dashboard._open_pull_request_status({
            "mergeStateStatus": "DIRTY", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        }), "issues")
        self.assertEqual(dashboard._open_pull_request_status({
            "mergeStateStatus": "BEHIND", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        }), "branch_update_required")
        self.assertEqual(dashboard._open_pull_request_status({
            "mergeStateStatus": "CLEAN", "statusCheckRollup": [],
        }), "ready_to_merge")
        self.assertEqual(dashboard._open_pull_request_status({
            "mergeStateStatus": "CLEAN", "reviewDecision": "REVIEW_REQUIRED",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        }), "ready_for_review")
        self.assertEqual(dashboard._open_pull_request_status({
            "mergeStateStatus": "CLEAN", "statusCheckRollup": [{"state": "SUCCESS"}],
        }), "ready_to_merge")
        self.assertEqual(dashboard._open_pull_request_status({
            "mergeStateStatus": "CLEAN", "reviewDecision": "CHANGES_REQUESTED",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        }), "issues")
        self.assertEqual(dashboard._owner_approval_status({"reviews": []}, "pcvantol"), "pending")
        self.assertEqual(dashboard._owner_approval_status({"reviews": [], "reviewDecision": None}, "pcvantol"), "not_required")
        self.assertEqual(dashboard._owner_approval_status({
            "reviews": [], "reviewDecision": None,
            "statusCheckRollup": [{
                "__typename": "StatusContext", "context": "Owner Authorization", "state": "FAILURE",
            }],
        }, "pcvantol"), "pending")
        self.assertEqual(dashboard._owner_approval_status({
            "reviews": [], "reviewDecision": None,
            "statusCheckRollup": [{
                "__typename": "StatusContext", "context": "Owner Authorization", "state": "SUCCESS",
            }],
        }, "pcvantol"), "approved")
        self.assertEqual(dashboard._owner_approval_status({"reviews": [], "reviewDecision": "REVIEW_REQUIRED"}, "pcvantol"), "pending")
        self.assertEqual(dashboard._owner_approval_status({"reviews": [
            {"author": {"login": "pcvantol"}, "state": "APPROVED", "submittedAt": "2026-08-24T12:00:00Z"},
            {"author": {"login": "pcvantol"}, "state": "CHANGES_REQUESTED", "submittedAt": "2026-08-24T12:01:00Z"},
        ]}, "pcvantol"), "changes_requested")

    @patch("engineering_platform.server_console_services.GitHubProvider")
    @patch("engineering_platform.server_console_services.GitProvider")
    def test_owner_authorization_dispatches_only_current_qualified_high_risk_sha(
        self, git_provider: object, github_provider: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        candidate_sha = "a" * 40
        git_provider.return_value.execute.return_value = completed(
            ("git",), 0, "git@github.com:pcvantol/djconnect.git\n", ""
        )
        github_provider.return_value.github.side_effect = [
            json.dumps({
                "number": 940,
                "state": "OPEN",
                "headRefOid": candidate_sha,
                "baseRefName": "main",
                "statusCheckRollup": [
                    {"__typename": "StatusContext", "context": "Owner Authorization", "state": "FAILURE"},
                    {"__typename": "CheckRun", "name": "Trusted Delivery qualification / Qualify trusted delivery", "status": "COMPLETED", "conclusion": "SUCCESS"},
                ],
            }),
            "",
        ]

        self.assertEqual(
            dashboard._request_owner_authorization(root, 940),
            {"queued": True, "pull_request": 940},
        )
        self.assertEqual(
            github_provider.return_value.github.call_args_list,
            [
                call(
                    "pr", "view", "940", "--repo", "pcvantol/djconnect",
                    "--json", "number,state,headRefOid,baseRefName,statusCheckRollup",
                ),
                call(
                    "workflow", "run", "owner-authorization.yml", "--repo", "pcvantol/djconnect",
                    "-f", "repository=pcvantol/djconnect", "-f", "pr_number=940",
                    "-f", f"candidate_sha={candidate_sha}", "-f", "branch=main",
                ),
            ],
        )

    @patch("engineering_platform.server_console_services.GitHubProvider")
    @patch("engineering_platform.server_console_services.GitProvider")
    def test_owner_authorization_refuses_incomplete_or_stale_github_evidence(
        self, git_provider: object, github_provider: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        candidate_sha = "b" * 40
        git_provider.return_value.execute.return_value = completed(
            ("git",), 0, "git@github.com:pcvantol/djconnect.git\n", ""
        )
        qualified = {
            "number": 940,
            "state": "OPEN",
            "headRefOid": candidate_sha,
            "baseRefName": "main",
            "statusCheckRollup": [
                {"__typename": "StatusContext", "context": "Owner Authorization", "state": "FAILURE"},
                {"__typename": "CheckRun", "name": "Trusted Delivery qualification / Qualify trusted delivery", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
        }

        with self.assertRaisesRegex(dashboard.OwnerAuthorizationRequestError, "invalid_request"):
            dashboard._request_owner_authorization(root, 0)

        git_provider.return_value.execute.return_value = completed(("git",), 0, "https://example.invalid/repo.git\n", "")
        with self.assertRaisesRegex(dashboard.OwnerAuthorizationRequestError, "unavailable"):
            dashboard._request_owner_authorization(root, 940)

        git_provider.return_value.execute.return_value = completed(
            ("git",), 0, "git@github.com:pcvantol/djconnect.git\n", ""
        )
        github_provider.return_value.github.side_effect = RuntimeError("offline")
        with self.assertRaisesRegex(dashboard.OwnerAuthorizationRequestError, "unavailable"):
            dashboard._request_owner_authorization(root, 940)

        github_provider.return_value.github.side_effect = None
        github_provider.return_value.github.return_value = "[]"
        with self.assertRaisesRegex(dashboard.OwnerAuthorizationRequestError, "unavailable"):
            dashboard._request_owner_authorization(root, 940)

        github_provider.return_value.github.return_value = json.dumps({"number": 940})
        with self.assertRaisesRegex(dashboard.OwnerAuthorizationRequestError, "not_requested"):
            dashboard._request_owner_authorization(root, 940)

        missing_qualification = {**qualified, "statusCheckRollup": qualified["statusCheckRollup"][:1]}
        github_provider.return_value.github.return_value = json.dumps(missing_qualification)
        with self.assertRaisesRegex(dashboard.OwnerAuthorizationRequestError, "qualification_pending"):
            dashboard._request_owner_authorization(root, 940)

        github_provider.return_value.github.side_effect = [json.dumps(qualified), RuntimeError("dispatch failed")]
        with self.assertRaisesRegex(dashboard.OwnerAuthorizationRequestError, "dispatch_failed"):
            dashboard._request_owner_authorization(root, 940)

    def test_rate_limit_helpers_cover_generic_windows_and_unavailable_provider_version(self) -> None:
        self.assertEqual(dashboard._rate_limit_window_label(1_440), "1-daags venster")
        self.assertEqual(dashboard._rate_limit_window_label(120), "2-uursvenster")
        self.assertEqual(dashboard._rate_limit_window_label(17), "17-minutenvenster")
        self.assertEqual(dashboard._normalize_rate_limits([]), {})
        self.assertEqual(dashboard._normalize_rate_limits({"rateLimits": []}), {})

        dashboard._codex_identity_cache = None
        with patch("engineering_platform.server_console_services.codex_cli_executable", return_value=None):
            self.assertEqual(
                dashboard._codex_provider_identity(),
                {"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"},
            )

    def test_codex_cli_update_check_and_install_are_version_pinned_and_verified(self) -> None:
        completed = __import__("subprocess").CompletedProcess
        root = Path("/workspace")
        dashboard._codex_identity_cache = None
        dashboard._codex_update_cache = None

        with (
            patch("engineering_platform.server_console_services.shutil.which", side_effect=lambda name: f"/usr/local/bin/{name}"),
            patch("engineering_platform.server_console_services.codex_cli_executable", return_value="/managed/bin/codex"),
            patch("engineering_platform.server_console_services.LocalProcessProvider.execute", side_effect=[
                completed(("codex", "--version"), 0, "codex-cli 0.149.0", ""),
                completed(("npm", "view"), 0, '"0.150.0"', ""),
            ]) as execute,
        ):
            self.assertEqual(
                dashboard._codex_cli_update_status(root, refresh=True),
                {"state": "update_available", "update_available": True, "current_version": "0.149.0", "latest_version": "0.150.0"},
            )
            self.assertEqual(execute.call_args_list[1].args[1], ("/usr/local/bin/npm", "view", "@openai/codex", "version", "--json"))

        with patch("engineering_platform.server_console_services.managed_codex_runtime.provision", return_value={"updated": True, "current_version": "0.150.0"}) as provision:
            self.assertEqual(dashboard._install_codex_cli_update(root), {"updated": True, "current_version": "0.150.0"})
            provision.assert_called_once_with(root)

    def test_codex_cli_update_installation_is_blocked_during_an_active_execution(self) -> None:
        with patch(
            "engineering_platform.server_console_services._status",
            return_value=b'{"watcher_state":"ENGINEERING_RUN_ACTIVE","run_id":"inbox-active"}',
        ), patch("engineering_platform.server_console_services._codex_cli_update_status") as check:
            with self.assertRaisesRegex(dashboard.CodexCliUpdateError, "codex_cli_update_execution_active"):
                dashboard._install_codex_cli_update(Path("/workspace"))
            check.assert_not_called()

    def test_codex_cli_update_reports_unavailable_current_and_failed_install_states(self) -> None:
        root = Path("/workspace")
        dashboard._codex_update_cache = None
        with (
            patch("engineering_platform.server_console_services._codex_provider_identity", return_value={"provider_version": "not-a-version"}),
            patch("engineering_platform.server_console_services._npm_executable", return_value=None),
        ):
            self.assertEqual(
                dashboard._codex_cli_update_status(root, refresh=True),
                {"state": "unavailable", "update_available": False},
            )

        with patch("engineering_platform.server_console_services._execution_active", return_value=False), patch(
            "engineering_platform.server_console_services.managed_codex_runtime.provision",
            return_value={"updated": False, "current_version": "0.150.0"},
        ):
            self.assertEqual(
                dashboard._install_codex_cli_update(root),
                {"updated": False, "current_version": "0.150.0"},
            )

        with patch("engineering_platform.server_console_services._execution_active", return_value=False), patch(
            "engineering_platform.server_console_services.managed_codex_runtime.provision",
            side_effect=dashboard.managed_codex_runtime.ManagedCodexRuntimeError("codex_cli_update_unavailable"),
        ):
            with self.assertRaisesRegex(dashboard.CodexCliUpdateError, "codex_cli_update_unavailable"):
                dashboard._install_codex_cli_update(root)

        with patch("engineering_platform.server_console_services._execution_active", return_value=False), patch(
            "engineering_platform.server_console_services.managed_codex_runtime.provision",
            side_effect=dashboard.managed_codex_runtime.ManagedCodexRuntimeError("codex_cli_update_permissions_required"),
        ):
            with self.assertRaisesRegex(dashboard.CodexCliUpdateError, "codex_cli_update_permissions_required"):
                dashboard._install_codex_cli_update(root)

    def test_component_processes_and_metrics_ignore_invalid_process_rows(self) -> None:
        self.assertEqual(dashboard._component_processes("unknown"), [])
        with patch("engineering_platform.server_console_services.subprocess.run", side_effect=OSError):
            self.assertEqual(dashboard._component_processes("dashboard"), [])
        with patch("engineering_platform.server_console_services.subprocess.run") as run:
            run.return_value = __import__("subprocess").CompletedProcess(
                ("ps",),
                0,
                "bad\n1 x 3 dashboard.py\n2 4 5 python -m engineering_platform.server_console_services run\n3 12 01:05 python -m engineering_platform.server_console_services run\n4 32 00:20 python -m engineering_platform.server_console_services /tmp/djconnect-dashboard-test-example\n",
                "",
            )
            # The retired direct-dashboard process is no longer a component.
            self.assertEqual(dashboard._component_processes("dashboard"), [])
        self.assertEqual(dashboard._process_elapsed_seconds("2-01:02:03"), 176_523)
        with tempfile.TemporaryDirectory() as temporary, patch("engineering_platform.server_console_services.subprocess.run") as run:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "current.json").write_text('{"run_id":"run-owned"}', encoding="utf-8")
            (status / "runner_process.json").write_text('{"run_id":"run-owned","pid":3,"process_group":42}', encoding="utf-8")
            run.return_value = __import__("subprocess").CompletedProcess(
                ("ps",), 0, "1 1 99.0 codex unrelated\n2 42 1.5 worker child\n3 42 2.5 codex exec\n", ""
            )
            metrics = json.loads(dashboard._codex_process_metrics(root))
        self.assertEqual(metrics["process_count"], 2)
        self.assertEqual(metrics["cpu_percent"], 4.0)

    def test_report_and_runtime_projections_reject_invalid_or_unavailable_input(self) -> None:
        root = Path("/missing")
        self.assertEqual(dashboard._report_for_run(root, "INVALID"), b"")
        self.assertEqual(dashboard._report_analysis_for_run(root, "INVALID"), b"")
        self.assertFalse(dashboard._report_analysis_available_for_run(root, "INVALID"))
        self.assertEqual(dashboard._reviewer_agents_for_run(root, "run-1"), b"[]")
        self.assertEqual(dashboard._last_executed_agent_execution(root, "INVALID"), b"{}")
        self.assertEqual(dashboard._last_executed_runtime_metadata(root, "INVALID"), b"{}")

    def test_launch_agent_details_and_component_details_handle_unavailable_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "engineering_platform.server_console_services.Path.home", return_value=Path(temporary)
        ):
            details = dashboard._launch_agent_details("com.example.missing")
        self.assertFalse(details["loaded"])
        self.assertEqual(details["program_arguments"], [])
        with patch("engineering_platform.server_console_services._platform_health", return_value={"components": {}}):
            with self.assertRaisesRegex(ValueError, "Onbekend Engineering Platform-onderdeel"):
                dashboard._component_details(Path("/missing"), "missing")

    def test_local_dashboard_supervisor_preserves_private_and_resilient_boundaries(self) -> None:
        source = package_path("dashboard_supervisor.swift").read_text(encoding="utf-8")
        self.assertIn("tailscale", source)
        self.assertIn("SO_NOSIGPIPE", source)
        self.assertIn("Thread.sleep(forTimeInterval: 5)", source)
        self.assertNotIn("0.0.0.0", source)

    def test_dashboard_serves_assets_and_minimal_semantic_contract(self) -> None:
        page = _dashboard_html("Engineering Status").decode()

        self.assertIn("<title>Engineering Status</title>", page)
        self.assertIn('href="/assets/dashboard.css?build=onbekend"', page)
        self.assertIn('src="/assets/dashboard.js?build=onbekend" type="module"', page)
        self.assertIn('id="pageRefresh"', page)
        self.assertIn('id="promptHistoryScrollHint"', page)
        self.assertIn('aria-describedby="promptHistoryScrollHint"', page)
        self.assertIn('role="region" tabindex="0"', page)
        self.assertLess(page.index('id="currentFile"'), page.index('id="executionIdentity"'))
        self.assertLess(page.index('id="executionIdentity"'), page.index('id="indicator"'))
        self.assertLess(page.index('id="indicator"'), page.index('id="executionContext"'))
        self.assertLess(page.index('id="executionContext"'), page.index('id="processMetrics"'))

        for identifier in (
            "dashboardSplash",
            "engineering-dashboard-content",
            "currentRun",
            "platformHealth",
            "componentLogs",
            "codexChat",
            "confirmationModal",
            "dashboardErrorModal",
        ):
            self.assertIn(f'id="{identifier}"', page)

        root = Path("src/engineering_platform/assets")
        self.assertTrue((root / "dashboard.css").is_file())
        self.assertTrue((root / "dashboard.js").is_file())
        self.assertTrue((root / "dashboard_locales.mjs").is_file())
        self.assertTrue((root / "dashboard_status_store.mjs").is_file())
        stylesheet = (root / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn("touch-action:manipulation", stylesheet)
        self.assertIn("height:100dvh;min-height:100dvh", stylesheet)
        self.assertIn(":is(input,select,textarea){font-size:16px}", stylesheet)
        self.assertIn("body.dashboard-modal-open", stylesheet)
        self.assertIn("--report-modal-surface", stylesheet)
        self.assertIn("background:var(--report-modal-surface)", stylesheet)
        self.assertIn(".execution-history-action{background:#4f453c", stylesheet)
        self.assertIn("min-height:32px;min-width:0;padding:5px 9px", stylesheet)
        self.assertIn(".execution-history-action:hover:not(:disabled){background:#e7b876", stylesheet)
        self.assertIn(
            "#promptHistoryRows td:has(.prompt-history-actions){vertical-align:middle}",
            stylesheet,
        )
        self.assertIn("Dashboard UI component layer", stylesheet)
        self.assertIn("--dashboard-section-gap:24px", stylesheet)
        self.assertIn(".history-scroll-hint", stylesheet)
        self.assertIn("#promptHistory .log-table th:first-child", stylesheet)

    def test_execution_context_keeps_host_verified_target_details(self) -> None:
        script = (Path(__file__).parents[2] / "src" / "engineering_platform" / "assets" / "dashboard.js").read_text()
        self.assertIn("function renderExecutionContext(context, execution = {})", script)
        self.assertIn('[t("field.repository"), execution.target_repository]', script)
        self.assertIn('[t("detail.target_checkout"), execution.checkout_path]', script)
        self.assertIn('[t("ui.active_branch"), execution.active_branch]', script)
        self.assertIn("renderExecutionContext(x.execution_context, x);", script)
        root = Path("src/engineering_platform/assets")
        stylesheet = (root / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn("gap:var(--dashboard-section-gap)", stylesheet)
        self.assertIn("scrollbar-gutter:stable", stylesheet)
        self.assertIn(".inbox-queue,.prompt-history", stylesheet)
        self.assertIn("#promptHistory .log-table-wrap{isolation:isolate}", stylesheet)
        self.assertIn("#promptHistory .log-table tbody", stylesheet)
        self.assertIn("box-shadow:none", stylesheet)
        self.assertIn(".reset-log-filters", stylesheet)
        self.assertIn("--dashboard-control-label-gap:8px", stylesheet)
        self.assertIn("row-gap:var(--dashboard-control-label-gap)", stylesheet)
        script = (root / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("resetLogFiltersButton", script)
        self.assertNotIn("logRunFilter", script)
        self.assertIn("function filteredComponentLogEntries", script)
        self.assertIn("function renderComponentLogs", script)
        self.assertNotIn("renderLegacyComponentLogs", script)
        self.assertNotIn("renderSortedComponentLogs", script)
        self.assertNotIn("renderPaginatedComponentLogs", script)
        self.assertNotIn('id="logSort"', (Path(__file__).parents[2] / "src/engineering_platform/server_console_services.py").read_text(encoding="utf-8"))

    def test_codex_usage_is_shown_only_for_the_displayed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "status.json").write_text('{"run_id":"inbox-visible"}', encoding="utf-8")
            (status / "codex_usage.json").write_text(
                '{"run_id":"inbox-visible","usage":{"input_tokens":123,"cost":1.25}}',
                encoding="utf-8",
            )
            self.assertEqual(
                json.loads(_codex_usage(root)), {"input_tokens": 123, "cost": 1.25}
            )
            with open_storage(root) as connection:
                store_projection(
                    connection,
                    "watcher_status",
                    {"run_id": "inbox-active", "last_executed_run": "inbox-visible"},
                )
            self.assertEqual(
                json.loads(_codex_usage(root)), {},
                "Usage from the prior run must never appear on an active run.",
            )
            (status / "codex_usage.json").write_text(
                '{"run_id":"inbox-active","usage":{"input_tokens":456}}',
                encoding="utf-8",
            )
            self.assertEqual(json.loads(_codex_usage(root)), {"input_tokens": 456})
            (status / "codex_usage.json").write_text(
                '{"run_id":"inbox-other","usage":{"input_tokens":123}}', encoding="utf-8"
            )
            self.assertEqual(json.loads(_codex_usage(root)), {})

    def test_last_executed_usage_is_bound_to_its_exact_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "codex_usage.json").write_text(
                '{"run_id":"inbox-last","usage":{"input_tokens":123,"cost":1.25}}',
                encoding="utf-8",
            )
            self.assertEqual(
                json.loads(_codex_usage_for_run(root, "inbox-last")),
                {"input_tokens": 123, "cost": 1.25},
            )
            self.assertEqual(json.loads(_codex_usage_for_run(root, "inbox-other")), {})

    def test_rate_limits_keep_only_safe_window_and_reset_count_fields(self) -> None:
        limits = _normalize_rate_limits(
            {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 12.2,
                        "windowDurationMins": 300,
                        "resetsAt": 1_786_162_124,
                    },
                    "secondary": {
                        "usedPercent": 3,
                        "windowDurationMins": 10_080,
                        "resetsAt": 1_786_200_000,
                    },
                },
                "rateLimitResetCredits": {"availableCount": 2, "credits": [{"id": "secret"}]},
                "account": {"email": "do-not-display@example.invalid"},
            }
        )

        self.assertEqual(
            limits,
            {
                "windows": [
                    {
                        "label": "5-uursvenster",
                        "used_percent": 12,
                        "window_minutes": 300,
                        "resets_at": 1_786_162_124,
                    },
                    {
                        "label": "Weekvenster",
                        "used_percent": 3,
                        "window_minutes": 10_080,
                        "resets_at": 1_786_200_000,
                    },
                ],
                "reset_credits": 2,
            },
        )

    def test_rate_limit_labels_and_malformed_limit_payloads_fail_closed(self) -> None:
        self.assertEqual(dashboard._rate_limit_window_label(1_440), "1-daags venster")
        self.assertEqual(dashboard._rate_limit_window_label(120), "2-uursvenster")
        self.assertEqual(dashboard._rate_limit_window_label(17), "17-minutenvenster")
        self.assertEqual(dashboard._normalize_rate_limits(None), {})
        self.assertEqual(dashboard._normalize_rate_limits({"rateLimits": []}), {})
        self.assertEqual(
            dashboard._normalize_rate_limits(
                {"rateLimits": {"primary": {"usedPercent": True, "windowDurationMins": 300, "resetsAt": 1}}}
            ),
            {},
        )

    @patch("engineering_platform.server_console_services.GitHubProvider")
    def test_github_rate_limit_status_only_reports_exhausted_or_explicit_limits(self, github_provider: object) -> None:
        github_provider.return_value.github.return_value = json.dumps({
            "resources": {"core": {"remaining": 0, "reset": 1_786_162_124}, "graphql": {"remaining": 4, "reset": 0}},
        })
        self.assertEqual(dashboard._github_rate_limit_status(), {"limited": True, "reset_at": 1_786_162_124})
        github_provider.return_value.github.return_value = json.dumps({"resources": {"core": {"remaining": 1, "reset": 1}}})
        self.assertEqual(dashboard._github_rate_limit_status(), {"limited": False})
        github_provider.return_value.github.side_effect = RuntimeError("API rate limit exceeded")
        self.assertEqual(dashboard._github_rate_limit_status(), {"limited": True})
        github_provider.return_value.github.side_effect = RuntimeError("authentication failed")
        self.assertEqual(dashboard._github_rate_limit_status(), {"limited": False})

    @patch("engineering_platform.server_console_services.subprocess.run")
    @patch("engineering_platform.server_console_services.engineering_platform_codex_cli_prefix", return_value=Path("/managed/codex-cli"))
    @patch("engineering_platform.server_console_services.codex_cli_executable", return_value="/managed/codex-cli/bin/codex")
    def test_codex_provider_identity_includes_the_managed_cli_path(
        self, _: object, __: object, run: object
    ) -> None:
        run.return_value = __import__("subprocess").CompletedProcess(
            ("codex", "--version"), 0, "OpenAI Codex v0.146.0", ""
        )
        dashboard._codex_identity_cache = None
        self.assertEqual(
            _codex_provider_identity(),
            {
                "provider": "Codex CLI",
                "provider_version": "0.146.0",
                "provider_path": "/managed/codex-cli",
            },
        )
        dashboard._codex_identity_cache = None

    def test_codex_cli_installation_path_uses_the_managed_prefix(self) -> None:
        managed_prefix = dashboard.engineering_platform_codex_cli_prefix()
        self.assertEqual(
            _codex_cli_installation_path(str(managed_prefix / "bin" / "codex")),
            str(managed_prefix),
        )

    def test_codex_cli_installation_path_rejects_non_managed_executables(self) -> None:
        self.assertIsNone(_codex_cli_installation_path("codex"))
        self.assertIsNone(_codex_cli_installation_path("/opt/homebrew/bin/codex"))

    def test_codex_rate_limits_reads_a_deterministic_app_server_response(self) -> None:
        class RecordingInput:
            def __init__(self) -> None:
                self.chunks: list[str] = []
                self.closed = False

            def write(self, value: str) -> int:
                self.chunks.append(value)
                return len(value)

            def flush(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

            def getvalue(self) -> str:
                return "".join(self.chunks)

        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = RecordingInput()
                self.stdout = io.StringIO(
                    "\n".join(
                        (
                            json.dumps({"id": 1, "result": {}}),
                            json.dumps(
                                {
                                    "id": 2,
                                    "result": {
                                        "rateLimits": {
                                            "primary": {
                                                "usedPercent": 12,
                                                "windowDurationMins": 300,
                                                "resetsAt": 1_786_162_124,
                                            }
                                        },
                                        "rateLimitResetCredits": {"availableCount": 2},
                                    },
                                }
                            ),
                        )
                    )
                    + "\n"
                )
                self.terminated = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float) -> None:
                return None

        process = FakeProcess()
        with (
            patch("engineering_platform.server_console_services.subprocess.Popen", return_value=process),
            patch("engineering_platform.server_console_services.select.select", return_value=([process.stdout], [], [])),
            patch("engineering_platform.providers.codex_cli_executable", return_value="/managed/bin/codex"),
            patch(
                "engineering_platform.server_console_services._codex_provider_identity",
                return_value={"provider": "Codex CLI", "provider_version": "0.146.0"},
            ),
        ):
            dashboard._rate_limit_cache = None
            result = json.loads(dashboard._codex_rate_limits())
            dashboard._rate_limit_cache = None
        self.assertEqual(result["reset_credits"], 2)
        self.assertEqual(result["windows"][0]["label"], "5-uursvenster")
        self.assertIn('"method": "initialize"', process.stdin.getvalue())
        self.assertIn('"method": "account/rateLimits/read"', process.stdin.getvalue())
        self.assertTrue(process.terminated)
        self.assertTrue(process.stdin.closed)

    def test_codex_rate_limits_fails_closed_when_app_server_streams_are_unavailable(self) -> None:
        class FakeProcess:
            stdin = None
            stdout = None

            def __init__(self) -> None:
                self.terminated = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float) -> None:
                return None

        process = FakeProcess()
        with (
            patch("engineering_platform.server_console_services.subprocess.Popen", return_value=process),
            patch("engineering_platform.providers.codex_cli_executable", return_value="/managed/bin/codex"),
            patch(
                "engineering_platform.server_console_services._codex_provider_identity",
                return_value={"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"},
            ),
        ):
            dashboard._rate_limit_cache = None
            result = json.loads(dashboard._codex_rate_limits())
            self.assertEqual(result, {"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"})
            dashboard._rate_limit_cache = None
        self.assertTrue(process.terminated)

    def test_codex_rate_limits_fails_closed_when_app_server_cannot_start(self) -> None:
        with (
            patch("engineering_platform.server_console_services.subprocess.Popen", side_effect=OSError),
            patch("engineering_platform.providers.codex_cli_executable", return_value="/managed/bin/codex"),
            patch(
                "engineering_platform.server_console_services._codex_provider_identity",
                return_value={"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"},
            ),
        ):
            dashboard._rate_limit_cache = None
            result = json.loads(dashboard._codex_rate_limits())
            self.assertEqual(result, {"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"})
            dashboard._rate_limit_cache = None

    def test_codex_rate_limit_reset_consumes_one_credit_with_an_idempotency_key(self) -> None:
        class RecordingInput:
            def __init__(self) -> None:
                self.chunks: list[str] = []

            def write(self, value: str) -> int:
                self.chunks.append(value)
                return len(value)

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = RecordingInput()
                self.stdout = io.StringIO(
                    "\n".join(
                        (
                            json.dumps({"id": 1, "result": {}}),
                            json.dumps({"id": 2, "result": {"outcome": "reset"}}),
                        )
                    )
                    + "\n"
                )
                self.terminated = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float) -> None:
                return None

        process = FakeProcess()
        with (
            patch("engineering_platform.server_console_services.subprocess.Popen", return_value=process),
            patch("engineering_platform.server_console_services.select.select", return_value=([process.stdout], [], [])),
            patch("engineering_platform.providers.codex_cli_executable", return_value="/managed/bin/codex"),
        ):
            self.assertEqual(dashboard._consume_codex_rate_limit_reset_credit(), "reset")
        request = "".join(process.stdin.chunks)
        self.assertIn('"method": "account/rateLimitResetCredit/consume"', request)
        self.assertIn('"idempotencyKey":', request)
        self.assertTrue(process.terminated)

    def test_completion_commits_are_shown_only_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            runs = root / ".engineering" / "engineering-runs"
            status.mkdir(parents=True)
            runs.mkdir(parents=True)
            (status / "status.json").write_text('{"run_id":"inbox-done","current_phase":"COMPLETE"}', encoding="utf-8")
            (runs / "inbox-done.json").write_text('{"genesis_commit_sha":"' + "a" * 40 + '"}', encoding="utf-8")
            self.assertEqual(json.loads(_completion_commits(root)), {"Genesis-commit": "a" * 40})

    def test_last_executed_commits_are_bound_to_the_completed_last_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            runs = root / ".engineering" / "engineering-runs"
            status.mkdir(parents=True)
            runs.mkdir(parents=True)
            (status / "status.json").write_text(
                '{"last_executed_run":"inbox-done","last_executed_phase":"COMPLETE"}', encoding="utf-8"
            )
            (runs / "inbox-done.json").write_text(
                '{"implementation_merge_commit":"' + "b" * 40 + '"}', encoding="utf-8"
            )
            self.assertEqual(json.loads(_last_executed_commits(root)), {"Implementatie-mergecommit": "b" * 40})

    def test_last_executed_agent_execution_is_bound_to_the_exact_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / ".engineering" / "engineering-runs"
            runs.mkdir(parents=True)
            (runs / "inbox-last.json").write_text(
                '{"agent_execution_seconds":125.4}', encoding="utf-8"
            )
            (runs / "inbox-other.json").write_text(
                '{"agent_execution_seconds":999}', encoding="utf-8"
            )

            self.assertEqual(
                json.loads(_last_executed_agent_execution(root, "inbox-last")),
                {"seconds": 125.4},
            )
            self.assertEqual(json.loads(_last_executed_agent_execution(root, "invalid/run")), {})

    def test_last_executed_runtime_metadata_is_bound_to_its_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / ".engineering" / "reports"
            reports.mkdir(parents=True)
            (reports / "one_inbox-other.md").write_text(
                "- AI Model: `other-model`\n", encoding="utf-8"
            )
            (reports / "two_inbox-last.md").write_text(
                "\n".join(
                    (
                        "- Runtime Provider: `codex_cli`",
                        "- AI Model: `gpt-5.6-terra`",
                        "- Reasoning Profile: `medium`",
                        "- Configuration Profile: `workspace-write`",
                        "- Codex CLI Version: `0.146.0`",
                        "- Codex CLI Installation Path: `/managed/engineering-platform/codex-cli`",
                    )
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                json.loads(_last_executed_runtime_metadata(root, "inbox-last")),
                {
                    "runtime_provider": "codex_cli",
                    "model": "gpt-5.6-terra",
                    "reasoning_profile": "medium",
                    "configuration_profile": "workspace-write",
                    "codex_cli_version": "0.146.0",
                    "codex_cli_installation_path": "/managed/engineering-platform/codex-cli",
                },
            )
            self.assertEqual(json.loads(_last_executed_runtime_metadata(root, "bad/run")), {})

    def test_missing_status_uses_a_complete_degraded_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = json.loads(_status(Path(temporary)))

        self.assertEqual(status["watcher_state"], "REMOTE_ENGINEERING_DEGRADED")
        self.assertEqual(status["queue_depth"], 0)
        self.assertEqual(status["repository_state"], "UNKNOWN")
        self.assertEqual(status["workspace_state"], "UNKNOWN")
        self.assertIn("geen lokale engineeringstatus", status["diagnostic"])

    def test_live_runner_status_is_a_dashboard_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".engineering" / "status"
            directory.mkdir(parents=True)
            (directory / "current.json").write_text(
                json.dumps(
                    {
                        "run_id": "inbox-123",
                        "phase": "INITIALIZE",
                        "current_action": "Capability selection",
                        "transient_action": "Integrating runtime resolution",
                        "repository_state": "ACTIVE",
                        "workspace_state": "ACTIVE",
                        "prompt_characters": 4321,
                    }
                ),
                encoding="utf-8",
            )
            root = Path(temporary)
            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState("inbox-123", "repo", "prompt.md", "INITIALIZE")
            )
            acquire(root, "inbox-123", identity="test-host", instance_id="test-instance")
            status = json.loads(_status(Path(temporary)))

        self.assertEqual(status["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(status["current_phase"], "INITIALIZE")
        self.assertEqual(status["run_id"], "inbox-123")
        self.assertEqual(status["prompt_characters"], 4321)
        self.assertEqual(status["current_action"], "Integrating runtime resolution")

    def test_active_runner_status_wins_over_previous_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".engineering" / "status"
            directory.mkdir(parents=True)
            (directory / "status.json").write_text('{"current_phase":"BLOCKED"}', encoding="utf-8")
            (directory / "current.json").write_text(
                '{"run_id":"inbox-new","phase":"INITIALIZE","current_action":"Starting"}',
                encoding="utf-8",
            )
            status = json.loads(_status(Path(temporary)))

        self.assertEqual(status["current_phase"], "BLOCKED")
        self.assertIsNone(status.get("run_id"))

    def test_live_runner_status_preserves_the_watcher_queue_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".engineering" / "status"
            directory.mkdir(parents=True)
            (directory / "status.json").write_text(
                '{"queue_items":[{"filename":"later.md","title":"Later prompt","modified_at":"2026-08-01T10:00:00+00:00"}]}',
                encoding="utf-8",
            )
            (directory / "current.json").write_text(
                '{"run_id":"inbox-active","phase":"EXECUTE_AGENT"}', encoding="utf-8"
            )
            status = json.loads(_status(Path(temporary)))

        self.assertEqual(status["queue_items"][0]["filename"], "later.md")
        self.assertEqual(status["queue_items"][0]["title"], "Later prompt")

    def test_sse_status_is_single_line_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".engineering" / "status"
            directory.mkdir(parents=True)
            (directory / "status.json").write_text('{\n  "watcher_state": "WATCHER_IDLE"\n}', encoding="utf-8")
            payload = _sse_status(Path(temporary))

        self.assertNotIn(b"\n", payload)
        self.assertEqual(json.loads(payload)["watcher_state"], "WATCHER_IDLE")

    @patch("engineering_platform.server_console_services.subprocess.run")
    def test_tracked_file_count_counts_recursive_git_index_entries(self, run: object) -> None:
        run.return_value = __import__("subprocess").CompletedProcess(
            ("git",), 0, b"README.md\0docs/guide.md\0src/engineering_platform/server_console_services.py\0", b""
        )
        self.assertEqual(_tracked_file_count(Path("/workspace")), "3")

    @patch("engineering_platform.server_console_services._codex_rate_limits", return_value=b"{}")
    def test_sse_snapshot_contains_the_read_only_dashboard_projection(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".engineering" / "status"
            directory.mkdir(parents=True)
            (directory / "status.json").write_text('{"watcher_state":"WATCHER_IDLE"}', encoding="utf-8")
            snapshot = json.loads(_sse_snapshot(Path(temporary)))

        self.assertEqual(snapshot["status"]["watcher_state"], "WATCHER_IDLE")
        self.assertIn("build_commit", snapshot)
        self.assertIsInstance(snapshot["snapshot_source"], str)
        self.assertGreater(snapshot["snapshot_revision"], 0)
        self.assertEqual(snapshot["prompt_started"], {})
        self.assertEqual(snapshot["usage"], {})
        self.assertEqual(snapshot["rate_limits"], {})
        self.assertEqual(snapshot["telemetry"], [])
        self.assertIn("operations_console", snapshot["component_log_versions"])
        self.assertNotIn("inbox", snapshot["component_log_versions"])
        self.assertNotIn("dashboard", snapshot["component_log_versions"])
        self.assertNotEqual(snapshot["component_versions"]["worker"], "inbox-watcher")
        self.assertEqual(snapshot["workspace_git_lock"], {"state": "free", "active": False, "stale": False})
        self.assertEqual(snapshot["workspace_git"]["branch"], "Niet beschikbaar")
        self.assertIn("workspace_worktrees", snapshot)

    def test_latest_codex_log_is_local_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / ".engineering" / "logs" / "codex"
            logs.mkdir(parents=True)
            (logs / "run.log").write_text("redacted diagnostic", encoding="utf-8")
            self.assertEqual(_latest_codex_log(Path(temporary)), b"redacted diagnostic")

    @patch("engineering_platform.server_console_services.subprocess.run")
    def test_codex_process_metrics_ignore_unowned_codex_processes(self, run: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "current.json").write_text('{"run_id":"run-owned"}', encoding="utf-8")
            (status / "runner_process.json").write_text('{"run_id":"run-owned","pid":103,"process_group":303}', encoding="utf-8")
            run.return_value = __import__("subprocess").CompletedProcess(
                ("ps",), 0,
                "101  101  12.4 /opt/homebrew/bin/codex exec unrelated\n102  102  3.1 /usr/bin/python worker.py\n103  303  7.5 codex exec owned\n104  303  2.5 child worker\n", "",
            )
            metrics = json.loads(_codex_process_metrics(root))
        self.assertEqual(metrics["process_count"], 2)
        self.assertEqual(metrics["cpu_percent"], 10.0)
        self.assertIn("Execution Host-verwerking", metrics["gpu_status"])

    def test_current_codex_log_never_falls_back_to_a_different_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            logs = root / ".engineering" / "logs" / "codex"
            status.mkdir(parents=True)
            logs.mkdir(parents=True)
            (status / "current.json").write_text('{"run_id":"inbox-new","phase":"INITIALIZE"}', encoding="utf-8")
            (logs / "inbox-old.log").write_text("old diagnostic", encoding="utf-8")
            self.assertIn(b"huidige uitvoering", _current_codex_log(root))
            (logs / "inbox-new.log").write_text("new diagnostic", encoding="utf-8")
            self.assertEqual(_current_codex_log(root), b"new diagnostic")

    def test_last_executed_log_is_bound_to_last_executed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            logs = root / ".engineering" / "logs" / "codex"
            status.mkdir(parents=True)
            logs.mkdir(parents=True)
            (status / "status.json").write_text('{"last_executed_run":"inbox-last"}', encoding="utf-8")
            (logs / "inbox-other.log").write_text("old diagnostic", encoding="utf-8")
            self.assertIn(b"laatst uitgevoerde uitvoering", _last_executed_codex_log(root))
            (logs / "inbox-last.log").write_text("last diagnostic", encoding="utf-8")
            self.assertEqual(_last_executed_codex_log(root), b"last diagnostic")

    def test_report_is_bound_to_the_requested_last_executed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / ".engineering" / "reports"
            reports.mkdir(parents=True)
            (reports / "one_inbox-other.md").write_text("other", encoding="utf-8")
            (reports / "two_inbox-last.md").write_text("last", encoding="utf-8")
            self.assertEqual(_report_for_run(root, "inbox-last"), b"last")
            self.assertEqual(_report_for_run(root, "inbox-missing"), b"")

    def test_report_prefers_the_indexed_terminal_report_over_a_duplicate_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / ".engineering" / "reports"
            reports.mkdir(parents=True)
            actual = reports / "2026-08-03T19-40-44Z_inbox-last.md"
            fallback = reports / "corrected_inbox-last.md"
            actual.write_text("actual", encoding="utf-8")
            fallback.write_text("fallback", encoding="utf-8")
            from engineering_platform.prompt_history import record_prompt_execution
            record_prompt_execution(
                root,
                run_id="inbox-last",
                terminal_state="COMPLETE",
                prompt_title="Indexed report",
                executed_at="2026-08-03T19:40:44Z",
                report=actual,
            )

            self.assertEqual(_report_for_run(root, "inbox-last"), b"actual")

    def test_reviewer_agents_are_derived_from_the_exact_terminal_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / ".engineering" / "reports"
            reports.mkdir(parents=True)
            (reports / "2026-08-01T10-00-00Z_inbox-last.md").write_text(
                "\n".join(
                    (
                        "# Engineering Report",
                        "",
                        "## Reviewer Findings",
                        "Initial observations only.",
                        "- Reviewer: repository_governance",
                        "  - Capability: engineering",
                        "  - Selected because: repository-governance objective",
                        "  - Accepted recommendations: 2",
                        "- Reviewer: validation",
                        "  - Capability: validation",
                        "  - Selected because: validation objective",
                        "  - Accepted recommendations: 1",
                        "",
                        "## Repository Truth",
                        "Repository evidence is authoritative.",
                    )
                ),
                encoding="utf-8",
            )
            (reports / "2026-08-01T10-00-01Z_inbox-other.md").write_text(
                "## Reviewer Findings\n- Reviewer: other\n", encoding="utf-8"
            )

            self.assertEqual(
                json.loads(_reviewer_agents_for_run(root, "inbox-last")),
                [
                    {
                        "reviewer": "repository_governance",
                        "capability": "engineering",
                        "selected_because": "repository-governance objective",
                        "accepted_recommendations": 2,
                        "status": "Uitgevoerd",
                    },
                    {
                        "reviewer": "validation",
                        "capability": "validation",
                        "selected_because": "validation objective",
                        "accepted_recommendations": 1,
                        "status": "Uitgevoerd",
                    },
                ],
            )
            self.assertEqual(json.loads(_reviewer_agents_for_run(root, "inbox-other")), [{"reviewer": "other", "capability": "engineering", "selected_because": "Niet vastgelegd.", "accepted_recommendations": 0, "status": "Uitgevoerd"}])

    def test_report_analysis_is_bound_to_the_requested_last_executed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analyses = root / ".engineering" / "report-analysis"
            analyses.mkdir(parents=True)
            (analyses / "inbox-other.md").write_text("other analysis", encoding="utf-8")
            (analyses / "inbox-last.md").write_text("last analysis", encoding="utf-8")
            self.assertEqual(_report_analysis_for_run(root, "inbox-last"), b"last analysis")
            self.assertEqual(_report_analysis_for_run(root, "inbox-missing"), b"")
            self.assertTrue(_report_analysis_available_for_run(root, "inbox-last"))
            self.assertFalse(_report_analysis_available_for_run(root, "inbox-missing"))

    def test_report_analysis_defence_in_depth_rejects_a_normalized_path_change(self) -> None:
        """The filesystem guard remains effective independently of the id grammar.

        The allowlisted run-id grammar makes a basename change unreachable in
        normal operation.  Cover the second boundary explicitly so a future
        grammar widening cannot turn the report reader into path traversal.
        """
        with tempfile.TemporaryDirectory() as temporary, patch(
            "engineering_platform.server_console_services.os.path.basename", return_value="other-run",
        ):
            root = Path(temporary)
            self.assertEqual(_report_analysis_for_run(root, "run-1"), b"")
            self.assertFalse(_report_analysis_available_for_run(root, "run-1"))

    def test_report_analysis_retry_is_limited_to_temporary_processing_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analyses = root / ".engineering" / "report-analysis"
            analyses.mkdir(parents=True)
            report = root / ".engineering" / "reports" / "terminal.md"
            report.parent.mkdir(parents=True)
            report.write_text("# Engineering Report\n", encoding="utf-8")
            record_prompt_execution(
                root,
                run_id="inbox-retryable",
                terminal_state="COMPLETE",
                prompt_title="Retryable analysis",
                executed_at="2026-08-28T12:00:00Z",
                report=report,
            )
            (analyses / "inbox-retryable.md").write_text(
                "## Analyseverwerking\n- Status: `provider_unavailable`\n",
                encoding="utf-8",
            )
            generated = analyses / "inbox-retryable.md"

            def regenerate(*_args: object) -> Path:
                generated.write_text("## Analyseverwerking\n- Status: `processed`\n", encoding="utf-8")
                return generated

            with patch("engineering_platform.server_console_services.analyze_terminal_report", side_effect=regenerate) as analyze:
                self.assertEqual(_retry_report_analysis(root, "inbox-retryable"), generated.read_bytes())
            analyze.assert_called_once_with(root, "inbox-retryable", report.resolve())
            self.assertEqual(_report_analysis_processing_status(root, "inbox-retryable"), "processed")
            with self.assertRaisesRegex(ValueError, "hoeft niet opnieuw"):
                _retry_report_analysis(root, "inbox-retryable")

    def test_prompt_history_marks_only_the_matching_ai_analysis_as_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analyses = root / ".engineering" / "report-analysis"
            analyses.mkdir(parents=True)
            (analyses / "inbox-one.md").write_text("analysis", encoding="utf-8")
            record_prompt_execution(
                root,
                run_id="inbox-one",
                terminal_state="COMPLETE",
                prompt_title="One",
                executed_at="2026-08-03T12:00:00Z",
            )
            record_prompt_execution(
                root,
                run_id="inbox-two",
                terminal_state="COMPLETE",
                prompt_title="Two",
                executed_at="2026-08-03T11:00:00Z",
            )
            runs = json.loads(_prompt_history(root))["runs"]
            self.assertTrue(runs[0]["analysis_available"])
            self.assertFalse(runs[1]["analysis_available"])

    def test_prompt_history_detail_is_scoped_to_its_exact_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_prompt_execution(
                root,
                run_id="inbox-detail",
                terminal_state="COMPLETE",
                prompt_title="Detail prompt",
                executed_at="2026-08-03T12:00:00Z",
            )
            StateStore(root / ".engineering" / "engineering-runs").save(TransactionState(
                "inbox-detail", "pcvantol/djconnect", "prompt.md", "COMPLETE", terminal=True,
                implementation_pull_request=948, finalization_pull_request=949,
            ))
            payload = json.loads(_prompt_history_detail(root, "inbox-detail"))
            self.assertEqual(payload["history"]["run_id"], "inbox-detail")
            self.assertEqual(payload["history"]["title"], "Detail prompt")
            self.assertEqual(payload["usage"], {"invocation_detail": "UNAVAILABLE"})
            self.assertEqual(payload["pull_requests"], [
                {"role": "implementation", "number": 948, "url": "https://github.com/pcvantol/djconnect/pull/948"},
                {"role": "finalization", "number": 949, "url": "https://github.com/pcvantol/djconnect/pull/949"},
            ])
            self.assertEqual(_prompt_history_detail(root, "../../other"), b"")

    @patch("engineering_platform.server_console_services.GitHubProvider")
    @patch("engineering_platform.server_console_services.GitProvider")
    def test_managed_pull_request_evidence_includes_verified_github_counts(
        self, git_provider: MagicMock, github_provider: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            StateStore(root / ".engineering" / "engineering-runs").save(TransactionState(
                "inbox-pr-counts", "pcvantol/djconnect", "prompt.md", "COMPLETE", terminal=True,
                implementation_pull_request=948, finalization_pull_request=949,
            ))
            git_provider.return_value.execute.return_value = __import__("subprocess").CompletedProcess(
                ("git",), 0, "git@github.com:pcvantol/djconnect.git\n", "",
            )

            def github(*args: str) -> str:
                number = int(args[2])
                return json.dumps({
                    "number": number,
                    "commits": [{"oid": "a"}, {"oid": "b"}],
                    "changedFiles": 5,
                    "statusCheckRollup": [
                        {"__typename": "CheckRun", "name": "validate"},
                        {"__typename": "StatusContext", "context": "Owner Authorization"},
                        {},
                    ],
                })

            github_provider.return_value.github.side_effect = github
            self.assertEqual(dashboard._pull_requests_for_run(root, "inbox-pr-counts"), [
                {
                    "role": "implementation", "number": 948,
                    "url": "https://github.com/pcvantol/djconnect/pull/948",
                    "commit_count": 2, "check_count": 1, "changed_file_count": 5,
                },
                {
                    "role": "finalization", "number": 949,
                    "url": "https://github.com/pcvantol/djconnect/pull/949",
                    "commit_count": 2, "check_count": 1, "changed_file_count": 5,
                },
            ])

    def test_prompt_history_detail_omits_pr_links_without_managed_checkpoint_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_prompt_execution(
                root, run_id="inbox-genesis-detail", terminal_state="COMPLETE",
                prompt_title="Genesis detail", executed_at="2026-08-03T12:00:00Z",
            )
            StateStore(root / ".engineering" / "engineering-runs").save(TransactionState(
                "inbox-genesis-detail", "pcvantol/djconnect", "prompt.md", "COMPLETE",
                execution_mode="GENESIS", implementation_pull_request=948,
                finalization_pull_request=949, terminal=True,
            ))
            self.assertEqual(
                json.loads(_prompt_history_detail(root, "inbox-genesis-detail"))["pull_requests"], []
            )

    def test_prompt_history_detail_projects_run_scoped_provider_usage_without_fabricating_legacy_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected_run = "inbox-provider-usage"
            other_run = "inbox-other-provider-usage"
            for run_id in (selected_run, other_run, "inbox-legacy-usage"):
                record_prompt_execution(
                    root,
                    run_id=run_id,
                    terminal_state="COMPLETE",
                    prompt_title="Provider usage",
                    executed_at="2026-08-18T12:00:00Z",
                )
            for invocation in (
                ProviderInvocation(
                    run_id=selected_run, ordinal=1, provider="codex_cli", model="gpt-5.6-terra", model_authority="AUTHORITATIVE", raw_provider_model="gpt-5.6-terra",
                    phase="PROVIDER_EXECUTION", role="agent", started_at="2026-08-18T12:00:00Z",
                    completed_at="2026-08-18T12:00:01Z", duration_ms=1000,
                    usage={"input_tokens": 100, "cached_input_tokens": 25, "output_tokens": 10},
                    runtime_metadata={"configuration_profile": "normal"},
                ),
                ProviderInvocation(
                    run_id=selected_run, ordinal=2, provider="codex_cli", model="gpt-5.6-terra", model_authority="AUTHORITATIVE", raw_provider_model="gpt-5.6-terra",
                    phase="PROVIDER_EXECUTION", role="reviewer", started_at="2026-08-18T12:01:00Z",
                    completed_at="2026-08-18T12:01:01Z", duration_ms=1000,
                    usage={"input_tokens": 300, "cached_input_tokens": 100, "output_tokens": 20},
                    runtime_metadata={"configuration_profile": "normal"},
                ),
                ProviderInvocation(
                    run_id=other_run, ordinal=1, provider="codex_cli", model="gpt-5.6-terra", model_authority="AUTHORITATIVE", raw_provider_model="gpt-5.6-terra",
                    phase="PROVIDER_EXECUTION", role="agent", started_at="2026-08-18T12:02:00Z",
                    completed_at="2026-08-18T12:02:01Z", duration_ms=1000,
                    usage={"input_tokens": 999, "cached_input_tokens": 0, "output_tokens": 1},
                    runtime_metadata={"configuration_profile": "Fast Mode"},
                ),
            ):
                persist_provider_invocation(root, invocation)
            with open_storage(root) as connection:
                connection.execute(
                    """INSERT INTO execution_runs(
                        run_id, execution_date, arrived_at, execution_started_at,
                        execution_finished_at, queue_wait_seconds, execution_seconds,
                        terminal_state, input_tokens, output_tokens, total_tokens,
                        execution_mode, workspace, repository, execution_host_version
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "inbox-legacy-usage", "2026-08-18", "2026-08-18T12:00:00Z",
                        "2026-08-18T12:00:00Z", "2026-08-18T12:00:01Z", 0, 1,
                        "COMPLETE", 40, 8, 48, "MANAGED", "workspace",
                        "pcvantol/djconnect", "1.5.0",
                    ),
                )

            usage = json.loads(_prompt_history_detail(root, selected_run))["usage"]

            self.assertEqual(usage["provider_invocation_count"], 2)
            self.assertEqual(usage["input_tokens"], 400)
            self.assertEqual(usage["cached_input_tokens"], 125)
            self.assertEqual(usage["uncached_input_tokens"], 275)
            self.assertEqual(usage["max_input_tokens_per_invocation"], 300)
            self.assertAlmostEqual(usage["estimated_credits"], 0.023375)
            self.assertAlmostEqual(usage["estimated_eur"], 0.000935)
            self.assertEqual(usage["speed_state"], "NORMAL_DEFAULT")

            legacy_usage = json.loads(_prompt_history_detail(root, "inbox-legacy-usage"))["usage"]
            self.assertEqual(legacy_usage["invocation_detail"], "UNAVAILABLE")
            self.assertEqual(legacy_usage["input_tokens"], 40)
            self.assertEqual(legacy_usage["output_tokens"], 8)
            self.assertEqual(legacy_usage["total_tokens"], 48)
            self.assertNotIn("provider_invocation_count", legacy_usage)
            self.assertNotIn("max_input_tokens_per_invocation", legacy_usage)

    def test_prompt_history_detail_scopes_repair_audit_to_its_lifecycle_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id = "inbox-repair-audit"
            record_prompt_execution(root, run_id=run_id, terminal_state="BLOCKED", prompt_title="Audit", executed_at="2026-08-03T12:00:00Z")
            store = StateStore(root / ".engineering" / "engineering-runs")
            audit = ({"iteration": "1", "observed_at": "2026-08-03T12:00:00+00:00", "failed_checks": "Ruff", "proposed_action": "Repair Ruff.", "agent_summary": "Updated lint configuration.", "commit_sha": "a" * 40, "outcome": "submitted_for_recheck"},)
            store.save(TransactionState(
                run_id, "pcvantol/djconnect", "prompt.md", "REPAIR_AGENT",
                repair_iterations=1,
                repair_audit=audit,
            ))
            store.save(TransactionState(
                run_id, "pcvantol/djconnect", "prompt.md", "BLOCKED", terminal=True,
                repair_iterations=1, repair_audit=audit,
            ))
            payload = json.loads(_prompt_history_detail(root, run_id))
            self.assertNotIn("repair_audit", payload)
            repair = next(step for step in payload["lifecycle"]["steps"] if step["id"] == "REPAIR_AGENT")
            self.assertEqual(repair["repair_audit"][0]["failed_checks"], "Ruff")

    def test_prompt_history_detail_includes_only_its_own_terminal_failure_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id = "inbox-diagnostic"
            record_prompt_execution(
                root,
                run_id=run_id,
                terminal_state="BLOCKED",
                prompt_title="Blocked prompt",
                executed_at="2026-08-17T05:42:43Z",
            )
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                    (
                        "inbox",
                        json.dumps({
                            "event": "job_failed",
                            "run_id": run_id,
                            "diagnostic": "Pre-flight is NO-GO: rolling status records are stale.",
                        }),
                        "2026-08-17T05:42:43Z",
                    ),
                )
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                    (
                        "inbox",
                        json.dumps({
                            "event": "job_failed",
                            "run_id": "inbox-other-run",
                            "diagnostic": "This diagnostic belongs to another run.",
                        }),
                        "2026-08-17T05:42:44Z",
                    ),
                )

            StateStore(root / ".engineering" / "engineering-runs").save(TransactionState(
                run_id, "pcvantol/djconnect", "prompt.md", "BLOCKED", terminal=True,
                diagnostic="Checkpoint-owned blocking reason.",
            ))

            payload = json.loads(_prompt_history_detail(root, run_id))

            self.assertEqual(
                payload["history"]["execution_diagnostic"],
                "Checkpoint-owned blocking reason.",
            )
            self.assertEqual(payload["history"]["blocking_reason"], "Checkpoint-owned blocking reason.")

    def test_prompt_history_detail_projector_owns_evidence_and_presentation(self) -> None:
        entry = {"run_id": "inbox-projector", "target_repository": "stored/repository"}

        payload = json.loads(
            dashboard._project_prompt_history_detail(
                entry,
                execution={"state": "COMPLETE"},
                runtime={"provider": "codex_cli"},
                reviewers=[{"reviewer": "validation"}],
                commits=["abc123"],
                usage={"input_tokens": 10},
                report="\n".join(
                    (
                        "- Execution Host: `Engineering Platform`",
                        "- Target Repository: `forge`",
                        "- Target Commit: `abc123`",
                        "- Changed file: `one.py`",
                    )
                ),
            )
        )

        self.assertEqual(entry["target_repository"], "stored/repository")
        self.assertEqual(payload["history"]["target_repository"], "forge")
        self.assertEqual(payload["execution"], {"state": "COMPLETE"})
        self.assertEqual(payload["usage"], {"input_tokens": 10})
        self.assertEqual(
            payload["evidence"],
            [
                "Execution Host: Engineering Platform",
                "Target repository: forge",
                "Target commit: abc123",
                "Evidence Bundle: 1 gewijzigde bestanden",
            ],
        )

        without_report = json.loads(
            dashboard._project_prompt_history_detail(
                entry,
                execution={},
                runtime={},
                reviewers=[],
                commits=[],
                usage={},
                report=None,
            )
        )
        self.assertEqual(without_report["history"], entry)
        self.assertEqual(without_report["evidence"], [])

    def test_prompt_history_detail_projects_only_verified_phase_commit_timeline(self) -> None:
        checkpoint = {
            "commit_evidence": [
                {
                    "phase": "REPAIR_AGENT",
                    "observed_at": "2026-08-26T12:02:00+00:00",
                    "commit_sha": "b" * 40,
                    "description": "pull_request_repair_commit_verified",
                },
                {
                    "phase": "EXECUTE_AGENT",
                    "observed_at": "2026-08-26T12:01:00+00:00",
                    "commit_sha": "a" * 40,
                    "description": "implementation_agent_commit_verified",
                },
                {"phase": "REPAIR_AGENT", "observed_at": "not-a-date", "commit_sha": "not-a-sha", "description": "Unsafe"},
            ]
        }
        with patch("engineering_platform.server_console_services._canonical_checkpoint", return_value=checkpoint):
            timeline = dashboard._commit_timeline_for_run(Path("/workspace"), "inbox-commit-evidence")

        self.assertEqual([event["commit_sha"] for event in timeline], ["a" * 40, "b" * 40])
        self.assertEqual(timeline[0]["phase"], "EXECUTE_AGENT")

    def test_dashboard_projects_producer_metadata_from_the_exact_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                connection.execute(
                    """INSERT INTO execution_runs(
                        run_id, execution_date, arrived_at, execution_started_at, execution_finished_at,
                        queue_wait_seconds, terminal_state, execution_mode, workspace, repository,
                        execution_host_version, producer_id, producer_type, producer_version,
                        correlation_id, mission_id, engineering_action_id, execution_constraint_version
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("inbox-producer", "2026-08-04", "now", "now", "now", 0, "COMPLETE", "MANAGED",
                     "djconnect", "pcvantol/djconnect", "1.5.0", "forge", "FORGE", "2.0", "corr-42",
                     "MISSION-0003", "EA-0042", "1.0"),
                )
            record_prompt_execution(
                root, run_id="inbox-producer", terminal_state="COMPLETE", prompt_title="Produced prompt",
                executed_at="2026-08-04T12:00:00Z",
            )
            payload = json.loads(_prompt_history(root))
            entry = payload["runs"][0]
            self.assertEqual(entry["producer_type"], "FORGE")
            self.assertEqual(entry["mission_id"], "MISSION-0003")
            self.assertEqual(entry["engineering_action_id"], "EA-0042")

    def test_prompt_history_projects_report_bound_recommendation_handoff(self) -> None:
        report = """## Forge Mission Recommendation Handoff
```json
{"artifact_path":"forge/recommendation.json","projection_status":"COMPLETE","missing_fields":[],"recommendation":{"title":"Mission Aurora","status":"RECOMMENDED"},"alternatives":[]}
```
"""
        payload = json.loads(dashboard._project_prompt_history_detail(
            {"run_id": "inbox-handoff"}, execution={}, runtime={}, reviewers=[], commits={}, usage={}, report=report
        ))
        self.assertEqual(payload["recommendation_handoff"]["recommendation"]["title"], "Mission Aurora")


    def test_prompt_history_projection_is_private_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = json.loads(_prompt_history(root))
        self.assertEqual(payload, {"runs": []})

    def test_prompt_history_and_detail_fail_closed_when_evidence_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("engineering_platform.server_console_services.prompt_history", side_effect=OSError("unavailable")):
                self.assertEqual(json.loads(_prompt_history(root)), {"runs": []})

            record_prompt_execution(
                root,
                run_id="inbox-evidence",
                terminal_state="COMPLETE",
                prompt_title="Evidence",
                executed_at="2026-08-04T12:00:00Z",
            )
            reports = root / ".engineering" / "reports"
            reports.mkdir(parents=True)
            report = reports / "2026-08-04_inbox-evidence.md"
            report.write_text(
                "\n".join(
                    (
                        "- Execution Host: `Engineering Platform`",
                        "- Target Repository: `pcvantol/djconnect`",
                        "- Target Commit: `abc123`",
                        "- Changed file: `one.py`",
                        "- Changed file: `two.py`",
                    )
                ),
                encoding="utf-8",
            )
            record_prompt_execution(
                root,
                run_id="inbox-evidence",
                terminal_state="COMPLETE",
                prompt_title="Evidence",
                executed_at="2026-08-04T12:00:00Z",
                report=report,
                target_checkout_path="/Users/example/Documents/GitHub/forge",
                tracked_file_count=1655,
                target_branch="forge-phase-evidence",
            )
            connection = MagicMock()
            connection.execute.return_value.fetchone.return_value = (10, 20, 30)
            with patch("engineering_platform.server_console_services.open_storage", return_value=connection):
                detail = json.loads(_prompt_history_detail(root, "inbox-evidence"))
            self.assertEqual(
                detail["usage"],
                {
                    "invocation_detail": "UNAVAILABLE",
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "total_tokens": 30,
                },
            )
            self.assertEqual(
                detail["evidence"],
                [
                    "Execution Host: Engineering Platform",
                    "Target repository: pcvantol/djconnect",
                    "Target commit: abc123",
                    "Evidence Bundle: 2 gewijzigde bestanden",
                ],
            )
            self.assertEqual(detail["history"]["target_repository"], "pcvantol/djconnect")
            self.assertEqual(
                detail["history"]["target_checkout_path"],
                "/Users/example/Documents/GitHub/forge",
            )
            self.assertEqual(detail["history"]["tracked_file_count"], 1655)
            self.assertEqual(detail["history"]["target_branch"], "forge-phase-evidence")

    def test_dashboard_file_projections_reject_malformed_or_missing_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "status.json").write_text("not json", encoding="utf-8")
            self.assertIn(b"huidige uitvoering", dashboard._current_codex_log(root))
            self.assertIn(b"laatst uitgevoerde", dashboard._last_executed_codex_log(root))
            self.assertEqual(dashboard._prompt_started(root), b"{}")
            self.assertEqual(dashboard._tracked_file_count(root), "Niet beschikbaar")

            (status / "status.json").write_text('{"run_id":"inbox-current"}', encoding="utf-8")
            jobs = root / ".engineering" / "inbox-processing" / "one"
            jobs.mkdir(parents=True)
            (jobs / "job.json").write_text("not json", encoding="utf-8")
            valid_job = root / ".engineering" / "inbox-processing" / "two"
            valid_job.mkdir()
            (valid_job / "job.json").write_text(
                '{"run_id":"inbox-current","received_at":"2026-08-04T12:00:00Z"}', encoding="utf-8"
            )
            self.assertEqual(
                json.loads(dashboard._prompt_started(root)), {"started_at": "2026-08-04T12:00:00Z"}
            )
