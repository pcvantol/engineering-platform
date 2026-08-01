from __future__ import annotations

import io
import json
from http.client import HTTPConnection
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from tools.engineering import dashboard
from tools.engineering.dashboard import DASHBOARD_VERSION, LOOPBACK_ADDRESS, _codex_process_metrics, _codex_usage, _codex_usage_for_run, _component_log, _completion_commits, _current_codex_log, _dashboard_html, _last_executed_codex_log, _last_executed_commits, _latest_codex_log, _normalize_rate_limits, _report_analysis_for_run, _report_for_run, _sse_snapshot, _sse_status, _status, binding_addresses
from tools.engineering.inbox_watcher import WATCHER_VERSION
from tools.engineering.platform_version import EngineeringPlatformManifest


class DashboardStatusTest(unittest.TestCase):
    def test_component_versions_match_the_canonical_platform_manifest(self) -> None:
        root = Path(__file__).parents[2]
        manifest = EngineeringPlatformManifest.load(
            root / "tools/engineering/ENGINEERING_PLATFORM_VERSION.json"
        )

        self.assertEqual(DASHBOARD_VERSION, manifest.dashboard_version)
        self.assertEqual(WATCHER_VERSION, manifest.watcher_version)

    def test_local_dashboard_supervisor_preserves_private_and_resilient_boundaries(self) -> None:
        source = (Path(__file__).parents[2] / "tools/engineering/dashboard_supervisor.swift").read_text(encoding="utf-8")
        self.assertIn("tailscale", source)
        self.assertIn("SO_NOSIGPIPE", source)
        self.assertIn("Thread.sleep(forTimeInterval: 5)", source)
        self.assertNotIn("0.0.0.0", source)

    def test_dashboard_uses_server_push_without_browser_status_polling(self) -> None:
        page = _dashboard_html("Engineering Status").decode()

        self.assertIn("<title>Engineering Status</title>", page)
        self.assertIn("<h1>Engineering Status</h1>", page)
        self.assertIn('class="dashboard-grid"', page)
        self.assertIn('class="current-run" id="currentRun"', page)
        self.assertIn('class="current-run__title"', page)
        self.assertIn('class="current-run__grid"', page)
        self.assertLess(page.index('class="current-run__title"'), page.index('Promptstatus'))
        self.assertIn('$("currentRun").hidden=!active', page)
        self.assertNotIn('$("current").hidden=!active', page)
        self.assertIn("@media (min-width:900px)", page)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", page)
        self.assertIn('class="prompt-runs"', page)
        self.assertIn('class="prompt-runs__cards"', page)
        self.assertIn(".prompt-runs,.technical-details{grid-column:1 / -1}", page)
        self.assertIn('class="technical-details"', page)
        self.assertIn("Technische details", page)
        self.assertIn('id="currentDiagnostic" hidden', page)
        self.assertIn('id="lastDiagnostic" hidden', page)
        self.assertIn('class="card card--previous"', page)
        self.assertIn(".card--previous", page)
        self.assertIn('id="currentTime"', page)
        self.assertIn('id="lastRefresh"', page)
        self.assertIn('id="updateMode"', page)
        self.assertIn('timeZone:"Europe/Amsterdam"', page)
        self.assertIn('"nl-NL"', page)
        self.assertIn('new EventSource("/api/events")', page)
        self.assertIn('addEventListener("dashboard"', page)
        self.assertIn("Serverpush: verbonden", page)
        self.assertNotIn("setInterval(checkBuild,5000)", page)
        self.assertNotIn("setInterval(promptStarted,5000)", page)
        self.assertNotIn('fetch("/api/status")', page)
        self.assertIn('id="indicator"', page)
        self.assertIn('id="predecessorGate" hidden', page)
        self.assertIn('id="queueItems" hidden', page)
        self.assertIn('id="queueList"', page)
        self.assertIn("Nog niet geclaimde Inbox-prompts, oudste eerst.", page)
        self.assertIn("function queueItems(x)", page)
        self.assertIn("Wachtrij geblokkeerd", page)
        self.assertIn('id="predecessorRun"', page)
        self.assertIn('id="predecessorAction"', page)
        self.assertIn('WAITING_FOR_PREDECESSOR:"Wacht op voorafgaande prompt"', page)
        self.assertIn("indicator--green", page)
        self.assertIn("indicator--yellow", page)
        self.assertIn("indicator--orange", page)
        self.assertIn("indicator--red", page)
        self.assertIn("indicator--running", page)
        self.assertIn('function isActiveRun(x){return x.watcher_state==="ENGINEERING_RUN_ACTIVE"&&Boolean(x.run_id)}', page)
        self.assertIn("@keyframes spin", page)
        self.assertIn('ENGINEERING_RUN_ACTIVE:"Engineering actief"', page)
        self.assertIn('EXECUTE_AGENT:"Uitvoering"', page)
        self.assertIn('invoke_agent:"Engineering uitvoeren"', page)
        self.assertIn('MERGED_RECONCILED:"Samengevoegd en afgestemd"', page)
        self.assertIn('WORKSPACE_READY:"Werkruimte gereed"', page)
        self.assertIn('id="report"', page)
        self.assertIn("Engineeringrapport", page)
        self.assertIn('fetch("/api/report/last-executed?run_id="+encodeURIComponent(lastExecutedRun))', page)
        self.assertIn('id="reportAnalysis"', page)
        self.assertIn("Codex-analyse van rapport", page)
        self.assertIn('fetch("/api/report-analysis/last-executed?run_id="+encodeURIComponent(lastExecutedRun))', page)
        self.assertIn('id="promptRuns"', page)
        self.assertIn('id="lastExecution" hidden', page)
        self.assertIn('id="lastIndicator"', page)
        self.assertIn('id="lastFinalStatus"', page)
        self.assertIn('id="lastCommits" hidden', page)
        self.assertIn("lastCommits(snapshot.last_executed_commits)", page)
        self.assertIn('id="lastUsage" hidden', page)
        self.assertIn("lastUsage(snapshot.last_executed_usage)", page)
        self.assertIn("function finalStatus(phase)", page)
        self.assertIn("Geblokkeerd", page)
        self.assertIn("Mislukt", page)
        self.assertIn('id="executionContext" hidden', page)
        for label in ("Modus", "Repository", "Lokale checkout", "Actieve branch"):
            self.assertIn(label, page)
        self.assertIn('id="copyReport"', page)
        self.assertIn("navigator.clipboard.writeText", page)
        self.assertIn("function fallbackCopy(value)", page)
        self.assertIn('document.execCommand("copy")', page)
        self.assertIn("window.isSecureContext", page)
        self.assertIn('id="componentLogs"', page)
        self.assertIn('id="loadComponentLogs"', page)
        self.assertIn('fetch("/api/logs/inbox")', page)
        self.assertIn('fetch("/api/logs/dashboard")', page)
        self.assertIn('id="codexChat"', page)
        self.assertIn('id="chatModel">gpt-5.6-terra', page)
        self.assertIn("Codex gesprek", page)
        self.assertIn('fetch("/api/codex-chat"', page)
        self.assertIn("Alleen lezen", page)
        self.assertIn('id="currentDiagnostic" hidden', page)
        self.assertIn('id="lastDiagnostic" hidden', page)
        self.assertIn('x.startsWith("No Codex CLI diagnostic is available")', page)
        self.assertIn('return "grey"', page)
        self.assertIn('id="executionEstimate"', page)
        self.assertIn('id="executionEstimateMeta" hidden', page)
        self.assertIn('id="processMetrics" hidden', page)
        self.assertIn('id="codexCpu"', page)
        self.assertIn('id="codexGpu"', page)
        self.assertIn("estimate-primary", page)
        self.assertIn("estimate-meta", page)
        self.assertIn("function estimate(x)", page)
        self.assertIn("function renderEstimate(x)", page)
        self.assertIn("function executionRange(x)", page)
        self.assertIn("gebaseerd op promptomvang, fase en verstreken tijd", page)
        self.assertIn("Geen live Codex-voortgang of tokenverbruik", page)
        self.assertIn("Geen betrouwbare ETA", page)
        self.assertIn('id="usage"', page)
        self.assertIn('id="rateLimits" hidden', page)
        self.assertIn("function rateLimits(x)", page)
        self.assertIn("rateLimits(snapshot.rate_limits)", page)
        self.assertIn("Beschikbare resets", page)
        self.assertIn("Engineering Platform-versie", page)
        self.assertIn('id="platformVersion"', page)
        self.assertIn('id="dashboardVersion"', page)
        self.assertIn('id="workerVersion"', page)
        self.assertIn("component_versions", page)
        self.assertIn("Git-commit", page)
        self.assertIn("onbekend", page)
        self.assertIn('DASHBOARD_BUILD="onbekend"', page)
        for label in (
            "Watcher",
            "Fase",
            "Huidige actie",
            "Prompttitel",
            "Bestandsnaam",
            "Codex CLI-diagnose",
            "Run-ID",
            "Wachtrij",
            "Repositorystatus",
            "Werkruimtestatus",
        ):
            self.assertIn(label, page)

    def test_codex_usage_is_shown_only_for_the_displayed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".djconnect" / "status"
            status.mkdir(parents=True)
            (status / "status.json").write_text('{"run_id":"inbox-visible"}', encoding="utf-8")
            (status / "codex_usage.json").write_text(
                '{"run_id":"inbox-visible","usage":{"input_tokens":123,"cost":1.25}}',
                encoding="utf-8",
            )
            self.assertEqual(
                json.loads(_codex_usage(root)), {"input_tokens": 123, "cost": 1.25}
            )
            (status / "codex_usage.json").write_text(
                '{"run_id":"inbox-other","usage":{"input_tokens":123}}', encoding="utf-8"
            )
            self.assertEqual(json.loads(_codex_usage(root)), {})

    def test_last_executed_usage_is_bound_to_its_exact_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".djconnect" / "status"
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
            self.assertEqual(dashboard._codex_rate_limits(), b"{}")
            dashboard._rate_limit_cache = None
        self.assertTrue(process.terminated)

    def test_codex_rate_limits_fails_closed_when_app_server_cannot_start(self) -> None:
        with patch("tools.engineering.dashboard.subprocess.Popen", side_effect=OSError):
            dashboard._rate_limit_cache = None
            self.assertEqual(dashboard._codex_rate_limits(), b"{}")
            dashboard._rate_limit_cache = None

    def test_completion_commits_are_shown_only_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".djconnect" / "status"
            runs = root / ".djconnect" / "engineering-runs"
            status.mkdir(parents=True)
            runs.mkdir(parents=True)
            (status / "status.json").write_text('{"run_id":"inbox-done","current_phase":"COMPLETE"}', encoding="utf-8")
            (runs / "inbox-done.json").write_text('{"genesis_commit_sha":"' + "a" * 40 + '"}', encoding="utf-8")
            self.assertEqual(json.loads(_completion_commits(root)), {"Genesis-commit": "a" * 40})

    def test_last_executed_commits_are_bound_to_the_completed_last_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".djconnect" / "status"
            runs = root / ".djconnect" / "engineering-runs"
            status.mkdir(parents=True)
            runs.mkdir(parents=True)
            (status / "status.json").write_text(
                '{"last_executed_run":"inbox-done","last_executed_phase":"COMPLETE"}', encoding="utf-8"
            )
            (runs / "inbox-done.json").write_text(
                '{"implementation_merge_commit":"' + "b" * 40 + '"}', encoding="utf-8"
            )
            self.assertEqual(json.loads(_last_executed_commits(root)), {"Implementatie-mergecommit": "b" * 40})

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
            directory = Path(temporary) / ".djconnect" / "status"
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
            status = json.loads(_status(Path(temporary)))

        self.assertEqual(status["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(status["current_phase"], "INITIALIZE")
        self.assertEqual(status["run_id"], "inbox-123")
        self.assertEqual(status["prompt_characters"], 4321)

    def test_active_runner_status_wins_over_previous_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".djconnect" / "status"
            directory.mkdir(parents=True)
            (directory / "status.json").write_text('{"current_phase":"BLOCKED"}', encoding="utf-8")
            (directory / "current.json").write_text(
                '{"run_id":"inbox-new","phase":"INITIALIZE","current_action":"Starting"}',
                encoding="utf-8",
            )
            status = json.loads(_status(Path(temporary)))

        self.assertEqual(status["current_phase"], "INITIALIZE")
        self.assertEqual(status["run_id"], "inbox-new")

    def test_live_runner_status_preserves_the_watcher_queue_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".djconnect" / "status"
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
            directory = Path(temporary) / ".djconnect" / "status"
            directory.mkdir(parents=True)
            (directory / "status.json").write_text('{\n  "watcher_state": "WATCHER_IDLE"\n}', encoding="utf-8")
            payload = _sse_status(Path(temporary))

        self.assertNotIn(b"\n", payload)
        self.assertEqual(json.loads(payload)["watcher_state"], "WATCHER_IDLE")

    @patch("tools.engineering.dashboard._codex_rate_limits", return_value=b"{}")
    def test_sse_snapshot_contains_the_read_only_dashboard_projection(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".djconnect" / "status"
            directory.mkdir(parents=True)
            (directory / "status.json").write_text('{"watcher_state":"WATCHER_IDLE"}', encoding="utf-8")
            snapshot = json.loads(_sse_snapshot(Path(temporary)))

        self.assertEqual(snapshot["status"]["watcher_state"], "WATCHER_IDLE")
        self.assertIn("build_commit", snapshot)
        self.assertEqual(snapshot["prompt_started"], {})
        self.assertEqual(snapshot["usage"], {})
        self.assertEqual(snapshot["rate_limits"], {})
        self.assertEqual(snapshot["component_versions"]["dashboard"], DASHBOARD_VERSION)
        self.assertEqual(snapshot["component_versions"]["worker"], WATCHER_VERSION)

    def test_latest_codex_log_is_local_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / ".djconnect" / "logs" / "codex"
            logs.mkdir(parents=True)
            (logs / "run.log").write_text("redacted diagnostic", encoding="utf-8")
            self.assertEqual(_latest_codex_log(Path(temporary)), b"redacted diagnostic")

    @patch("tools.engineering.dashboard.subprocess.run")
    def test_codex_process_metrics_sum_only_codex_cli_processes(self, run: object) -> None:
        run.return_value = __import__("subprocess").CompletedProcess(
            ("ps",),
            0,
            "101  12.4 /opt/homebrew/bin/codex exec task\n102  3.1 /usr/bin/python worker.py\n103  7.5 codex exec review\n",
            "",
        )
        metrics = json.loads(_codex_process_metrics())
        self.assertEqual(metrics["process_count"], 2)
        self.assertEqual(metrics["cpu_percent"], 19.9)
        self.assertIn("Codex-verwerking draait extern", metrics["gpu_status"])

    def test_current_codex_log_never_falls_back_to_a_different_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".djconnect" / "status"
            logs = root / ".djconnect" / "logs" / "codex"
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
            status = root / ".djconnect" / "status"
            logs = root / ".djconnect" / "logs" / "codex"
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
            reports = root / ".djconnect" / "reports"
            reports.mkdir(parents=True)
            (reports / "one_inbox-other.md").write_text("other", encoding="utf-8")
            (reports / "two_inbox-last.md").write_text("last", encoding="utf-8")
            self.assertEqual(_report_for_run(root, "inbox-last"), b"last")
            self.assertEqual(_report_for_run(root, "inbox-missing"), b"")

    def test_report_analysis_is_bound_to_the_requested_last_executed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analyses = root / ".djconnect" / "report-analysis"
            analyses.mkdir(parents=True)
            (analyses / "inbox-other.md").write_text("other analysis", encoding="utf-8")
            (analyses / "inbox-last.md").write_text("last analysis", encoding="utf-8")
            self.assertEqual(_report_analysis_for_run(root, "inbox-last"), b"last analysis")
            self.assertEqual(_report_analysis_for_run(root, "inbox-missing"), b"")
    def test_component_log_is_bounded_to_known_redacted_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / ".djconnect" / "logs"
            logs.mkdir(parents=True)
            (logs / "inbox.log").write_text("first\nsecond\n", encoding="utf-8")
            self.assertEqual(_component_log(root, "inbox"), b"first\nsecond")
            self.assertEqual(_component_log(root, "unknown"), b"")

    @patch("tools.engineering.dashboard.TailscaleProvider.ipv4_address", return_value="100.108.178.11")
    def test_dashboard_binds_only_loopback_and_local_tailscale_address(self, _address: object) -> None:
        self.assertEqual(
            binding_addresses(),
            (LOOPBACK_ADDRESS, "100.108.178.11"),
        )

    @patch("tools.engineering.dashboard.TailscaleProvider.ipv4_address", return_value=None)
    def test_dashboard_fails_closed_to_loopback_without_tailscale(self, _address: object) -> None:
        self.assertEqual(binding_addresses(), (LOOPBACK_ADDRESS,))

    def test_http_dashboard_exposes_status_routes_and_bounded_read_only_chat(self) -> None:
        root = Path(__file__).parents[2]
        server = dashboard.DashboardHTTPServer((LOOPBACK_ADDRESS, 0), dashboard.handler(root))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection(LOOPBACK_ADDRESS, server.server_port, timeout=2)
        try:
            for route, content_type in (
                ("/", "text/html"),
                ("/api/status", "application/json"),
                ("/api/build", "application/json"),
                ("/api/health", "application/json"),
                ("/api/process-metrics", "application/json"),
                ("/api/usage", "application/json"),
                ("/api/usage/last-executed?run_id=invalid", "application/json"),
                ("/api/commits", "application/json"),
                ("/api/commits/last-executed", "application/json"),
                ("/api/prompt-started", "application/json"),
                ("/api/log/latest", "text/plain"),
                ("/api/logs/inbox", "text/plain"),
                ("/api/logs/dashboard", "text/plain"),
                ("/api/log/current", "text/plain"),
                ("/api/log/last", "text/plain"),
                ("/api/report/latest", "text/markdown"),
                ("/api/report/last-executed?run_id=invalid", "text/markdown"),
            ):
                connection.request("GET", route)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.getheader("Content-Type"))
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                response.read()
            connection.request("GET", "/missing")
            self.assertEqual(connection.getresponse().status, 404)
            with patch("tools.engineering.dashboard.codex_chat_response", return_value="Veilig advies."):
                connection.request(
                    "POST",
                    "/api/codex-chat",
                    body=json.dumps({"message": "Wat nu?", "history": []}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read()),
                    {"answer": "Veilig advies.", "model": "gpt-5.6-terra"},
                )
            connection.request(
                "POST",
                "/api/codex-chat",
                body=json.dumps({"message": "Wat nu?", "history": []}),
                headers={"Content-Type": "application/json", "Origin": "https://example.invalid"},
            )
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @patch("tools.engineering.dashboard.LaunchdProvider")
    @patch("tools.engineering.dashboard.run")
    def test_main_handles_service_lifecycle(self, run: object, launchd: object) -> None:
        root = Path(__file__).parents[2]
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.dashboard.Path.home", return_value=Path(temporary)
        ):
            self.assertEqual(dashboard.main(["run", "--repo", str(root), "--port", "9888"]), 0)
            run.assert_called_once()
            self.assertEqual(dashboard.main(["install", "--repo", str(root)]), 0)
            launchd.return_value.install.assert_called_once()
            self.assertEqual(dashboard.main(["uninstall", "--repo", str(root)]), 0)
            launchd.return_value.uninstall.assert_called_once()

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
            (root / ".djconnect" / "status").mkdir(parents=True)
            self.assertEqual(dashboard.main(["doctor", "--repo", str(root)]), 1)
            (root / ".djconnect" / "status" / "status.json").write_text("{}", encoding="utf-8")
            agent = Path(temporary) / "Library/LaunchAgents" / f"{dashboard.LABEL}.plist"
            agent.parent.mkdir(parents=True)
            agent.write_text("owned", encoding="utf-8")
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
            self.assertIn("/bin/zsh", rendered)
            self.assertIn("-lc", rendered)
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
        self.assertEqual(json.loads(_codex_process_metrics())["process_count"], 0)

    @patch("tools.engineering.dashboard.subprocess.run")
    def test_dashboard_build_identifier_handles_failed_git_query(self, run: object) -> None:
        run.return_value = __import__("subprocess").CompletedProcess(("git",), 1, "", "")
        self.assertEqual(dashboard._build_commit(Path("/missing")), "onbekend")

    def test_terminal_watcher_status_is_used_when_no_live_run_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".djconnect" / "status"
            status.mkdir(parents=True)
            (status / "status.json").write_text(
                '{"watcher_state":"JOB_COMPLETED","current_phase":"COMPLETE"}',
                encoding="utf-8",
            )
            self.assertEqual(json.loads(_status(root))["watcher_state"], "JOB_COMPLETED")
