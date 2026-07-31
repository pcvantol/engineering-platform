from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.dashboard import LOOPBACK_ADDRESS, _codex_usage, _completion_commits, _current_codex_log, _dashboard_html, _last_executed_codex_log, _latest_codex_log, _sse_status, _status, binding_addresses


class DashboardStatusTest(unittest.TestCase):
    def test_dashboard_shows_amsterdam_time_and_refresh_countdown(self) -> None:
        page = _dashboard_html("DJConnect Engineering").decode()

        self.assertIn('id="currentTime"', page)
        self.assertIn('id="lastRefresh"', page)
        self.assertIn('id="nextRefresh"', page)
        self.assertIn('timeZone:"Europe/Amsterdam"', page)
        self.assertIn('"nl-NL"', page)
        self.assertIn("REFRESH_SECONDS=5", page)
        self.assertIn('id="indicator"', page)
        self.assertIn("indicator--green", page)
        self.assertIn("indicator--yellow", page)
        self.assertIn("indicator--orange", page)
        self.assertIn("indicator--red", page)
        self.assertIn("indicator--running", page)
        self.assertIn("@keyframes spin", page)
        self.assertIn('ENGINEERING_RUN_ACTIVE:"Engineering actief"', page)
        self.assertIn('EXECUTE_AGENT:"Uitvoering"', page)
        self.assertIn('invoke_agent:"Engineering uitvoeren"', page)
        self.assertIn('MERGED_RECONCILED:"Samengevoegd en afgestemd"', page)
        self.assertIn('WORKSPACE_READY:"Werkruimte gereed"', page)
        self.assertIn('id="report"', page)
        self.assertIn("Engineeringrapport", page)
        self.assertIn('fetch("/api/report/latest")', page)
        self.assertIn('id="copyReport"', page)
        self.assertIn("navigator.clipboard.writeText", page)
        self.assertIn('return "grey"', page)
        self.assertIn('id="executionEstimate"', page)
        self.assertIn("function estimate(x)", page)
        self.assertIn("ongeveer 15–30 minuten", page)
        self.assertIn("geen betrouwbare ETA", page)
        self.assertIn('id="usage"', page)
        self.assertIn('fetch("/api/usage")', page)
        self.assertIn("Engineering Platform-versie", page)
        self.assertIn('id="platformVersion"', page)
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
                    }
                ),
                encoding="utf-8",
            )
            status = json.loads(_status(Path(temporary)))

        self.assertEqual(status["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(status["current_phase"], "INITIALIZE")
        self.assertEqual(status["run_id"], "inbox-123")

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

    def test_latest_codex_log_is_local_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / ".djconnect" / "logs" / "codex"
            logs.mkdir(parents=True)
            (logs / "run.log").write_text("redacted diagnostic", encoding="utf-8")
            self.assertEqual(_latest_codex_log(Path(temporary)), b"redacted diagnostic")

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

    @patch("tools.engineering.dashboard.TailscaleProvider.ipv4_address", return_value="100.108.178.11")
    def test_dashboard_binds_only_loopback_and_local_tailscale_address(self, _address: object) -> None:
        self.assertEqual(
            binding_addresses(),
            (LOOPBACK_ADDRESS, "100.108.178.11"),
        )

    @patch("tools.engineering.dashboard.TailscaleProvider.ipv4_address", return_value=None)
    def test_dashboard_fails_closed_to_loopback_without_tailscale(self, _address: object) -> None:
        self.assertEqual(binding_addresses(), (LOOPBACK_ADDRESS,))
