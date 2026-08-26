from __future__ import annotations

import io
import json
import logging
import sqlite3
from http.client import HTTPConnection
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from contextlib import ExitStack, contextmanager, nullcontext
from unittest.mock import ANY, MagicMock, call, patch

from tools.engineering import dashboard
from tools.engineering.dashboard import DASHBOARD_VERSION, LOOPBACK_ADDRESS, _clear_component_log, _codex_cli_installation_path, _codex_process_metrics, _codex_provider_identity, _codex_usage, _codex_usage_for_run, _component_log, _component_log_versions, _completion_commits, _component_uptime_seconds, _current_codex_log, _dashboard_html, _last_executed_agent_execution, _last_executed_codex_log, _last_executed_commits, _last_executed_runtime_metadata, _latest_codex_log, _normalize_rate_limits, _platform_health, _prompt_history, _prompt_history_detail, _report_analysis_available_for_run, _report_analysis_for_run, _report_for_run, _reviewer_agents_for_run, _sse_snapshot, _sse_status, _status, _tracked_file_count, _workspace_free_disk_space, _workspace_git_projection, _workspace_worktrees, binding_addresses
from tools.engineering.inbox_watcher import WATCHER_VERSION
from tools.engineering.platform_version import EngineeringPlatformManifest
from tools.engineering.prompt_history import record_prompt_execution
from tools.engineering.provider_usage import ProviderInvocation, persist_provider_invocation
from tools.engineering.storage import ENGINEERING_STORAGE_SCHEMA_VERSION, open_storage, store_projection
from tools.engineering.dashboard_configuration import inbox_root, update_inbox_root
from tools.engineering.agent_state import StateStore, TransactionState
from tools.engineering.execution_lease import acquire


