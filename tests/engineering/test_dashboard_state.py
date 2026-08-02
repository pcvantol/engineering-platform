from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.engineering import dashboard_state


class DashboardStateTest(unittest.TestCase):
    def test_status_prefers_the_live_projection_for_an_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "status.json").write_text(
                json.dumps({"queue_items": [{"filename": "later.md"}]}), encoding="utf-8"
            )
            (status / "current.json").write_text(
                json.dumps({"run_id": "run-1", "phase": "EXECUTE_AGENT"}), encoding="utf-8"
            )

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["queue_items"], [{"filename": "later.md"}])

    def test_snapshot_isolated_from_optional_telemetry_failure(self) -> None:
        root = Path("/workspace")

        payload = json.loads(
            dashboard_state.snapshot(
                root,
                status_reader=lambda _: b'{"last_executed_run":"run-2"}',
                unavailable_reader=dashboard_state.unavailable_status,
                prompt_started_reader=lambda _: b"{}",
                usage_reader=lambda _: b"{}",
                rate_limits_reader=lambda: b"{}",
                usage_for_run_reader=lambda _, __: b'{"input_tokens":3}',
                completion_commits_reader=lambda _: b"{}",
                last_executed_commits_reader=lambda _: b"{}",
                reviewer_agents_reader=lambda _, __: b"[]",
                execution_reader=lambda _, __: b"{}",
                runtime_metadata_reader=lambda _, __: b"{}",
                report_analysis_available_reader=lambda _, __: False,
                telemetry_reader=lambda _: (_ for _ in ()).throw(RuntimeError("unavailable")),
                process_metrics_reader=lambda: b"{}",
                build_commit_reader=lambda _: "abc123",
                component_log_versions_reader=lambda _: {"inbox": "1", "dashboard": "2"},
                dashboard_version="1.2.79",
                worker_version="1.1.2",
            )
        )

        self.assertEqual(payload["build_commit"], "abc123")
        self.assertEqual(payload["last_executed_usage"], {"input_tokens": 3})
        self.assertEqual(payload["telemetry"], [])
        self.assertEqual(payload["component_log_versions"], {"inbox": "1", "dashboard": "2"})
