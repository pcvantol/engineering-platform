from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.dashboard import LOOPBACK_ADDRESS, _codex_process_metrics, _codex_usage, _codex_usage_for_run, _completion_commits, _current_codex_log, _dashboard_html, _last_executed_codex_log, _last_executed_commits, _latest_codex_log, _normalize_rate_limits, _report_for_run, _sse_snapshot, _sse_status, _status, binding_addresses


class DashboardStatusTest(unittest.TestCase):
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
        self.assertIn("No local engineering status", status["diagnostic"])

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
        self.assertIn("Codex-inference draait extern", metrics["gpu_status"])

    def test_current_codex_log_never_falls_back_to_a_different_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".djconnect" / "status"
            logs = root / ".djconnect" / "logs" / "codex"
            status.mkdir(parents=True)
            logs.mkdir(parents=True)
            (status / "current.json").write_text('{"run_id":"inbox-new","phase":"INITIALIZE"}', encoding="utf-8")
            (logs / "inbox-old.log").write_text("old diagnostic", encoding="utf-8")
            self.assertIn(b"current run", _current_codex_log(root))
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
            self.assertIn(b"last executed run", _last_executed_codex_log(root))
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

    @patch("tools.engineering.dashboard.TailscaleProvider.ipv4_address", return_value="100.108.178.11")
    def test_dashboard_binds_only_loopback_and_local_tailscale_address(self, _address: object) -> None:
        self.assertEqual(
            binding_addresses(),
            (LOOPBACK_ADDRESS, "100.108.178.11"),
        )

    @patch("tools.engineering.dashboard.TailscaleProvider.ipv4_address", return_value=None)
    def test_dashboard_fails_closed_to_loopback_without_tailscale(self, _address: object) -> None:
        self.assertEqual(binding_addresses(), (LOOPBACK_ADDRESS,))
