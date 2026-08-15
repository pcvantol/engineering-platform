from __future__ import annotations

import io
import json
import logging
from http.client import HTTPConnection
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from contextlib import contextmanager, nullcontext
from unittest.mock import ANY, MagicMock, patch

from tools.engineering import dashboard
from tools.engineering.dashboard import DASHBOARD_VERSION, LOOPBACK_ADDRESS, _clear_component_log, _codex_process_metrics, _codex_provider_identity, _codex_usage, _codex_usage_for_run, _component_log, _component_log_versions, _completion_commits, _component_uptime_seconds, _current_codex_log, _dashboard_html, _last_executed_agent_execution, _last_executed_codex_log, _last_executed_commits, _last_executed_runtime_metadata, _latest_codex_log, _normalize_rate_limits, _platform_health, _prompt_history, _prompt_history_detail, _report_analysis_available_for_run, _report_analysis_for_run, _report_for_run, _reviewer_agents_for_run, _sse_snapshot, _sse_status, _status, _tracked_file_count, _workspace_free_disk_space, binding_addresses
from tools.engineering.inbox_watcher import WATCHER_VERSION
from tools.engineering.platform_version import EngineeringPlatformManifest
from tools.engineering.prompt_history import record_prompt_execution
from tools.engineering.storage import open_storage, store_projection
from tools.engineering.agent_state import StateStore, TransactionState
from tools.engineering.execution_lease import acquire


class DashboardStatusTest(unittest.TestCase):
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
        self.assertIn("Engineering Platform 1.5.0", page)

    def test_dashboard_exposes_the_canonical_five_locale_catalog(self) -> None:
        root = Path(__file__).parents[2]
        catalog = (root / "tools/engineering/assets/dashboard_locales.mjs").read_text(encoding="utf-8")
        page = _dashboard_html("Engineering Status").decode("utf-8")

        self.assertIn('id="dashboardLocale"', page)
        self.assertIn('"/assets/dashboard_locales.mjs"', (root / "tools/engineering/dashboard.py").read_text(encoding="utf-8"))
        for locale in ("en", "nl", "de", "fr", "es"):
            self.assertIn(f"  {locale}: {{", catalog)
            self.assertIn(f'"language.{locale}"', catalog)
        self.assertIn('"retry.details"', catalog)
        for key in (
            "detail.recommended_next_mission", "detail.recommendation_status", "detail.mission_origin",
            "detail.business_value", "detail.confidence", "detail.dependencies", "detail.alternatives",
            "detail.decision_evidence", "detail.projection_incomplete", "technical.git_lock",
            "technical.git_lock_recovery_action",
        ):
            self.assertEqual(catalog.count(f'"{key}"'), 5)
        self.assertNotIn("Retry Execution", (root / "tools/engineering/assets/dashboard.js").read_text(encoding="utf-8"))
        dashboard_script = (root / "tools/engineering/assets/dashboard.js").read_text(encoding="utf-8")
        self.assertIn("createLocaleService", dashboard_script)
        self.assertNotIn('"nl-NL"', dashboard_script)
        self.assertNotIn("localeCompare(", dashboard_script)

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

    def test_rate_limit_helpers_cover_generic_windows_and_unavailable_provider_version(self) -> None:
        self.assertEqual(dashboard._rate_limit_window_label(1_440), "1-daags venster")
        self.assertEqual(dashboard._rate_limit_window_label(120), "2-uursvenster")
        self.assertEqual(dashboard._rate_limit_window_label(17), "17-minutenvenster")
        self.assertEqual(dashboard._normalize_rate_limits([]), {})
        self.assertEqual(dashboard._normalize_rate_limits({"rateLimits": []}), {})

        dashboard._codex_identity_cache = None
        with patch("tools.engineering.dashboard.shutil.which", return_value=None):
            self.assertEqual(
                dashboard._codex_provider_identity(),
                {"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"},
            )

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
        self.assertLess(page.index('id="executionContext"'), page.index('id="indicator"'))
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
        self.assertIn("height:100lvh;min-height:100dvh", stylesheet)
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
        self.assertIn("gap:var(--dashboard-section-gap)", stylesheet)
        self.assertIn("scrollbar-gutter:stable", stylesheet)
        self.assertIn(".inbox-queue,.prompt-history", stylesheet)
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

    @patch("tools.engineering.dashboard.subprocess.run")
    @patch("tools.engineering.dashboard.shutil.which", return_value="/usr/local/bin/codex")
    def test_codex_provider_identity_keeps_only_the_cli_version(
        self, _: object, run: object
    ) -> None:
        run.return_value = __import__("subprocess").CompletedProcess(
            ("codex", "--version"), 0, "OpenAI Codex v0.146.0", ""
        )
        dashboard._codex_identity_cache = None
        self.assertEqual(
            _codex_provider_identity(),
            {"provider": "Codex CLI", "provider_version": "0.146.0"},
        )
        dashboard._codex_identity_cache = None

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
        with patch("tools.engineering.dashboard.subprocess.Popen", return_value=process):
            dashboard._rate_limit_cache = None
            self.assertEqual(
                json.loads(dashboard._codex_rate_limits()),
                {"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"},
            )
            dashboard._rate_limit_cache = None
        self.assertTrue(process.terminated)

    def test_codex_rate_limits_fails_closed_when_app_server_cannot_start(self) -> None:
        with patch("tools.engineering.dashboard.subprocess.Popen", side_effect=OSError):
            dashboard._rate_limit_cache = None
            self.assertEqual(
                json.loads(dashboard._codex_rate_limits()),
                {"provider": "Codex CLI", "provider_version": "versie niet beschikbaar"},
            )
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
        self.assertEqual(details["schema_version"], "19")

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
            payload = json.loads(_prompt_history_detail(root, "inbox-detail"))
            self.assertEqual(payload["history"]["run_id"], "inbox-detail")
            self.assertEqual(payload["history"]["title"], "Detail prompt")
            self.assertEqual(payload["usage"], {})
            self.assertEqual(_prompt_history_detail(root, "../../other"), b"")

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
            self.assertEqual(detail["usage"], {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30})
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
        connection = HTTPConnection(LOOPBACK_ADDRESS, server.server_port, timeout=2)
        try:
            yield root, connection
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_dashboard_status_routes(self) -> None:
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
            self.assertIn("<key>PYTHONPATH</key><string>" + str(root) + "</string>", rendered)
            self.assertIn("<key>WorkingDirectory</key><string>/</string>", rendered)
            self.assertIn("/bin/zsh", rendered)
            self.assertIn("-lc", rendered)
            self.assertIn("cd / &amp;&amp; exec", rendered)
            self.assertIn("exec", rendered)
            self.assertNotIn("StandardOutPath", rendered)
            self.assertNotIn("StandardErrorPath", rendered)

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
