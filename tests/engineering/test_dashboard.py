from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.dashboard import LOOPBACK_ADDRESS, _current_codex_log, _latest_codex_log, _sse_status, _status, binding_addresses


class DashboardStatusTest(unittest.TestCase):
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

    @patch("tools.engineering.dashboard.TailscaleProvider.ipv4_address", return_value="100.108.178.11")
    def test_dashboard_binds_only_loopback_and_local_tailscale_address(self, _address: object) -> None:
        self.assertEqual(
            binding_addresses(),
            (LOOPBACK_ADDRESS, "100.108.178.11"),
        )

    @patch("tools.engineering.dashboard.TailscaleProvider.ipv4_address", return_value=None)
    def test_dashboard_fails_closed_to_loopback_without_tailscale(self, _address: object) -> None:
        self.assertEqual(binding_addresses(), (LOOPBACK_ADDRESS,))