class DashboardStatusTest(unittest.TestCase):
    def test_browser_dashboard_validation_uses_bounded_ci_workers(self) -> None:
        config = (Path(__file__).parents[2] / "playwright.config.mjs").read_text(encoding="utf-8")

        self.assertIn("fullyParallel: true", config)
        self.assertIn("workers: process.env.CI ? 1 : undefined", config)
        self.assertIn("maxFailures: process.env.CI ? 3 : undefined", config)

    def test_workspace_card_shows_free_space_on_its_volume(self) -> None:
        with patch(
            "tools.engineering.dashboard.shutil.disk_usage",
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
        self.assertIn("/private/engineering/inbox", page)
        self.assertIn('id="configurationInboxModal"', page)
        self.assertIn('id="configurationInboxBrowse"', page)
        self.assertIn('id="configurationLogRetention"', page)
        self.assertIn('id="configurationLogLevel"', page)
        self.assertNotIn('id="configurationAuditLogging"', page)
        self.assertNotIn('configuration.audit_logging', page)
        self.assertEqual(page.count('class="configuration-info"'), 7)
        self.assertEqual(page.count("data-i18n-title=\"configuration."), 7)
        for control in (
            "configurationInboxScanInterval", "configurationOpenPrInterval",
            "configurationPlatformHealthInterval", "configurationComponentDetailsInterval",
        ):
            self.assertIn(f'id="{control}"', page)
        for key, value in (
            ("configuration.inbox_scan_interval", "configuration.seconds_15"),
            ("configuration.operator_merge_interval", "configuration.seconds_60"),
            ("configuration.required_checks_interval", "configuration.seconds_15"),
            ("configuration.open_pr_interval", "configuration.seconds_30"),
            ("configuration.dashboard_stream_interval", "configuration.second_1"),
            ("configuration.platform_health_interval", "configuration.seconds_15"),
            ("configuration.component_details_interval", "configuration.seconds_5"),
            ("configuration.lease_heartbeat_interval", "configuration.seconds_15"),
            ("configuration.lease_timeout", "configuration.seconds_90"),
            ("configuration.github_retry_backoff", "configuration.github_retry_backoff_value"),
        ):
            self.assertIn(f'data-i18n="{key}"', page)
            self.assertIn(f'data-i18n="{value}"', page)
        self.assertNotIn('id="dashboardLocale"', page[page.index('id="configuration"'):])
        self.assertNotIn('id="autoRefresh"', page[page.index('id="configuration"'):])
        self.assertLess(
            page.index('id="workspaceCard"'),
            page.index('id="configuration"'),
        )
        self.assertLess(page.index('id="configuration"'), page.index("</main>"))

    @patch("tools.engineering.dashboard.provider_readiness_status")
    def test_provider_login_status_is_token_free_and_classifies_auth(
        self, readiness: MagicMock,
    ) -> None:
        readiness.return_value = {
            "codex": {"provider": "CODEX", "state": "READY"},
            "github": {"provider": "GITHUB", "state": "AUTH_REQUIRED"},
        }

        status = dashboard._provider_login_status(Path("/workspace"))

        self.assertEqual(status, {
            "codex": {"provider": "CODEX", "state": "READY"},
            "github": {"provider": "GITHUB", "state": "AUTH_REQUIRED"},
        })
        readiness.assert_called_once_with(Path("/workspace"))

    @patch("tools.engineering.dashboard._provider_login_status", return_value={"codex": {"state": "AUTH_REQUIRED"}})
    @patch("tools.engineering.dashboard.CodexCliProvider")
    @patch("tools.engineering.dashboard.LocalProcessProvider")
    @patch("tools.engineering.dashboard._npm_executable", return_value="/usr/local/bin/npm")
    @patch("tools.engineering.dashboard._execution_active", return_value=False)
    def test_codex_install_is_serialized_and_verifies_the_cli_before_login(
        self, _active: MagicMock, _npm: MagicMock, process: MagicMock, codex: MagicMock, _status: MagicMock,
    ) -> None:
        completed = __import__("subprocess").CompletedProcess
        process.return_value.execute.side_effect = [
            completed(("npm", "view"), 0, '"0.150.0"', ""),
            completed(("npm", "install"), 0, "", ""),
        ]
        codex.return_value.command.return_value = completed(("codex", "--version"), 0, "0.150.0", "")
        dashboard._install_provider(Path("/workspace"), "CODEX")
        self.assertEqual(codex.return_value.command.call_args.args, ("--version",))
        self.assertEqual(process.return_value.execute.call_count, 2)

    @patch("tools.engineering.dashboard.LocalProcessProvider")
    @patch("tools.engineering.dashboard.CodexCliProvider")
    @patch("tools.engineering.dashboard.shutil.which", return_value="/usr/local/bin/gh")
    @patch("tools.engineering.dashboard.sys.platform", "darwin")
    def test_provider_login_allows_only_one_interactive_repair(
        self, _which: MagicMock, codex: MagicMock, process: MagicMock,
    ) -> None:
        completed = __import__("subprocess").CompletedProcess
        codex.return_value.status.return_value.qualified = True
        codex.return_value._executable = "/usr/local/bin/codex"
        process.return_value.execute.return_value = completed(("osascript",), 0, "", "")
        with patch.object(dashboard, "_provider_login_active", None), patch.object(
            dashboard, "_provider_login_started_at", 0.0
        ):
            dashboard._start_provider_login(Path("/workspace"), "CODEX")
            with self.assertRaisesRegex(ValueError, "already in progress"):
                dashboard._start_provider_login(Path("/workspace"), "GITHUB")
        self.assertEqual(process.return_value.execute.call_count, 1)

    @patch("tools.engineering.dashboard.LocalProcessProvider")
    @patch("tools.engineering.dashboard.sys.platform", "darwin")
    def test_local_directory_picker_returns_a_selected_mac_folder(self, provider: MagicMock) -> None:
        completed = __import__("subprocess").CompletedProcess
        with tempfile.TemporaryDirectory() as temporary:
            provider.return_value.execute.return_value = completed(
                ("osascript",), 0, f"{temporary}\n", ""
            )
            self.assertEqual(dashboard._choose_local_directory(Path(temporary)), temporary)
        provider.return_value.execute.assert_called_once()

    @patch("tools.engineering.dashboard.GitProvider")
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

    @patch("tools.engineering.dashboard.GitProvider")
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

    @patch("tools.engineering.dashboard.GitProvider")
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

    @patch("tools.engineering.dashboard.GitProvider")
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
            {"path": "/workspace", "branch": "main", "commit": "123456789abc", "detached": False},
            {"path": "/tmp/polish", "branch": "codex/polish", "commit": "abcdef123456", "detached": False},
            {"path": "/tmp/detached", "branch": None, "commit": "ffffff123456", "detached": True},
        ]})

    @patch("tools.engineering.dashboard.GitProvider")
    def test_workspace_worktrees_projection_includes_unchecked_out_main(self, git_provider: object) -> None:
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "worktree /workspace\nHEAD 123456789abcde\nbranch refs/heads/codex/feature\n", ""),
            completed(("git",), 0, "abcdef1234567890\n", ""),
        ]

        projection = _workspace_worktrees(Path("/workspace"))

        self.assertEqual(projection, {"available": True, "worktrees": [
            {"path": None, "branch": "main", "commit": "abcdef123456", "detached": False, "checked_out": False},
            {"path": "/workspace", "branch": "codex/feature", "commit": "123456789abc", "detached": False},
        ]})

    def test_dashboard_exposes_the_canonical_five_locale_catalog(self) -> None:
        root = Path(__file__).parents[2]
        catalog = (root / "tools/engineering/assets/dashboard_locales.mjs").read_text(encoding="utf-8")
        page = _dashboard_html("Engineering Status").decode("utf-8")

        self.assertIn('id="dashboardLocale"', page)
        self.assertIn('data-project-id="onbekend" data-project-name="Project"', page)
        self.assertIn('"/assets/dashboard_locales.mjs"', (root / "tools/engineering/dashboard.py").read_text(encoding="utf-8"))
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
            "configuration.inbox_location_open", "configuration.inbox_location_modal_description",
            "configuration.inbox_location_queue_not_empty",
            "configuration.inbox_location_input", "configuration.inbox_location_browse", "configuration.inbox_location_requirement",
            "configuration.inbox_location_save", "configuration.inbox_location_confirm",
            "configuration.inbox_location_saved", "configuration.inbox_location_failed",
            "configuration.safe_settings", "configuration.log_retention", "configuration.log_level", "configuration.retention_confirm",
            "configuration.telemetry_retention", "configuration.telemetry_retention_help", "configuration.telemetry_retention_confirm",
            "configuration.days",
            "configuration.saved", "configuration.save_failed", "configuration.load_failed",
        ):
            self.assertEqual(catalog.count(f'"{key}"'), 5)
        self.assertNotIn("Retry Execution", (root / "tools/engineering/assets/dashboard.js").read_text(encoding="utf-8"))
        dashboard_script = (root / "tools/engineering/assets/dashboard.js").read_text(encoding="utf-8")
        self.assertIn("createLocaleService", dashboard_script)
        self.assertNotIn('"nl-NL"', dashboard_script)
        self.assertNotIn("localeCompare(", dashboard_script)
        self.assertIn("initializeDashboardConfiguration", dashboard_script)
        self.assertIn("scheduleProviderReadinessRefresh", dashboard_script)

    def test_dashboard_run_logs_startup_and_graceful_shutdown_identity(self) -> None:
        class InterruptingServer:
            server_address = (LOOPBACK_ADDRESS, 8765)

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lifecycle_context = {
                "application_version": DASHBOARD_VERSION,
                "git_commit": "abc123def456",
                "launchd_label": dashboard.LABEL,
                "launch_agent_path": "/tmp/dashboard.plist",
            }
            with (
                patch("tools.engineering.dashboard.provision_workspace"),
                patch("tools.engineering.dashboard.component_logger", return_value=logging.getLogger("test")) as logger,
                patch("tools.engineering.dashboard.component_lifecycle_context", return_value=lifecycle_context),
                patch("tools.engineering.dashboard.shutdown_signal_logging", return_value=nullcontext()),
                patch("tools.engineering.dashboard.create_servers", return_value=(InterruptingServer(),)),
                patch("tools.engineering.dashboard.log_event") as log_event,
            ):
                dashboard.run(root)

            logger.assert_called_once_with(root, "dashboard")
            self.assertEqual(log_event.call_args_list[0].args[2], "dashboard_started")
            self.assertEqual(log_event.call_args_list[-1].args[2], "dashboard_shutdown_completed")
            self.assertEqual(log_event.call_args_list[-1].kwargs["context"], lifecycle_context)

    def test_component_versions_match_the_canonical_platform_manifest(self) -> None:
        root = Path(__file__).parents[2]
        manifest = EngineeringPlatformManifest.load(
            root / "tools/engineering/ENGINEERING_PLATFORM_VERSION.json"
        )

        self.assertEqual(DASHBOARD_VERSION, manifest.dashboard_version)
        self.assertEqual(WATCHER_VERSION, manifest.watcher_version)

    def test_dashboard_helpers_fail_closed_for_unavailable_local_dependencies(self) -> None:
        with patch("tools.engineering.dashboard.shutil.which", return_value=None):
            self.assertEqual(
                dashboard._launch_agent_health("com.example.missing"),
                {"healthy": False, "state": "unavailable", "detail": "launchctl ontbreekt"},
            )
            with self.assertRaises(ValueError):
                dashboard._restart_component("unknown_component")
            with self.assertRaises(OSError):
                dashboard._restart_component("dashboard")

        with (
            patch("tools.engineering.dashboard.shutil.which", return_value="/bin/launchctl"),
            patch("tools.engineering.dashboard.subprocess.run") as run,
        ):
            run.return_value = __import__("subprocess").CompletedProcess(("launchctl",), 1, "", "")
            self.assertEqual(
                dashboard._launch_agent_health("com.example.missing"),
                {"healthy": False, "state": "not_running", "detail": "LaunchAgent is niet geladen"},
            )
            with self.assertRaisesRegex(OSError, "De herstart is niet gelukt"):
                dashboard._restart_component("dashboard")

    @patch("tools.engineering.dashboard.LaunchdProvider")
    @patch("tools.engineering.dashboard.GitProvider")
    def test_managed_branch_recovery_requires_a_clean_workspace_and_restarts_watcher(
        self, git_provider: object, launchd: object
    ) -> None:
        root = Path("/repository")
        git_provider.return_value.execute.side_effect = [
            __import__("subprocess").CompletedProcess(("git",), 0, "", ""),
            __import__("subprocess").CompletedProcess(("git",), 0, "codex/ui-polish\n", ""),
        ]
        self.assertEqual(
            dashboard._restore_managed_main_branch(root),
            {"previous_branch": "codex/ui-polish", "branch": "main", "watcher": "restarted"},
        )
        git_provider.return_value.command.assert_called_once_with(root, "git", "switch", "main")
        launchd.return_value.restart.assert_called_once_with(dashboard.WATCHER_LABEL)

        git_provider.return_value.execute.side_effect = [
            __import__("subprocess").CompletedProcess(("git",), 0, "M dashboard.py\n", ""),
            __import__("subprocess").CompletedProcess(("git",), 0, "codex/ui-polish\n", ""),
        ]
        with self.assertRaisesRegex(RuntimeError, "geen lokale wijzigingen"):
            dashboard._restore_managed_main_branch(root)

    @patch("tools.engineering.dashboard.LaunchdProvider")
    @patch("tools.engineering.dashboard.GitProvider")
    def test_managed_branch_synchronization_only_fast_forwards_a_clean_expected_branch(
        self, git_provider: object, launchd: object
    ) -> None:
        root = Path(__file__).parents[2]
        git_provider.return_value.execute.side_effect = [
            __import__("subprocess").CompletedProcess(("git",), 0, "", ""),
            __import__("subprocess").CompletedProcess(("git",), 0, "main\n", ""),
            __import__("subprocess").CompletedProcess(("git",), 0, "origin/main\n", ""),
            __import__("subprocess").CompletedProcess(("git",), 0, "1\t0\n", ""),
            __import__("subprocess").CompletedProcess(("git",), 0, "0\t0\n", ""),
        ]

        self.assertEqual(
            dashboard._synchronize_managed_branch_with_upstream(root),
            {"branch": "main", "upstream": "origin/main", "watcher": "restarted"},
        )
        self.assertEqual(
            git_provider.return_value.command.call_args_list,
            [
                call(root, "git", "fetch", "--quiet", "origin"),
                call(root, "git", "merge", "--ff-only", "@{upstream}"),
            ],
        )
        launchd.return_value.restart.assert_called_once_with(dashboard.WATCHER_LABEL)

    @patch("tools.engineering.dashboard.LaunchdProvider")
    @patch("tools.engineering.dashboard.GitProvider")
    def test_managed_branch_synchronization_refuses_local_commits(
        self, git_provider: object, launchd: object
    ) -> None:
        root = Path(__file__).parents[2]
        git_provider.return_value.execute.side_effect = [
            __import__("subprocess").CompletedProcess(("git",), 0, "", ""),
            __import__("subprocess").CompletedProcess(("git",), 0, "main\n", ""),
            __import__("subprocess").CompletedProcess(("git",), 0, "origin/main\n", ""),
            __import__("subprocess").CompletedProcess(("git",), 0, "0\t1\n", ""),
        ]

        with self.assertRaisesRegex(RuntimeError, "lokale commits"):
            dashboard._synchronize_managed_branch_with_upstream(root)
        git_provider.return_value.command.assert_called_once_with(root, "git", "fetch", "--quiet", "origin")
        launchd.return_value.restart.assert_not_called()

    @patch("tools.engineering.dashboard.LocalProcessProvider")
    @patch("tools.engineering.dashboard.shutil.which", return_value="/usr/sbin/lsof")
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

    @patch("tools.engineering.dashboard._workspace_git_lock", return_value={"state": "stale", "stale": True})
    def test_stale_workspace_git_lock_recovery_removes_only_confirmed_lock(self, lock_state: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / ".git" / "index.lock"
            lock.parent.mkdir()
            lock.write_text("", encoding="utf-8")
            self.assertEqual(
                dashboard._recover_stale_workspace_git_lock(root),
                {"state": "free", "recovered": True},
            )
            self.assertFalse(lock.exists())

    @patch("tools.engineering.dashboard._stale_local_branch_pull_request", return_value=None)
    @patch("tools.engineering.dashboard.GitProvider")
    def test_stale_local_branch_cleanup_removes_only_reviewed_patch_equivalent_branches(
        self, git_provider: object, _: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "main\n", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "0\t0\n", ""),
            completed(("git",), 0, "worktree /workspace\nHEAD a\nbranch refs/heads/main\n", ""),
            completed(("git",), 0, "codex/different\ncodex/remote\ncodex/stale\nmain\n", ""),
            completed(("git",), 1, "", ""),
            completed(("git",), 1, "", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 1, "", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "", ""),
        ]

        preview = dashboard._stale_local_branch_preview(root)
        self.assertEqual(preview, {"branches": [{"name": "codex/stale", "reason": "remote_absent_and_matches_main"}]})

        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "main\n", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "0\t0\n", ""),
            completed(("git",), 0, "worktree /workspace\nHEAD a\nbranch refs/heads/main\n", ""),
            completed(("git",), 0, "codex/different\ncodex/remote\ncodex/stale\nmain\n", ""),
            completed(("git",), 1, "", ""),
            completed(("git",), 1, "", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 1, "", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "", ""),
        ]
        self.assertEqual(
            dashboard._cleanup_stale_local_branches(root, ["codex/stale"]),
            {"removed": ["codex/stale"], "removed_count": 1},
        )
        self.assertEqual(
            git_provider.return_value.execute.call_args_list[-1],
            call(root, "git", "branch", "-D", "--", "codex/stale"),
        )

    @patch("tools.engineering.dashboard._stale_local_branch_pull_request", return_value=None)
    @patch("tools.engineering.dashboard.GitProvider")
    def test_stale_local_branch_preview_excludes_branches_used_by_active_worktrees(
        self, git_provider: object, _: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "main\n", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "0\t0\n", ""),
            completed(("git",), 0, "worktree /workspace\nHEAD a\nbranch refs/heads/main\n\nworktree /tmp/review\nHEAD b\nbranch refs/heads/codex/in-use\n", ""),
            completed(("git",), 0, "codex/in-use\ncodex/stale\nmain\n", ""),
            completed(("git",), 1, "", ""),
            completed(("git",), 0, "", ""),
        ]

        self.assertEqual(
            dashboard._stale_local_branch_preview(root),
            {"branches": [{"name": "codex/stale", "reason": "remote_absent_and_matches_main"}]},
        )
        self.assertNotIn(
            call(root, "git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/codex/in-use"),
            git_provider.return_value.execute.call_args_list,
        )

    @patch("tools.engineering.dashboard.GitHubProvider")
    @patch("tools.engineering.dashboard.GitProvider")
    def test_stale_local_branch_preview_adds_an_exact_merged_pull_request_link_when_available(
        self, git_provider: object, github_provider: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "main\n", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "0\t0\n", ""),
            completed(("git",), 0, "worktree /workspace\nHEAD a\nbranch refs/heads/main\n", ""),
            completed(("git",), 0, "codex/stale\nmain\n", ""),
            completed(("git",), 1, "", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "git@github.com:pcvantol/djconnect.git\n", ""),
        ]
        github_provider.return_value.github.return_value = json.dumps([
            {"number": 847, "url": "https://github.com/pcvantol/djconnect/pull/847", "headRefName": "codex/stale"},
        ])

        self.assertEqual(
            dashboard._stale_local_branch_preview(root),
            {"branches": [{
                "name": "codex/stale",
                "reason": "remote_absent_and_matches_main",
                "pull_request": {"number": 847, "url": "https://github.com/pcvantol/djconnect/pull/847"},
            }]},
        )

    @patch("tools.engineering.dashboard.GitProvider")
    def test_switch_to_fast_forward_main_only_switches_a_clean_branch_and_fast_forwards(
        self, git_provider: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "codex/work\n", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "2\t0\n", ""),
        ]

        self.assertEqual(
            dashboard._switch_to_fast_forward_main(root),
            {"previous_branch": "codex/work", "branch": "main", "synchronized": "true"},
        )
        self.assertEqual(
            git_provider.return_value.command.call_args_list,
            [
                call(root, "git", "switch", "main"),
                call(root, "git", "merge", "--ff-only", "origin/main"),
            ],
        )

    @patch("tools.engineering.dashboard.GitProvider")
    def test_switch_to_fast_forward_main_refuses_dirty_or_ahead_workspaces(self, git_provider: object) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, " M dashboard.py\n", ""),
            completed(("git",), 0, "codex/work\n", ""),
        ]
        with self.assertRaisesRegex(RuntimeError, "werkmap moet schoon"):
            dashboard._switch_to_fast_forward_main(root)

        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "main\n", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "0\t1\n", ""),
        ]
        with self.assertRaisesRegex(RuntimeError, "Lokale commits"):
            dashboard._switch_to_fast_forward_main(root)
        git_provider.return_value.command.assert_not_called()

    @patch("tools.engineering.dashboard.GitHubProvider")
    @patch("tools.engineering.dashboard.GitProvider")
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
        self.assertIn('id="workspaceBranchCleanup" type="button" hidden', page)

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

    @patch("tools.engineering.dashboard.GitHubProvider")
    @patch("tools.engineering.dashboard.GitProvider")
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

    @patch("tools.engineering.dashboard.GitHubProvider")
    @patch("tools.engineering.dashboard.GitProvider")
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

    @patch("tools.engineering.dashboard.LaunchdProvider")
    @patch("tools.engineering.dashboard.GitProvider")
    def test_restore_managed_main_branch_requires_a_clean_workspace_and_restarts_watcher(
        self, git_provider: object, launchd_provider: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess

        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "codex/feature", ""),
        ]
        self.assertEqual(
            dashboard._restore_managed_main_branch(root),
            {"previous_branch": "codex/feature", "branch": "main", "watcher": "restarted"},
        )
        git_provider.return_value.command.assert_called_once_with(root, "git", "switch", "main")
        launchd_provider.return_value.restart.assert_called_once_with(dashboard.WATCHER_LABEL)

        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "M dashboard.py\n", ""),
            completed(("git",), 0, "main", ""),
        ]
        with self.assertRaisesRegex(RuntimeError, "geen lokale wijzigingen"):
            dashboard._restore_managed_main_branch(root)

    @patch("tools.engineering.dashboard.LaunchdProvider")
    @patch("tools.engineering.dashboard.PlatformConfiguration.load")
    @patch("tools.engineering.dashboard.GitProvider")
    def test_synchronize_managed_branch_fast_forwards_only_a_clean_expected_branch(
        self, git_provider: object, configuration: object, launchd_provider: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        configuration.return_value.workspace.default_branch = "main"
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "main", ""),
            completed(("git",), 0, "origin/main", ""),
            completed(("git",), 0, "2 0", ""),
            completed(("git",), 0, "0\t0", ""),
        ]

        self.assertEqual(
            dashboard._synchronize_managed_branch_with_upstream(root),
            {"branch": "main", "upstream": "origin/main", "watcher": "restarted"},
        )
        self.assertEqual(
            git_provider.return_value.command.call_args_list,
            [
                call(root, "git", "fetch", "--quiet", "origin"),
                call(root, "git", "merge", "--ff-only", "@{upstream}"),
            ],
        )
        launchd_provider.return_value.restart.assert_called_once_with(dashboard.WATCHER_LABEL)

        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "codex/feature", ""),
            completed(("git",), 0, "origin/main", ""),
        ]
        with self.assertRaisesRegex(RuntimeError, "verwachte branch"):
            dashboard._synchronize_managed_branch_with_upstream(root)

    @patch("tools.engineering.dashboard.PlatformConfiguration.load")
    @patch("tools.engineering.dashboard.GitProvider")
    def test_switch_to_fast_forward_main_never_overwrites_workspace_or_local_commits(
        self, git_provider: object, configuration: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        configuration.return_value.workspace.default_branch = "main"
        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "codex/feature", ""),
            completed(("git",), 0, "", ""),
            completed(("git",), 0, "1 0", ""),
        ]
        self.assertEqual(
            dashboard._switch_to_fast_forward_main(root),
            {"previous_branch": "codex/feature", "branch": "main", "synchronized": "true"},
        )
        self.assertEqual(
            git_provider.return_value.command.call_args_list,
            [
                call(root, "git", "switch", "main"),
                call(root, "git", "merge", "--ff-only", "origin/main"),
            ],
        )

        git_provider.return_value.execute.side_effect = [
            completed(("git",), 0, "M dashboard.py\n", ""),
            completed(("git",), 0, "main", ""),
        ]
        with self.assertRaisesRegex(RuntimeError, "moet schoon zijn"):
            dashboard._switch_to_fast_forward_main(root)

    @patch("tools.engineering.dashboard.GitHubProvider")
    @patch("tools.engineering.dashboard.GitProvider")
    def test_stale_branch_pull_request_context_never_affects_cleanup_safety(
        self, git_provider: object, github_provider: object
    ) -> None:
        root = Path(__file__).parents[2]
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.return_value = completed(("git",), 1, "", "")
        self.assertIsNone(dashboard._stale_local_branch_pull_request(root, "codex/stale"))

        git_provider.return_value.execute.return_value = completed(
            ("git",), 0, "https://example.invalid/repository.git\n", ""
        )
        self.assertIsNone(dashboard._stale_local_branch_pull_request(root, "codex/stale"))

        git_provider.return_value.execute.return_value = completed(
            ("git",), 0, "git@github.com:pcvantol/djconnect.git\n", ""
        )
        github_provider.return_value.github.return_value = "not-json"
        self.assertIsNone(dashboard._stale_local_branch_pull_request(root, "codex/stale"))

    def test_rate_limit_helpers_cover_generic_windows_and_unavailable_provider_version(self) -> None:
        self.assertEqual(dashboard._rate_limit_window_label(1_440), "1-daags venster")
        self.assertEqual(dashboard._rate_limit_window_label(120), "2-uursvenster")
        self.assertEqual(dashboard._rate_limit_window_label(17), "17-minutenvenster")
        self.assertEqual(dashboard._normalize_rate_limits([]), {})
        self.assertEqual(dashboard._normalize_rate_limits({"rateLimits": []}), {})

        dashboard._codex_identity_cache = None
        with patch("tools.engineering.dashboard.codex_cli_executable", return_value=None):
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
            patch("tools.engineering.dashboard.shutil.which", side_effect=lambda name: f"/usr/local/bin/{name}"),
            patch("tools.engineering.dashboard.LocalProcessProvider.execute", side_effect=[
                completed(("codex", "--version"), 0, "codex-cli 0.149.0", ""),
                completed(("npm", "view"), 0, '"0.150.0"', ""),
            ]) as execute,
        ):
            self.assertEqual(
                dashboard._codex_cli_update_status(root, refresh=True),
                {"state": "update_available", "update_available": True, "current_version": "0.149.0", "latest_version": "0.150.0"},
            )
            self.assertEqual(execute.call_args_list[1].args[1], ("/usr/local/bin/npm", "view", "@openai/codex", "version", "--json"))

        dashboard._codex_identity_cache = None
        dashboard._codex_update_cache = None
        with (
            patch("tools.engineering.dashboard.shutil.which", side_effect=lambda name: f"/usr/local/bin/{name}"),
            patch("tools.engineering.dashboard.LocalProcessProvider.execute", side_effect=[
                completed(("codex", "--version"), 0, "codex-cli 0.149.0", ""),
                completed(("npm", "view"), 0, '"0.150.0"', ""),
                completed(("npm", "install"), 0, "installed", ""),
                completed(("codex", "--version"), 0, "codex-cli 0.150.0", ""),
            ]) as execute,
        ):
            self.assertEqual(dashboard._install_codex_cli_update(root), {"updated": True, "current_version": "0.150.0"})
            self.assertEqual(
                execute.call_args_list[2].args[1],
                ("/usr/local/bin/npm", "install", "--global", "--prefix", str(dashboard.engineering_platform_codex_cli_prefix()), "@openai/codex@0.150.0"),
            )
        dashboard._codex_identity_cache = None
        dashboard._codex_update_cache = None

    def test_codex_cli_update_installation_is_blocked_during_an_active_execution(self) -> None:
        with patch(
            "tools.engineering.dashboard._status",
            return_value=b'{"watcher_state":"ENGINEERING_RUN_ACTIVE","run_id":"inbox-active"}',
        ), patch("tools.engineering.dashboard._codex_cli_update_status") as check:
            with self.assertRaisesRegex(dashboard.CodexCliUpdateError, "codex_cli_update_execution_active"):
                dashboard._install_codex_cli_update(Path("/workspace"))
            check.assert_not_called()

    def test_codex_cli_update_reports_unavailable_current_and_failed_install_states(self) -> None:
        root = Path("/workspace")
        dashboard._codex_update_cache = None
        with (
            patch("tools.engineering.dashboard._codex_provider_identity", return_value={"provider_version": "not-a-version"}),
            patch("tools.engineering.dashboard._npm_executable", return_value=None),
        ):
            self.assertEqual(
                dashboard._codex_cli_update_status(root, refresh=True),
                {"state": "unavailable", "update_available": False},
            )

        with patch("tools.engineering.dashboard._execution_active", return_value=False), patch(
            "tools.engineering.dashboard._codex_cli_update_status",
            return_value={"state": "current", "update_available": False, "current_version": "0.150.0"},
        ):
            self.assertEqual(
                dashboard._install_codex_cli_update(root),
                {"updated": False, "current_version": "0.150.0"},
            )

        with patch("tools.engineering.dashboard._execution_active", return_value=False), patch(
            "tools.engineering.dashboard._codex_cli_update_status",
            return_value={"state": "update_available", "update_available": True, "latest_version": "0.150.0"},
        ), patch("tools.engineering.dashboard._npm_executable", return_value=None):
            with self.assertRaisesRegex(dashboard.CodexCliUpdateError, "codex_cli_update_unavailable"):
                dashboard._install_codex_cli_update(root)

        with patch("tools.engineering.dashboard._execution_active", return_value=False), patch(
            "tools.engineering.dashboard._codex_cli_update_status",
            return_value={"state": "update_available", "update_available": True, "latest_version": "0.150.0"},
        ), patch("tools.engineering.dashboard._npm_executable", return_value="/usr/local/bin/npm"), patch(
            "tools.engineering.dashboard.LocalProcessProvider.execute",
            return_value=__import__("subprocess").CompletedProcess(("npm",), 1, "", "npm error code EACCES: permission denied"),
        ):
            with self.assertRaisesRegex(dashboard.CodexCliUpdateError, "codex_cli_update_permissions_required"):
                dashboard._install_codex_cli_update(root)

    def test_component_processes_and_metrics_ignore_invalid_process_rows(self) -> None:
        self.assertEqual(dashboard._component_processes("unknown"), [])
        with patch("tools.engineering.dashboard.subprocess.run", side_effect=OSError):
            self.assertEqual(dashboard._component_processes("dashboard"), [])
        with patch("tools.engineering.dashboard.subprocess.run") as run:
            run.return_value = __import__("subprocess").CompletedProcess(
                ("ps",),
                0,
                "bad\n1 x 3 dashboard.py\n2 4 5 python -m tools.engineering.dashboard run\n3 12 01:05 python -m tools.engineering.dashboard run\n",
                "",
            )
            self.assertEqual(
                dashboard._component_processes("dashboard"),
                [
                    {"pid": 2, "memory_kib": 4, "uptime_seconds": 5},
                    {"pid": 3, "memory_kib": 12, "uptime_seconds": 65},
                ],
            )
        self.assertEqual(dashboard._process_elapsed_seconds("2-01:02:03"), 176_523)
        with tempfile.TemporaryDirectory() as temporary, patch("tools.engineering.dashboard.subprocess.run") as run:
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
            "tools.engineering.dashboard.Path.home", return_value=Path(temporary)
        ):
            details = dashboard._launch_agent_details("com.example.missing")
        self.assertFalse(details["loaded"])
        self.assertEqual(details["program_arguments"], [])
        with patch("tools.engineering.dashboard._platform_health", return_value={"components": {}}):
            with self.assertRaisesRegex(ValueError, "Onbekend Engineering Platform-onderdeel"):
                dashboard._component_details(Path("/missing"), "missing")

    def test_dashboard_launch_agent_discovers_its_module_from_the_selected_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.dashboard.Path.home", return_value=Path(temporary)
        ):
            repository = Path(temporary) / "project"
            repository.mkdir()
            rendered = dashboard.launch_agent(repository).read_text(encoding="utf-8")
        self.assertIn(f"cd {repository} &amp;&amp; exec", rendered)
        self.assertIn("-m tools.engineering.dashboard", rendered)
        self.assertIn(f"<key>WorkingDirectory</key><string>{repository}</string>", rendered)
        self.assertNotIn("-P", rendered)
        self.assertNotIn("PYTHONPATH", rendered)

    def test_local_dashboard_supervisor_preserves_private_and_resilient_boundaries(self) -> None:
        source = (Path(__file__).parents[2] / "tools/engineering/dashboard_supervisor.swift").read_text(encoding="utf-8")
        self.assertIn("tailscale", source)
        self.assertIn("SO_NOSIGPIPE", source)
        self.assertIn("Thread.sleep(forTimeInterval: 5)", source)
        self.assertNotIn("0.0.0.0", source)

    def test_dashboard_serves_assets_and_minimal_semantic_contract(self) -> None:
        page = _dashboard_html("Engineering Status").decode()

        self.assertIn("<title>Engineering Status</title>", page)
        self.assertIn('href="/assets/dashboard.css"', page)
        self.assertIn('src="/assets/dashboard.js" type="module"', page)
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

        root = Path("tools/engineering/assets")
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
        script = (Path(__file__).parents[2] / "tools" / "engineering" / "assets" / "dashboard.js").read_text()
        self.assertIn("function renderExecutionContext(context, execution = {})", script)
        self.assertIn('[t("field.repository"), execution.target_repository]', script)
        self.assertIn('[t("detail.target_checkout"), execution.checkout_path]', script)
        self.assertIn('[t("ui.active_branch"), execution.active_branch]', script)
        self.assertIn("renderExecutionContext(x.execution_context, x);", script)
        root = Path("tools/engineering/assets")
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
        self.assertNotIn('id="logSort"', (Path(__file__).parents[2] / "tools/engineering/dashboard.py").read_text(encoding="utf-8"))

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

    @patch("tools.engineering.dashboard.GitHubProvider")
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

    @patch("tools.engineering.dashboard.subprocess.run")
    @patch("tools.engineering.dashboard.shutil.which", return_value="/usr/local/bin/codex")
    @patch("tools.engineering.dashboard.codex_cli_executable", return_value="/usr/local/bin/codex")
    def test_codex_provider_identity_includes_the_resolved_cli_path(
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
                "provider_path": "/usr/local/bin/codex",
            },
        )
        dashboard._codex_identity_cache = None

    def test_codex_cli_installation_path_uses_the_managed_prefix(self) -> None:
        managed_prefix = dashboard.engineering_platform_codex_cli_prefix()
        self.assertEqual(
            _codex_cli_installation_path(str(managed_prefix / "bin" / "codex")),
            str(managed_prefix),
        )

    @patch("tools.engineering.dashboard.shutil.which", return_value=None)
    def test_codex_cli_installation_path_is_unavailable_when_no_executable_resolves(
        self, _: object
    ) -> None:
        self.assertIsNone(_codex_cli_installation_path("codex"))

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
            patch("tools.engineering.dashboard.subprocess.Popen", return_value=process),
            patch("tools.engineering.dashboard.select.select", return_value=([process.stdout], [], [])),
            patch(
                "tools.engineering.dashboard._codex_provider_identity",
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
            patch("tools.engineering.dashboard.subprocess.Popen", return_value=process),
            patch(
                "tools.engineering.dashboard._codex_provider_identity",
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
            patch("tools.engineering.dashboard.subprocess.Popen", side_effect=OSError),
            patch(
                "tools.engineering.dashboard._codex_provider_identity",
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
            patch("tools.engineering.dashboard.subprocess.Popen", return_value=process),
            patch("tools.engineering.dashboard.select.select", return_value=([process.stdout], [], [])),
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

    def test_engineering_database_details_are_read_only_and_report_the_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = dashboard._engineering_database_details(root)
            self.assertEqual(
                missing["path"], str((root / ".engineering" / "engineering.db").resolve())
            )
            self.assertEqual(missing["size"], "Niet beschikbaar")
            self.assertEqual(missing["schema_version"], "Niet beschikbaar")
            self.assertFalse((root / ".engineering").exists())

            with open_storage(root) as connection:
                connection.execute("SELECT 1")
            details = dashboard._engineering_database_details(root)

        self.assertRegex(details["size"], r"^\d+,\d{2} MB$")
        self.assertNotEqual(details["size"], "0,00 MB")
        self.assertEqual(details["schema_version"], str(ENGINEERING_STORAGE_SCHEMA_VERSION))

    def test_engineering_database_snapshot_is_a_consistent_read_only_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                connection.execute("CREATE TABLE backup_probe (value TEXT)")
                connection.execute("INSERT INTO backup_probe VALUES ('preserved')")

            snapshot = dashboard._engineering_database_snapshot(root)

        self.assertIsNotNone(snapshot)
        with tempfile.NamedTemporaryFile(suffix=".db") as backup:
            backup.write(snapshot or b"")
            backup.flush()
            with sqlite3.connect(backup.name) as connection:
                self.assertEqual(connection.execute("SELECT value FROM backup_probe").fetchone(), ("preserved",))

    @patch("tools.engineering.dashboard.subprocess.run")
    def test_tracked_file_count_counts_recursive_git_index_entries(self, run: object) -> None:
        run.return_value = __import__("subprocess").CompletedProcess(
            ("git",), 0, b"README.md\0docs/guide.md\0tools/engineering/dashboard.py\0", b""
        )
        self.assertEqual(_tracked_file_count(Path("/workspace")), "3")

    @patch("tools.engineering.dashboard._codex_rate_limits", return_value=b"{}")
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
        self.assertEqual(
            snapshot["component_log_versions"],
            {"inbox": "sqlite:0:0", "dashboard": "sqlite:0:0"},
        )
        self.assertEqual(snapshot["component_versions"]["dashboard"], DASHBOARD_VERSION)
        self.assertEqual(snapshot["component_versions"]["worker"], WATCHER_VERSION)
        self.assertEqual(snapshot["workspace_git_lock"], {"state": "free", "active": False, "stale": False})
        self.assertEqual(snapshot["workspace_git"]["branch"], "Niet beschikbaar")
        self.assertIn("workspace_worktrees", snapshot)

    def test_latest_codex_log_is_local_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / ".engineering" / "logs" / "codex"
            logs.mkdir(parents=True)
            (logs / "run.log").write_text("redacted diagnostic", encoding="utf-8")
            self.assertEqual(_latest_codex_log(Path(temporary)), b"redacted diagnostic")

    @patch("tools.engineering.dashboard.subprocess.run")
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
            from tools.engineering.prompt_history import record_prompt_execution
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

    @patch("tools.engineering.dashboard.GitHubProvider")
    @patch("tools.engineering.dashboard.GitProvider")
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

            payload = json.loads(_prompt_history_detail(root, run_id))

            self.assertEqual(
                payload["history"]["execution_diagnostic"],
                "Pre-flight is NO-GO: rolling status records are stale.",
            )

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
        with patch("tools.engineering.dashboard._canonical_checkpoint", return_value=checkpoint):
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

    def test_component_log_is_read_from_canonical_sqlite_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES('inbox','{\"event\":\"first\"}','now')"
                )
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES('inbox','{\"event\":\"second\"}','now')"
                )
            self.assertIn(b'"event":"first"', _component_log(root, "inbox"))
            self.assertIn(b'"event":"second"', _component_log(root, "inbox"))
            self.assertEqual(_component_log(root, "unknown"), b"")

    def test_component_log_clear_is_limited_to_the_requested_known_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES('inbox','inbox event','now')"
                )
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES('dashboard','dashboard event','now')"
                )

            _clear_component_log(root, "inbox")

            self.assertNotIn(b"inbox event", _component_log(root, "inbox"))
            self.assertIn(b"dashboard event", _component_log(root, "dashboard"))
            with self.assertRaises(ValueError):
                _clear_component_log(root, "../outside")

    def test_component_log_versions_change_when_component_log_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root):
                pass
            self.assertEqual(
                _component_log_versions(root),
                {"inbox": "sqlite:0:0", "dashboard": "sqlite:0:0"},
            )
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES('inbox','one','now')"
                )
            self.assertEqual(_component_log_versions(root)["inbox"], "sqlite:1:1")

    @patch("tools.engineering.dashboard._component_uptime_seconds", side_effect=(3661, 122))
    @patch("tools.engineering.dashboard._launch_agent_health")
    def test_platform_health_reports_each_visible_component(
        self, launch_agent_health: object, component_uptime: object
    ) -> None:
        launch_agent_health.return_value = {
            "healthy": True,
            "state": "running",
            "detail": "LaunchAgent is geladen",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            health = _platform_health(root)

        self.assertEqual(health["health"], "ok")
        self.assertTrue(health["healthy"])
        self.assertEqual(set(health["components"]), {
            "dashboard",
            "inbox_watcher",
            "dashboard_relay",
        })
        self.assertIsInstance(health["components"]["dashboard"]["uptime_seconds"], int)
        self.assertEqual(health["components"]["inbox_watcher"]["uptime_seconds"], 3661)
        self.assertEqual(health["components"]["dashboard_relay"]["uptime_seconds"], 122)
        component_uptime.assert_called()

    @patch("tools.engineering.dashboard._component_processes")
    def test_component_uptime_uses_the_longest_owned_process_lifetime(self, processes: object) -> None:
        processes.return_value = [
            {"pid": 10, "memory_kib": 100, "uptime_seconds": 20},
            {"pid": 11, "memory_kib": 100, "uptime_seconds": 90},
        ]
        self.assertEqual(_component_uptime_seconds("inbox_watcher"), 90)
        processes.return_value = []
        self.assertIsNone(_component_uptime_seconds("inbox_watcher"))

    def test_dashboard_binds_only_loopback_and_delegates_tailnet_ingress_to_relay(self) -> None:
        self.assertEqual(binding_addresses(), (LOOPBACK_ADDRESS,))

    def test_prompt_history_projection_is_private_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = json.loads(_prompt_history(root))
        self.assertEqual(payload, {"runs": []})

    def test_prompt_history_and_detail_fail_closed_when_evidence_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("tools.engineering.dashboard.prompt_history", side_effect=OSError("unavailable")):
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
            with patch("tools.engineering.dashboard.open_storage", return_value=connection):
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

    def test_dashboard_process_and_component_projections_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "current.json").write_text('{"run_id":"inbox-owned"}', encoding="utf-8")
            (status / "runner_process.json").write_text(
                '{"run_id":"inbox-owned","pid":9,"process_group":8}', encoding="utf-8"
            )
            with patch("tools.engineering.dashboard.subprocess.run") as run:
                run.return_value = __import__("subprocess").CompletedProcess(
                    ("ps",), 0, "malformed\n10 8 invalid worker\n", ""
                )
                self.assertEqual(json.loads(_codex_process_metrics(root))["process_count"], 0)
            with patch("tools.engineering.dashboard._platform_health", return_value={"components": {"dashboard_relay": {}}}):
                details = dashboard._component_details(root, "dashboard_relay")
            self.assertEqual(details["launchd"]["label"], dashboard.RELAY_LABEL)
            self.assertEqual(dashboard._process_elapsed_seconds("1:02"), 62)
            with self.assertRaises(ValueError):
                dashboard._process_elapsed_seconds("1:2:3:4")

    @contextmanager
    def _dashboard_http_connection(self):
        root = Path(__file__).parents[2]
        server = dashboard.DashboardHTTPServer((LOOPBACK_ADDRESS, 0), dashboard.handler(root))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # The dashboard landing page reads the current Git and pull-request
        # state.  On a busy CI runner that legitimate local work can exceed
        # the short socket timeout, which made this integration check flaky.
        connection = HTTPConnection(LOOPBACK_ADDRESS, server.server_port, timeout=10)
        try:
            yield root, connection
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @patch("tools.engineering.dashboard._workspace_open_pull_requests", return_value=[])
    def test_http_dashboard_status_routes(self, _open_pull_requests: object) -> None:
        with self._dashboard_http_connection() as (_, connection):
            for route, content_type in (
                ("/", "text/html"),
                ("/assets/operations-console/apple-touch-icon-dark.png", "image/png"),
                ("/assets/operations-console/apple-touch-icon-light.png", "image/png"),
                ("/favicon.ico", "image/png"),
                ("/apple-touch-icon.png", "image/png"),
                ("/apple-touch-icon-precomposed.png", "image/png"),
                ("/api/status", "application/json"),
                ("/api/build", "application/json"),
                ("/api/health", "application/json"),
                ("/api/components/dashboard/details", "application/json"),
                ("/api/process-metrics", "application/json"),
                ("/api/usage", "application/json"),
                ("/api/commits", "application/json"),
                ("/api/prompt-started", "application/json"),
                ("/api/log/latest", "text/plain"),
                ("/api/logs/inbox", "text/plain"),
                ("/api/logs/dashboard", "text/plain"),
                ("/api/log/current", "text/plain"),
                ("/api/report/latest", "text/markdown"),
            ):
                connection.request("GET", route)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.getheader("Content-Type"))
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                response.read()

    @patch("tools.engineering.dashboard._request_owner_authorization", return_value={"queued": True, "pull_request": 940})
    @patch("tools.engineering.dashboard._workspace_open_pull_requests", return_value=[])
    def test_http_owner_authorization_dispatch_uses_only_the_pull_request_number(
        self, _open_pull_requests: object, request_authorization: object
    ) -> None:
        with self._dashboard_http_connection() as (root, connection):
            connection.request(
                "POST", "/api/open-pull-requests/940/owner-authorization",
                body=b"{}", headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 202)
            self.assertEqual(json.loads(response.read()), {"queued": True, "pull_request": 940})
        request_authorization.assert_called_once_with(root, 940)

    def test_http_configuration_routes_preserve_the_server_side_safety_contract(self) -> None:
        """Exercise successful configuration changes and their guarded counterparts."""
        event = {"key": "log_retention_days", "previous": 90, "value": 30}
        runtime = MagicMock()
        runtime.resolve_runtime_prompt_transport.return_value.inbox = Path("/private/inbox")
        configuration = MagicMock()
        configuration.resolver.return_value = runtime
        with self._dashboard_http_connection() as (root, connection), patch(
            "tools.engineering.dashboard.update_dashboard_configuration", return_value=event
        ) as update, patch(
            "tools.engineering.dashboard.prune_component_logs"
        ) as prune, patch(
            "tools.engineering.dashboard.prune_telemetry"
        ) as prune_telemetry, patch(
            "tools.engineering.dashboard.log_event"
        ), patch(
            "tools.engineering.dashboard.PlatformConfiguration.load", return_value=configuration
        ), patch(
            "tools.engineering.dashboard._execution_active", return_value=False
        ), patch(
            "tools.engineering.dashboard._inbox_has_items", return_value=False
        ), patch(
            "tools.engineering.dashboard._change_inbox_location", return_value={
                "key": "inbox_root", "previous": "/old", "value": "/new", "watcher_verified": True,
            }
        ) as change_inbox, patch(
            "tools.engineering.dashboard._choose_local_directory", return_value="/private/chosen"
        ):
            connection.request(
                "POST", "/api/configuration",
                body=json.dumps({"key": "log_retention_days", "value": 30, "previous": 90}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), event)
            update.assert_called_once_with(root, "log_retention_days", 30, expected_previous=90)
            prune.assert_called_once_with(root, 30)

            telemetry_event = {"key": "telemetry_retention_days", "previous": 90, "value": 60}
            update.reset_mock()
            update.return_value = telemetry_event
            connection.request(
                "POST", "/api/configuration",
                body=json.dumps({"key": "telemetry_retention_days", "value": 60, "previous": 90}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), telemetry_event)
            update.assert_called_once_with(root, "telemetry_retention_days", 60, expected_previous=90)
            prune_telemetry.assert_called_once_with(root, 60)

            connection.request(
                "POST", "/api/configuration/inbox-location",
                body=json.dumps({"inbox_root": "/private/new"}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["value"], "/new")
            change_inbox.assert_called_once_with(root, "/private/new", Path("/private/inbox"))

            connection.request(
                "POST", "/api/configuration/inbox-location/browse", body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"cancelled": False, "value": "/private/chosen"})

            connection.request(
                "POST", "/api/configuration", body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read()), {"error": "Ongeldige dashboardinstelling."})

    def test_http_configuration_refuses_unsafe_codex_capacity_reserve_increases(self) -> None:
        settings = {"codex_capacity_reserve_percent": 0}

        def update_reserve(_root: Path, key: str, value: int, *, expected_previous: int) -> dict[str, object]:
            self.assertEqual(key, "codex_capacity_reserve_percent")
            self.assertEqual(expected_previous, settings[key])
            previous = settings[key]
            settings[key] = value
            return {"key": key, "previous": previous, "value": value}

        with self._dashboard_http_connection() as (_, connection), patch(
            "tools.engineering.dashboard.dashboard_configuration", return_value=settings,
        ), patch("tools.engineering.dashboard.update_dashboard_configuration", side_effect=update_reserve):
            with patch("tools.engineering.dashboard.read_remaining_percent", return_value=47):
                connection.request(
                    "POST", "/api/configuration",
                    body=json.dumps({"key": "codex_capacity_reserve_percent", "value": 50, "previous": 0}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {
                    "error_code": "codex_capacity_reserve_exceeds_remaining",
                    "value": 0,
                    "remaining_percent": 47,
                })

                connection.request(
                    "POST", "/api/configuration",
                    body=json.dumps({"key": "codex_capacity_reserve_percent", "value": 25, "previous": 0}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["value"], 25)

            with patch("tools.engineering.dashboard.read_remaining_percent", return_value=None):
                connection.request(
                    "POST", "/api/configuration",
                    body=json.dumps({"key": "codex_capacity_reserve_percent", "value": 75, "previous": 25}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {
                    "error_code": "codex_capacity_reserve_unavailable",
                    "value": 25,
                    "remaining_percent": None,
                })

                connection.request(
                    "POST", "/api/configuration",
                    body=json.dumps({"key": "codex_capacity_reserve_percent", "value": 20, "previous": 25}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["value"], 20)

    def test_inbox_location_change_requires_restart_and_route_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old", root / "new"
            (old / "Inbox").mkdir(parents=True)
            (new / "Inbox").mkdir(parents=True)
            update_inbox_root(root, str(old))

            with patch("tools.engineering.dashboard._restart_and_verify_inbox_watcher") as restart:
                event = dashboard._change_inbox_location(root, str(new), old / "Inbox")

            self.assertTrue(event["watcher_verified"])
            self.assertEqual(inbox_root(root), new.resolve())
            restart.assert_called_once_with(root, (new / "Inbox").resolve())

    def test_inbox_location_restart_or_route_verification_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old", root / "new"
            (old / "Inbox").mkdir(parents=True)
            (new / "Inbox").mkdir(parents=True)
            update_inbox_root(root, str(old))

            with patch(
                "tools.engineering.dashboard._restart_and_verify_inbox_watcher",
                side_effect=(OSError("restart failed"), None),
            ) as restart:
                with self.assertRaises(dashboard.InboxLocationChangeError):
                    dashboard._change_inbox_location(root, str(new), old / "Inbox")

            self.assertEqual(inbox_root(root), old.resolve())
            self.assertEqual(
                restart.call_args_list,
                [call(root, (new / "Inbox").resolve()), call(root, old / "Inbox")],
            )

    def test_inbox_location_route_verification_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old", root / "new"
            (old / "Inbox").mkdir(parents=True)
            (new / "Inbox").mkdir(parents=True)
            update_inbox_root(root, str(old))

            with patch(
                "tools.engineering.dashboard._restart_and_verify_inbox_watcher",
                side_effect=(OSError("new watcher reported a different route"), None),
            ):
                with self.assertRaises(dashboard.InboxLocationChangeError):
                    dashboard._change_inbox_location(root, str(new), old / "Inbox")

            self.assertEqual(inbox_root(root), old.resolve())

    def test_inbox_location_http_never_acknowledges_unconfirmed_watcher(self) -> None:
        runtime = MagicMock()
        runtime.resolve_runtime_prompt_transport.return_value.inbox = Path("/private/old/Inbox")
        configuration = MagicMock()
        configuration.resolver.return_value = runtime
        with self._dashboard_http_connection() as (_, connection), patch(
            "tools.engineering.dashboard._execution_active", return_value=False
        ), patch(
            "tools.engineering.dashboard.PlatformConfiguration.load", return_value=configuration
        ), patch(
            "tools.engineering.dashboard._inbox_has_items", return_value=False
        ), patch(
            "tools.engineering.dashboard._change_inbox_location",
            side_effect=dashboard.InboxLocationChangeError("watcher_restart_failed_rolled_back"),
        ):
            connection.request(
                "POST", "/api/configuration/inbox-location", body=json.dumps({"inbox_root": "/private/new"}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.read()), {"error_code": "inbox_watcher_restart_failed"})

    def test_http_configuration_refuses_inbox_changes_while_work_is_active_or_queued(self) -> None:
        payload = json.dumps({"inbox_root": "/private/new"})
        with self._dashboard_http_connection() as (_, connection), patch(
            "tools.engineering.dashboard._execution_active", return_value=True
        ):
            connection.request("POST", "/api/configuration/inbox-location", body=payload,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            self.assertIn("geen uitvoering actief", json.loads(response.read())["error"])

        runtime = MagicMock()
        runtime.resolve_runtime_prompt_transport.return_value.inbox = Path("/private/inbox")
        configuration = MagicMock()
        configuration.resolver.return_value = runtime
        with self._dashboard_http_connection() as (_, connection), patch(
            "tools.engineering.dashboard._execution_active", return_value=False
        ), patch(
            "tools.engineering.dashboard.PlatformConfiguration.load", return_value=configuration
        ), patch(
            "tools.engineering.dashboard._inbox_has_items", return_value=True
        ):
            connection.request("POST", "/api/configuration/inbox-location", body=payload,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            self.assertEqual(json.loads(response.read()), {"error_code": "inbox_not_empty"})

    def test_http_operational_controls_dispatch_only_their_validated_payloads(self) -> None:
        """Keep the non-destructive control endpoints covered as stable API contracts."""
        requests = (
            ("/api/managed-branch-recovery", {}, 202),
            ("/api/managed-branch-synchronization", {}, 202),
            ("/api/stale-git-lock-recovery", {}, 202),
            ("/api/stale-local-branch-cleanup", {"branches": ["codex/merged"]}, 202),
            ("/api/workspace-switch-to-main", {}, 202),
            ("/api/stale-local-branch-cleanup-preview", {}, 200),
            ("/api/execution-retry", {"run_id": "run-1"}, 202),
            ("/api/status-reconciliation-preview", {"run_id": "run-1"}, 200),
            ("/api/status-reconciliation", {"run_id": "run-1"}, 202),
            ("/api/execution-dismiss", {"run_id": "run-1"}, 202),
            ("/api/execution-merge-wait-abort", {"run_id": "run-1"}, 202),
            ("/api/execution-emergency-rollback", {"run_id": "run-1"}, 202),
            ("/api/execution-merge-status-check", {"run_id": "run-1"}, 202),
            ("/api/queue-defer", {"filename": "queued.md"}, 202),
            ("/api/audit/user-action", {"action": "chat_downloaded"}, 200),
        )
        with ExitStack() as patches:
            patches.enter_context(patch("tools.engineering.dashboard.log_event"))
            patches.enter_context(patch("tools.engineering.dashboard._restore_managed_main_branch", return_value={"previous_branch": "feature", "branch": "main", "watcher": "restarted"}))
            patches.enter_context(patch("tools.engineering.dashboard._synchronize_managed_branch_with_upstream", return_value={"branch": "main", "upstream": "origin/main", "watcher": "restarted"}))
            patches.enter_context(patch("tools.engineering.dashboard._recover_stale_workspace_git_lock", return_value={"state": "free", "recovered": True}))
            patches.enter_context(patch("tools.engineering.dashboard._cleanup_stale_local_branches", return_value={"removed_count": 1}))
            patches.enter_context(patch("tools.engineering.dashboard._switch_to_fast_forward_main", return_value={"previous_branch": "feature", "branch": "main", "synchronized": "true"}))
            patches.enter_context(patch("tools.engineering.dashboard._stale_local_branch_preview", return_value={"branches": []}))
            patches.enter_context(patch("tools.engineering.dashboard.retry_admission_preflight"))
            patches.enter_context(patch("tools.engineering.dashboard.submit_execution_retry", return_value={"retry_run_id": "run-2"}))
            patches.enter_context(patch("tools.engineering.dashboard.status_reconciliation_preview", return_value={"run_id": "run-1"}))
            patches.enter_context(patch("tools.engineering.dashboard.submit_status_reconciliation", return_value={"run_id": "run-1"}))
            patches.enter_context(patch("tools.engineering.dashboard.dismiss_execution", return_value={"run_id": "run-1"}))
            patches.enter_context(patch("tools.engineering.dashboard.abort_operator_merge_wait", return_value={"run_id": "run-1"}))
            patches.enter_context(patch("tools.engineering.dashboard.execute_emergency_recovery", return_value={"run_id": "run-1"}))
            patches.enter_context(patch("tools.engineering.dashboard.check_operator_merge_status", return_value={"verified": True}))
            patches.enter_context(patch("tools.engineering.dashboard.defer_queued_prompt", return_value={"filename": "queued.md", "deferred_filename": "queued.deferred.md"}))
            patches.enter_context(patch("tools.engineering.dashboard.cloud_root", return_value=Path("/private/cloud")))
            with self._dashboard_http_connection() as (_, connection):
                for path, payload, expected_status in requests:
                    connection.request("POST", path, body=json.dumps(payload),
                                       headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected_status, path)
                    response.read()

    @patch("tools.engineering.dashboard._workspace_open_pull_requests", return_value=[])
    def test_dashboard_document_reresolves_the_saved_inbox_location(
        self, _open_pull_requests: object
    ) -> None:
        first = Path("/private/engineering/first")
        second = Path("/private/engineering/second")
        with patch(
            "tools.engineering.platform_api.configured_inbox_root", return_value=first
        ) as configured_inbox:
            with self._dashboard_http_connection() as (_, connection):
                connection.request("GET", "/")
                first_page = connection.getresponse().read().decode("utf-8")
                self.assertIn(f"{first}/Inbox", first_page)

                configured_inbox.return_value = second
                connection.request("GET", "/")
                second_page = connection.getresponse().read().decode("utf-8")
                self.assertIn(f"{second}/Inbox", second_page)
                self.assertNotIn(f"{first}/Inbox", second_page)

    @patch("tools.engineering.dashboard._workspace_open_pull_requests", return_value=None)
    def test_http_open_pull_requests_keeps_unknown_github_state_distinct_from_an_empty_list(
        self, _open_pull_requests: object
    ) -> None:
        with self._dashboard_http_connection() as (_, connection):
            connection.request("GET", "/api/open-pull-requests")
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertEqual(
                json.loads(response.read()),
                {"error": "GitHub pull-requeststatus is tijdelijk niet beschikbaar."},
            )

    def test_http_read_only_evidence_and_telemetry_routes_keep_response_contracts(self) -> None:
        with ExitStack() as patches:
            patches.enter_context(patch("tools.engineering.dashboard._prompt_history_detail", return_value=b'{"run_id":"run-1"}'))
            patches.enter_context(patch("tools.engineering.dashboard.report_for_prompt_history", return_value=b"# report\n"))
            patches.enter_context(patch("tools.engineering.dashboard._report_analysis_for_run", return_value=b"# analysis\n"))
            patches.enter_context(patch("tools.engineering.dashboard._github_rate_limit_status", return_value={"state": "ok"}))
            patches.enter_context(patch("tools.engineering.dashboard._codex_cli_update_status", return_value={"state": "current"}))
            patches.enter_context(patch("tools.engineering.dashboard.daily_timing_detail", return_value={"date": "2026-08-25"}))
            patches.enter_context(patch("tools.engineering.dashboard._component_details", return_value={"component": "dashboard"}))
            patches.enter_context(patch("tools.engineering.dashboard._engineering_database_snapshot", return_value=b"SQLite format 3\x00backup"))
            patches.enter_context(patch("tools.engineering.dashboard.log_event"))
            with self._dashboard_http_connection() as (_, connection):
                for route, content_type in (
                    ("/api/prompt-history/run-1/details", "application/json"),
                    ("/api/prompt-history/run-1/report?audit=download", "text/markdown"),
                    ("/api/prompt-history/run-1/analysis?audit=download", "text/markdown"),
                    ("/api/github-rate-limit", "application/json"),
                    ("/api/codex-cli-update", "application/json"),
                    ("/api/telemetry/2026-08-25", "application/json"),
                    ("/api/components/dashboard/details", "application/json"),
                    ("/api/engineering-database/download?audit=download", "application/vnd.sqlite3"),
                ):
                    connection.request("GET", route)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200, route)
                    self.assertIn(content_type, response.getheader("Content-Type"))
                    if route.startswith("/api/engineering-database/"):
                        self.assertRegex(response.getheader("Content-Disposition") or "", r'^attachment; filename="engineering-database-backup-\d{8}T\d{6}Z\.db"$')
                    response.read()

    def test_http_read_only_evidence_routes_return_explicit_not_found_or_invalid_responses(self) -> None:
        with self._dashboard_http_connection() as (_, connection), patch(
            "tools.engineering.dashboard._prompt_history_detail", return_value=b""
        ), patch(
            "tools.engineering.dashboard.report_for_prompt_history", return_value=None
        ), patch(
            "tools.engineering.dashboard._report_analysis_for_run", return_value=b""
        ), patch(
            "tools.engineering.dashboard._engineering_database_snapshot", return_value=None
        ), patch(
            "tools.engineering.dashboard.daily_timing_detail", side_effect=ValueError("bad date")
        ), patch(
            "tools.engineering.dashboard._component_details", side_effect=ValueError("unknown")
        ):
            for route, expected_status in (
                ("/api/prompt-history/missing/details", 404),
                ("/api/prompt-history/missing/report", 404),
                ("/api/prompt-history/missing/analysis", 404),
                ("/api/telemetry/nope", 400),
                ("/api/components/nope/details", 404),
                ("/api/engineering-database/download", 404),
            ):
                connection.request("GET", route)
                response = connection.getresponse()
                self.assertEqual(response.status, expected_status, route)
                response.read()

    def test_http_codex_cli_update_routes_only_install_after_post(self) -> None:
        with self._dashboard_http_connection() as (_, connection), patch(
            "tools.engineering.dashboard._codex_cli_update_status",
            return_value={"state": "update_available", "update_available": True, "current_version": "0.149.0", "latest_version": "0.150.0"},
        ) as check, patch(
            "tools.engineering.dashboard._install_codex_cli_update",
            return_value={"updated": True, "current_version": "0.150.0"},
        ) as install:
            connection.request("GET", "/api/codex-cli-update")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["latest_version"], "0.150.0")
            check.assert_called_once()
            install.assert_not_called()
            connection.request("POST", "/api/codex-cli-update", body="{}", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"updated": True, "current_version": "0.150.0"})
            install.assert_called_once()
            with patch("tools.engineering.dashboard._codex_rate_limits", return_value=b"{}"):
                connection.request("GET", "/api/dashboard-snapshot")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("application/json", response.getheader("Content-Type"))
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                snapshot = json.loads(response.read())
                self.assertIn("status", snapshot)
                self.assertIn("rate_limits", snapshot)
            with patch(
                "tools.engineering.dashboard._platform_health",
                return_value={"health": "ok", "healthy": True, "components": {}},
            ):
                connection.request("GET", "/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["health"], "ok")
            connection.request("GET", "/missing")
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()

    def test_http_codex_cli_update_rejects_invalid_requests_and_reports_update_errors(self) -> None:
        with self._dashboard_http_connection() as (_, connection):
            connection.request("POST", "/api/codex-cli-update", body="[]", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read()), {"error": "invalid_request"})

            with patch(
                "tools.engineering.dashboard._install_codex_cli_update",
                side_effect=dashboard.CodexCliUpdateError("codex_cli_update_execution_active"),
            ):
                connection.request("POST", "/api/codex-cli-update", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(
                    json.loads(response.read()),
                    {"error": "codex_cli_update_execution_active"},
                )

    def test_http_dashboard_history_routes(self) -> None:
        with self._dashboard_http_connection() as (_, connection):
            connection.request("GET", "/api/prompt-history")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("application/json", response.getheader("Content-Type"))
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            response.read()
            for route in (
                "/api/report/last-executed?run_id=invalid",
                "/api/report-analysis/last-executed?run_id=invalid",
                "/api/usage/last-executed?run_id=invalid",
                "/api/commits/last-executed",
                "/api/log/last",
            ):
                connection.request("GET", route)
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()

    def test_http_dashboard_operator_routes(self) -> None:
        with self._dashboard_http_connection() as (root, connection):
            with (
                patch("tools.engineering.dashboard.clear_telemetry", return_value={"execution_runs": 3, "daily_statistics": 2}) as clear_telemetry,
                patch("tools.engineering.dashboard.log_event"),
            ):
                connection.request("POST", "/api/telemetry/clear", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read()),
                    {"cleared": True, "execution_runs": 3, "daily_statistics": 2},
                )
                clear_telemetry.assert_called_once_with(root)
            connection.request("POST", "/api/telemetry/clear", body="[]", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            with (
                patch("tools.engineering.dashboard._clear_component_log") as clear_log,
                patch("tools.engineering.dashboard.log_event"),
            ):
                connection.request("POST", "/api/logs/inbox", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"cleared": "inbox"})
                clear_log.assert_called_once_with(root, "inbox")
            for route, body in (("/api/logs/not-a-component", "{}"), ("/api/logs/inbox", "[]")):
                connection.request("POST", route, body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            with (
                patch("tools.engineering.dashboard.Timer") as timer,
                patch("tools.engineering.dashboard.component_lifecycle_context", return_value={"git_commit": "abc"}),
                patch("tools.engineering.dashboard.log_event") as log_event,
            ):
                connection.request("POST", "/api/components/inbox_watcher/restart", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), {"restarting": "inbox_watcher"})
                timer.assert_called_once()
                timer.return_value.start.assert_called_once()
                restart_event = next(call for call in log_event.call_args_list if call.args[2] == "component_restart_trigger_received")
                self.assertEqual(restart_event.kwargs["context"]["target_component"], "inbox_watcher")
            connection.request("POST", "/api/components/unknown_component/restart", body="{}", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            with (
                patch("tools.engineering.dashboard._consume_codex_rate_limit_reset_credit", return_value="reset"),
                patch("tools.engineering.dashboard._codex_rate_limits", return_value=b'{"reset_credits":1}'),
                patch("tools.engineering.dashboard.log_event") as reset_log_event,
            ):
                connection.request("POST", "/api/rate-limit-reset", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"outcome": "reset", "rate_limits": {"reset_credits": 1}})
                reset_log_event.assert_any_call(ANY, logging.INFO, "ai_usage_reset_completed", diagnostic="outcome=reset")
            for outcome in ("nothingToReset", "noCredit", "alreadyRedeemed"):
                with (
                    patch("tools.engineering.dashboard._consume_codex_rate_limit_reset_credit", return_value=outcome),
                    patch("tools.engineering.dashboard._codex_rate_limits", return_value=b'{"reset_credits":1}'),
                    patch("tools.engineering.dashboard.log_event") as reset_log_event,
                ):
                    connection.request("POST", "/api/rate-limit-reset", body="{}", headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 409)
                    self.assertEqual(json.loads(response.read()), {"outcome": outcome, "rate_limits": {"reset_credits": 1}})
                    reset_log_event.assert_any_call(ANY, logging.INFO, "ai_usage_reset_not_consumed", diagnostic=f"outcome={outcome}")
            with patch("tools.engineering.dashboard.log_event") as audit_log_event:
                connection.request("POST", "/api/audit/user-action", body='{"action":"chat_downloaded"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"logged": True})
                audit_log_event.assert_any_call(ANY, logging.INFO, "chat_downloaded")
            dashboard.update_dashboard_configuration(root, "log_level", "INFO")
            connection.request("POST", "/api/configuration", body='{"key":"log_level","value":"DEBUG","previous":"INFO"}', headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["value"], "DEBUG")
            connection.request("POST", "/api/configuration", body='{"key":"log_level","value":"INFO","previous":"DEBUG"}', headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.request("POST", "/api/configuration", body='{"key":"log_level","value":"DEBUG","previous":"DEBUG"}', headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            self.assertEqual(json.loads(response.read())["value"], "INFO")
            connection.request("POST", "/api/configuration", body='{"key":"unknown","value":1,"previous":1}', headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            transport = root / "configured-transport"
            (transport / "Inbox").mkdir(parents=True, exist_ok=True)
            with (
                patch(
                    "tools.engineering.dashboard._status",
                    return_value=b'{"watcher_state":"WATCHER_IDLE","run_id":null,"current_phase":"COMPLETE"}',
                ),
                patch("tools.engineering.dashboard._inbox_has_items", return_value=False),
                patch(
                    "tools.engineering.dashboard._change_inbox_location",
                    return_value={
                        "key": "inbox_root",
                        "previous": None,
                        "value": str(transport.resolve()),
                        "changed_at": "2026-08-25T00:00:00+00:00",
                    },
                ) as change_inbox_location,
            ):
                connection.request("POST", "/api/configuration/inbox-location", body=json.dumps({"inbox_root": str(transport)}), headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["value"], str(transport.resolve()))
                change_inbox_location.assert_called_with(root, str(transport), ANY)
                change_inbox_location.reset_mock()
                connection.request("POST", "/api/configuration/inbox-location", body=json.dumps({"inbox_root": str(transport / "Inbox")}), headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["value"], str(transport.resolve()))
                change_inbox_location.assert_called_with(root, str(transport / "Inbox"), ANY)
            with patch("tools.engineering.dashboard._inbox_has_items", return_value=True):
                connection.request("POST", "/api/configuration/inbox-location", body=json.dumps({"inbox_root": str(root / "another-transport")}), headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error_code": "inbox_not_empty"})
            with patch(
                "tools.engineering.dashboard._status",
                return_value=b'{"watcher_state":"ENGINEERING_RUN_ACTIVE","run_id":"inbox-running","current_phase":"COMPLETE"}',
            ):
                connection.request("POST", "/api/configuration/inbox-location", body=json.dumps({"inbox_root": str(transport)}), headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "Wijzig de Inbox-locatie pas wanneer geen uitvoering actief is."})
            retry_outcome = {"blocking_run_id": "inbox-blocked", "retry_filename": "retry-inbox-blocked.md", "retry_run_id": "inbox-retry"}
            with (
                patch("tools.engineering.dashboard.cloud_root", return_value=root),
                patch("tools.engineering.dashboard.predecessor_retry_admission_preflight", return_value="inbox-blocked") as preflight_retry,
                patch("tools.engineering.dashboard.submit_predecessor_retry", return_value=retry_outcome) as submit_retry,
                patch("tools.engineering.dashboard.log_event") as retry_log_event,
            ):
                connection.request("POST", "/api/predecessor-retry", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), retry_outcome)
                preflight_retry.assert_called_once_with(root)
                submit_retry.assert_called_once_with(root, root)
                retry_log_event.assert_any_call(ANY, logging.INFO, "predecessor_retry_submission_triggered", run_id="inbox-blocked", diagnostic="retry_run_id=inbox-retry")
            with (
                patch(
                    "tools.engineering.dashboard.predecessor_retry_admission_preflight",
                    side_effect=dashboard.RetrySubmissionError("Preflight mislukt: werkmap is niet schrijfbaar."),
                ),
                patch("tools.engineering.dashboard.submit_predecessor_retry") as submit_retry,
            ):
                connection.request("POST", "/api/queue-recovery", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "Preflight mislukt: werkmap is niet schrijfbaar."})
                submit_retry.assert_not_called()
            connection.request("POST", "/api/predecessor-retry", body="[]", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            self.assertEqual(json.loads(response.read()), {"error": "De Inbox-watcher verwerkt momenteel een actie. Probeer het opnieuw."})
            managed_recovery = {"previous_branch": "codex/work", "restored_branch": "main"}
            with patch("tools.engineering.dashboard._restore_managed_main_branch", return_value=managed_recovery):
                connection.request("POST", "/api/managed-branch-recovery", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), managed_recovery)
            with patch("tools.engineering.dashboard._restore_managed_main_branch", side_effect=RuntimeError("busy")):
                connection.request("POST", "/api/managed-branch-recovery", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "De werkmap kon niet veilig naar main worden hersteld."})
            synchronization = {"branch": "main", "upstream": "origin/main", "watcher": "restarted"}
            with patch("tools.engineering.dashboard._synchronize_managed_branch_with_upstream", return_value=synchronization):
                connection.request("POST", "/api/managed-branch-synchronization", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), synchronization)
            with patch("tools.engineering.dashboard._synchronize_managed_branch_with_upstream", side_effect=RuntimeError("busy")):
                connection.request("POST", "/api/managed-branch-synchronization", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "De verwachte branch kon niet veilig worden gesynchroniseerd."})
            with patch("tools.engineering.dashboard._recover_stale_workspace_git_lock", return_value={"recovered": True}):
                connection.request("POST", "/api/stale-git-lock-recovery", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), {"recovered": True})
            with patch("tools.engineering.dashboard._recover_stale_workspace_git_lock", side_effect=RuntimeError("active")):
                connection.request("POST", "/api/stale-git-lock-recovery", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "De Git-vergrendeling is niet veilig herstelbaar."})
            branch_preview = {"branches": [{"name": "codex/stale", "reason": "remote_absent_and_matches_main"}]}
            with patch("tools.engineering.dashboard._stale_local_branch_preview", return_value=branch_preview):
                connection.request("POST", "/api/stale-local-branch-cleanup-preview", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), branch_preview)
            cleanup_outcome = {"removed": ["codex/stale"], "removed_count": 1}
            with patch("tools.engineering.dashboard._cleanup_stale_local_branches", return_value=cleanup_outcome) as cleanup:
                connection.request("POST", "/api/stale-local-branch-cleanup", body='{"branches":["codex/stale"]}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), cleanup_outcome)
                cleanup.assert_called_once_with(root, ["codex/stale"])
            with patch("tools.engineering.dashboard._cleanup_stale_local_branches", side_effect=RuntimeError("changed")):
                connection.request("POST", "/api/stale-local-branch-cleanup", body='{"branches":["codex/stale"]}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "Lokale branches konden niet veilig worden opgeruimd."})
            execution_retry_outcome = {"retry_of": "inbox-blocked", "original_run_id": "inbox-blocked", "retry_generation": 1, "retry_timestamp": "2026-08-03T12:00:00+00:00", "filename": "retry-inbox-blocked.md", "retry_run_id": "inbox-retry"}
            with (
                patch("tools.engineering.dashboard.cloud_root", return_value=root),
                patch("tools.engineering.dashboard.retry_admission_preflight") as retry_preflight,
                patch("tools.engineering.dashboard.submit_execution_retry", return_value=execution_retry_outcome) as submit_execution_retry,
                patch("tools.engineering.dashboard.log_event") as execution_retry_log,
            ):
                connection.request("POST", "/api/execution-retry", body='{"run_id":"inbox-blocked"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), execution_retry_outcome)
                retry_preflight.assert_called_once_with(root, "inbox-blocked")
                submit_execution_retry.assert_called_once_with(root, root, "inbox-blocked")
                execution_retry_log.assert_any_call(ANY, logging.INFO, "execution_retry_triggered", run_id="inbox-blocked", diagnostic="retry_run_id=inbox-retry")
            with (
                patch("tools.engineering.dashboard.retry_admission_preflight", side_effect=dashboard.RetrySubmissionError("Preflight mislukt: Git kan geen index-lock aanmaken.")),
                patch("tools.engineering.dashboard.submit_execution_retry") as submit_execution_retry,
            ):
                connection.request("POST", "/api/execution-retry", body='{"run_id":"inbox-blocked"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "Preflight mislukt: Git kan geen index-lock aanmaken."})
                submit_execution_retry.assert_not_called()
            connection.request("POST", "/api/execution-retry", body="{}", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            reconciliation_preview = {"run_id": "inbox-status-drift", "reason": "merged_status_records_stale"}
            with patch("tools.engineering.dashboard.status_reconciliation_preview", return_value=reconciliation_preview) as preview:
                connection.request("POST", "/api/status-reconciliation-preview", body='{"run_id":"inbox-status-drift"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), reconciliation_preview)
                preview.assert_called_once_with(root, "inbox-status-drift")
            with patch("tools.engineering.dashboard.status_reconciliation_preview", side_effect=dashboard.RetrySubmissionError("Geen veilige statusreconciliatie.")):
                connection.request("POST", "/api/status-reconciliation-preview", body='{"run_id":"inbox-status-drift"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "Geen veilige statusreconciliatie."})
            reconciliation_outcome = {**reconciliation_preview, "filename": "status-reconciliation-inbox-status-drift.md"}
            with (
                patch("tools.engineering.dashboard.cloud_root", return_value=root),
                patch("tools.engineering.dashboard.submit_status_reconciliation", return_value=reconciliation_outcome) as submit_reconciliation,
                patch("tools.engineering.dashboard.log_event") as reconciliation_log,
            ):
                connection.request("POST", "/api/status-reconciliation", body='{"run_id":"inbox-status-drift"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), reconciliation_outcome)
                submit_reconciliation.assert_called_once_with(root, root, "inbox-status-drift")
                reconciliation_log.assert_any_call(ANY, logging.INFO, "status_reconciliation_requested", run_id="inbox-status-drift")
            with patch("tools.engineering.dashboard.submit_status_reconciliation", side_effect=dashboard.RetrySubmissionError("Statusherstel is niet beschikbaar.")):
                connection.request("POST", "/api/status-reconciliation", body='{"run_id":"inbox-status-drift"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "Statusherstel is niet beschikbaar."})
            connection.request("POST", "/api/status-reconciliation", body="{}", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            response.read()
            deferred = {"filename": "later.md", "deferred_filename": "later.md", "deferred_at": "2026-08-07T16:00:00+00:00"}
            with (
                patch("tools.engineering.dashboard.cloud_root", return_value=root),
                patch("tools.engineering.dashboard.defer_queued_prompt", return_value=deferred) as defer_prompt,
                patch("tools.engineering.dashboard.log_event") as defer_log_event,
            ):
                connection.request("POST", "/api/queue-defer", body='{"filename":"later.md"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), deferred)
                defer_prompt.assert_called_once_with(root, root, "later.md")
                defer_log_event.assert_any_call(ANY, logging.INFO, "queue_item_deferred", diagnostic="filename=later.md; deferred_filename=later.md")
            for body in ("{}", "[]", '{"filename":1}', '{"filename":"later.md","extra":true}'):
                connection.request("POST", "/api/queue-defer", body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            dismissal = {"run_id": "inbox-blocked", "dismissed": True, "dismissed_at": "2026-08-03T12:01:00+00:00", "dismissed_by": "dashboard_operator"}
            with (
                patch("tools.engineering.dashboard.dismiss_execution", return_value=dismissal) as dismiss,
                patch("tools.engineering.dashboard.log_event") as dismiss_log_event,
            ):
                connection.request("POST", "/api/execution-dismiss", body='{"run_id":"inbox-blocked"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), dismissal)
                dismiss.assert_called_once_with(root, "inbox-blocked")
                dismiss_log_event.assert_any_call(ANY, logging.INFO, "execution_dismissed", run_id="inbox-blocked")
            with patch("tools.engineering.dashboard.dismiss_execution", side_effect=dashboard.RetrySubmissionError("De uitvoering is nog actief.")):
                connection.request("POST", "/api/execution-dismiss", body='{"run_id":"inbox-active"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "De uitvoering is nog actief."})
            for body in ("{}", "[]", '{"run_id":1}', '{"run_id":"inbox-blocked","extra":true}'):
                connection.request("POST", "/api/execution-dismiss", body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertEqual(json.loads(response.read()), {"error": "De uitvoering kan nu niet veilig worden bevestigd."})
            aborted = {"run_id": "inbox-merge-wait", "dismissed": True}
            with (
                patch("tools.engineering.dashboard.abort_operator_merge_wait", return_value=aborted) as abort,
                patch("tools.engineering.dashboard.log_event") as abort_log_event,
            ):
                connection.request("POST", "/api/execution-merge-wait-abort", body='{"run_id":"inbox-merge-wait"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), aborted)
                abort.assert_called_once_with(root, "inbox-merge-wait")
                abort_log_event.assert_any_call(ANY, logging.INFO, "operator_merge_wait_aborted", run_id="inbox-merge-wait")
            with patch("tools.engineering.dashboard.abort_operator_merge_wait", side_effect=dashboard.RetrySubmissionError("Deze uitvoering wacht niet op een pull request-merge.")):
                connection.request("POST", "/api/execution-merge-wait-abort", body='{"run_id":"inbox-merge-wait"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "Deze uitvoering wacht niet op een pull request-merge."})
            for body in ("{}", "[]", '{"run_id":1}', '{"run_id":"inbox-merge-wait","extra":true}'):
                connection.request("POST", "/api/execution-merge-wait-abort", body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertEqual(json.loads(response.read()), {"error": "De wachtende uitvoering kon niet veilig worden afgebroken."})
            emergency_outcome = {"run_id": "inbox-emergency", "stopped": True, "rolled_back": True}
            with (
                patch("tools.engineering.dashboard.execute_emergency_recovery", return_value=emergency_outcome) as emergency,
                patch("tools.engineering.dashboard.log_event") as emergency_log,
            ):
                connection.request("POST", "/api/execution-emergency-rollback", body='{"run_id":"inbox-emergency"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read()), emergency_outcome)
                emergency.assert_called_once_with(root, "inbox-emergency")
                emergency_log.assert_any_call(ANY, logging.WARNING, "execution_emergency_rollback_completed", run_id="inbox-emergency")
            with patch("tools.engineering.dashboard.execute_emergency_recovery", side_effect=dashboard.EmergencyRecoveryError("De host is niet veilig identificeerbaar.")):
                connection.request("POST", "/api/execution-emergency-rollback", body='{"run_id":"inbox-emergency"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "De host is niet veilig identificeerbaar."})
            with (
                patch("tools.engineering.dashboard.check_operator_merge_status", return_value={"verified": True, "continuation": "scheduled", "pull_request": 915}) as check,
                patch("tools.engineering.dashboard.log_event") as check_log_event,
            ):
                connection.request("POST", "/api/execution-merge-status-check", body='{"run_id":"inbox-merge-wait"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                self.assertEqual(json.loads(response.read())["continuation"], "scheduled")
                check.assert_called_once_with(root, "inbox-merge-wait")
                check_log_event.assert_any_call(ANY, logging.INFO, "operator_merge_status_checked", run_id="inbox-merge-wait", diagnostic="scheduled")
            with patch("tools.engineering.dashboard.check_operator_merge_status", return_value={"verified": False, "reason": "pull_request_not_merged", "pull_request": 915}):
                connection.request("POST", "/api/execution-merge-status-check", body='{"run_id":"inbox-merge-wait"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read())["reason"], "pull_request_not_merged")
            with patch("tools.engineering.dashboard.check_operator_merge_status", side_effect=dashboard.RetrySubmissionError("Ongeldige run-ID.")):
                connection.request("POST", "/api/execution-merge-status-check", body='{"run_id":"inbox-merge-wait"}', headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "Ongeldige run-ID."})
            connection.request("POST", "/api/execution-merge-status-check", body="{}", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read()), {"error": "De pull request-status kon niet veilig worden gecontroleerd."})
            connection.request("POST", "/api/rate-limit-reset", body="[]", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            with (
                patch("tools.engineering.dashboard._consume_codex_rate_limit_reset_credit", side_effect=dashboard.RateLimitResetError("Reset niet beschikbaar.")),
                patch("tools.engineering.dashboard.log_event") as reset_failure_log_event,
            ):
                connection.request("POST", "/api/rate-limit-reset", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                response.read()
                reset_failure_log_event.assert_any_call(ANY, logging.WARNING, "ai_usage_reset_failed", diagnostic="Reset niet beschikbaar.")
            with patch("tools.engineering.dashboard._clear_component_log", side_effect=OSError("Niet beschikbaar.")):
                connection.request("POST", "/api/logs/inbox", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                response.read()

    def test_http_dashboard_chat_routes(self) -> None:
        with self._dashboard_http_connection() as (_, connection):
            with (
                patch("tools.engineering.dashboard.codex_chat_response", return_value="Veilig advies."),
                patch("tools.engineering.dashboard.log_event") as chat_log_event,
            ):
                connection.request("POST", "/api/codex-chat", body=json.dumps({"message": "Wat nu?", "history": []}), headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"answer": "Veilig advies.", "model": "gpt-5.6-terra"})
                chat_log_event.assert_any_call(ANY, logging.INFO, "ai_chat_message_sent", diagnostic="[REDACTED]")
            connection.request("POST", "/api/codex-chat", body="{}", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            connection.request("POST", "/api/codex-chat", body=json.dumps({"message": "Wat nu?", "history": []}), headers={"Content-Type": "application/json", "Origin": "https://example.invalid"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()

    @patch(
        "tools.engineering.dashboard.build_relay",
        return_value=Path("/private/tmp/engineering-dashboard-relay"),
    )
    @patch("tools.engineering.dashboard.LaunchdProvider")
    @patch("tools.engineering.dashboard.run")
    def test_main_handles_service_lifecycle(self, run: object, launchd: object, _: object) -> None:
        root = Path(__file__).parents[2]
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.dashboard.Path.home", return_value=Path(temporary)
        ):
            self.assertEqual(dashboard.main(["run", "--repo", str(root), "--port", "9888"]), 0)
            run.assert_called_once()
            self.assertEqual(dashboard.main(["install", "--repo", str(root)]), 0)
            self.assertEqual(launchd.return_value.install.call_count, 2)
            self.assertEqual(dashboard.main(["uninstall", "--repo", str(root)]), 0)
            self.assertEqual(launchd.return_value.uninstall.call_count, 2)

    @patch("tools.engineering.dashboard.TailscaleProvider")
    def test_doctor_reports_both_ready_and_degraded_states(self, provider: object) -> None:
        provider.return_value.status.return_value = __import__(
            "tools.engineering.providers", fromlist=["ProviderStatus"]
        ).ProviderStatus("tailscale", "configured", True, "connected")
        provider.return_value.ipv4_address.return_value = "100.100.100.100"
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.dashboard.Path.home", return_value=Path(temporary)
        ):
            root = Path(temporary) / "repository"
            (root / ".engineering" / "status").mkdir(parents=True)
            self.assertEqual(dashboard.main(["doctor", "--repo", str(root)]), 1)
            (root / ".engineering" / "status" / "status.json").write_text("{}", encoding="utf-8")
            agent = Path(temporary) / "Library/LaunchAgents" / f"{dashboard.LABEL}.plist"
            agent.parent.mkdir(parents=True)
            agent.write_text("owned", encoding="utf-8")
            relay = Path(temporary) / "Library/LaunchAgents" / f"{dashboard.RELAY_LABEL}.plist"
            relay.write_text("owned", encoding="utf-8")
            self.assertEqual(dashboard.main(["doctor", "--repo", str(root)]), 0)

    @patch("tools.engineering.dashboard.binding_addresses", return_value=(LOOPBACK_ADDRESS,))
    @patch("tools.engineering.dashboard.handler")
    def test_server_creation_and_launch_agent_are_private_and_owned(
        self, request_handler: object, _: object
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.dashboard.Path.home", return_value=Path(temporary)
        ):
            root = Path(temporary) / "repository"
            root.mkdir()
            request_handler.return_value = dashboard.BaseHTTPRequestHandler
            servers = dashboard.create_servers(root, port=0)
            try:
                self.assertEqual(len(servers), 1)
            finally:
                for server in servers:
                    server.server_close()
            agent = dashboard.launch_agent(root)
            rendered = agent.read_text(encoding="utf-8")
            self.assertIn(dashboard.LABEL, rendered)
            self.assertIn("KeepAlive", rendered)
            self.assertIn(str(root), rendered)
            self.assertNotIn("<key>PYTHONPATH</key>", rendered)
            self.assertIn("<key>WorkingDirectory</key><string>" + str(root) + "</string>", rendered)
            self.assertIn("/bin/zsh", rendered)
            self.assertIn("-lc", rendered)
            self.assertNotIn(" -P -m tools.engineering.dashboard ", rendered)
            self.assertIn("cd " + str(root) + " &amp;&amp; exec", rendered)
            self.assertIn("exec", rendered)
            self.assertNotIn("StandardOutPath", rendered)
            self.assertNotIn("StandardErrorPath", rendered)

    def test_launch_agent_keeps_the_persisted_log_level_over_an_inherited_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.dashboard.Path.home", return_value=Path(temporary)
        ), patch.dict("os.environ", {dashboard.LOG_LEVEL_ENVIRONMENT: "DEBUG"}):
            root = Path(temporary) / "repository"
            root.mkdir()
            dashboard.update_dashboard_configuration(root, "log_level", "INFO")

            rendered = dashboard.launch_agent(root).read_text(encoding="utf-8")

        self.assertIn(
            f"<key>{dashboard.LOG_LEVEL_ENVIRONMENT}</key><string>INFO</string>",
            rendered,
        )

    @patch("tools.engineering.dashboard._last_executed_commits", return_value=b"not-json")
    @patch("tools.engineering.dashboard._completion_commits", return_value=b"not-json")
    @patch("tools.engineering.dashboard._codex_usage_for_run", return_value=b"not-json")
    @patch("tools.engineering.dashboard._codex_usage", return_value=b"not-json")
    @patch("tools.engineering.dashboard._prompt_started", return_value=b"not-json")
    @patch("tools.engineering.dashboard._sse_status", return_value=b"not-json")
    def test_snapshot_fails_closed_when_optional_projections_are_invalid(self, *_: object) -> None:
        snapshot = json.loads(_sse_snapshot(Path("/missing")))
        self.assertEqual(snapshot["status"]["watcher_state"], "REMOTE_ENGINEERING_DEGRADED")
        self.assertEqual(snapshot["usage"], {})
        self.assertEqual(snapshot["completion_commits"], {})

    @patch("tools.engineering.dashboard.subprocess.run", side_effect=OSError)
    def test_dashboard_process_metrics_fail_closed(self, _: object) -> None:
        self.assertEqual(json.loads(_codex_process_metrics(Path("/missing")))["process_count"], 0)

    @patch("tools.engineering.dashboard.subprocess.run")
    def test_dashboard_build_identifier_handles_failed_git_query(self, run: object) -> None:
        run.return_value = __import__("subprocess").CompletedProcess(("git",), 1, "", "")
        self.assertEqual(dashboard._build_commit(Path("/missing")), "onbekend")

    def test_terminal_watcher_status_is_used_when_no_live_run_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "status.json").write_text(
                '{"watcher_state":"JOB_COMPLETED","current_phase":"COMPLETE"}',
                encoding="utf-8",
            )
            self.assertEqual(json.loads(_status(root))["watcher_state"], "JOB_COMPLETED")
