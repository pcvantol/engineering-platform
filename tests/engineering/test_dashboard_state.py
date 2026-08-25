from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering import dashboard_state
from tools.engineering.agent_state import StateStore, TransactionState
from tools.engineering.execution_lease import acquire
from tools.engineering.storage import record_submission


class DashboardStateTest(unittest.TestCase):
    def test_status_prefers_the_live_projection_for_an_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "status.json").write_text(
                json.dumps(
                    {
                        "platform_version": "2.0.0",
                        "queue_depth": 1,
                        "queue_items": [{"filename": "later.md"}],
                    }
                ),
                encoding="utf-8",
            )
            (status / "current.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "phase": "EXECUTE_AGENT",
                        "transient_action": "Inspect the configuration boundary",
                        "workspace_progress": {
                            "modified": 3,
                            "created": 2,
                            "deleted": 1,
                            "codex_commands_executed": 17,
                        },
                    }
                ),
                encoding="utf-8",
            )
            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState("run-1", "repo", "prompt.md", "EXECUTE_AGENT")
            )
            acquire(root, "run-1", identity="test-host", instance_id="test-instance")

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(payload["platform_version"], "2.0.0")
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["queue_depth"], 1)
        self.assertEqual(payload["queue_items"], [{"filename": "later.md"}])
        self.assertEqual(
            payload["workspace_progress"],
            {"modified": 3, "created": 2, "deleted": 1, "codex_commands_executed": 17},
        )
        self.assertEqual(payload["lifecycle"]["live_activity"], "Inspect the configuration boundary")

    def test_status_projects_only_the_persisted_execution_context_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            context = {
                "mission_id": "MISSION-42",
                "business_summary": "Protect the operator journey.",
                "planning_confidence": {"value": "0.91"},
            }
            (status / "status.json").write_text(json.dumps({}), encoding="utf-8")
            (status / "current.json").write_text(json.dumps({"run_id": "run-42", "phase": "EXECUTE_AGENT"}), encoding="utf-8")
            record_submission(
                root, submission_id="submission-42", producer_id="forge", producer_type="FORGE",
                prompt_content="prompt", prompt_metadata={}, target_identity={}, original_envelope="{}",
                received_at="2026-08-07T08:00:00Z", link_run_id="run-42",
                execution_context=context,
            )

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload["execution_context"], context)

    def test_status_ignores_a_terminal_live_projection_for_active_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            watcher = {
                "watcher_state": "WAITING_FOR_PREDECESSOR",
                "run_id": None,
                "queue_items": [{"filename": "later.md"}],
                "last_executed_run": "inbox-terminal",
                "last_executed_phase": "BLOCKED",
            }
            (status / "status.json").write_text(json.dumps(watcher), encoding="utf-8")
            (status / "current.json").write_text(
                json.dumps({"run_id": "inbox-terminal", "phase": "BLOCKED"}),
                encoding="utf-8",
            )

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload, watcher)

    def test_status_includes_blocking_predecessor_lifecycle_while_queue_waits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            watcher = {
                "watcher_state": "WAITING_FOR_PREDECESSOR",
                "run_id": None,
                "blocking_predecessor_run": "blocked-run",
                "blocking_predecessor_phase": "BLOCKED",
            }
            (status / "status.json").write_text(json.dumps(watcher), encoding="utf-8")
            (status / "current.json").write_text(json.dumps({}), encoding="utf-8")
            predecessor_lifecycle = {
                "run_id": "blocked-run",
                "available": True,
                "terminal_state": "BLOCKED",
                "steps": [{"id": "TERMINAL", "state": "BLOCKED"}],
                "recovery": {"kind": "status_reconciliation", "run_id": "blocked-run"},
            }

            with patch("tools.engineering.dashboard_state.lifecycle_projection", return_value=predecessor_lifecycle):
                payload = json.loads(dashboard_state.status(root))

        self.assertIsNone(payload["run_id"])
        self.assertEqual(payload["lifecycle"]["run_id"], "blocked-run")
        self.assertEqual(payload["lifecycle"]["terminal_state"], "BLOCKED")
        self.assertIsNone(payload["lifecycle"]["recovery"])

    def test_status_hides_a_stale_dismissed_predecessor_projection(self) -> None:
        from tools.engineering.prompt_history import record_prompt_execution
        from tools.engineering.storage import record_execution_dismissal

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            run_id = "inbox-dismissed-projection"
            record_prompt_execution(root, run_id=run_id, terminal_state="BLOCKED", prompt_title="Historical", executed_at="2026-08-18T12:00:00Z")
            record_execution_dismissal(root, run_id=run_id, terminal_state="BLOCKED", dismissed_at="2026-08-18T12:01:00Z", dismissed_by="dashboard_operator")
            (status / "status.json").write_text(json.dumps({
                "watcher_state": "WAITING_FOR_PREDECESSOR", "queue_items": [{"filename": "deferred-benchmark.md"}],
                "blocking_predecessor_run": run_id, "blocking_predecessor_phase": "BLOCKED",
            }), encoding="utf-8")

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload["watcher_state"], "WATCHER_IDLE")
        self.assertIsNone(payload["blocking_predecessor_run"])
        self.assertEqual(payload["queue_items"], [{"filename": "deferred-benchmark.md"}])

    def test_status_projects_stale_liveness_without_claiming_an_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "status.json").write_text(
                json.dumps({"watcher_state": "RUNNER_STARTING", "run_id": "inbox-stale"}),
                encoding="utf-8",
            )
            (status / "current.json").write_text(
                json.dumps({"run_id": "inbox-stale", "phase": "EXECUTE_AGENT"}),
                encoding="utf-8",
            )
            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState("inbox-stale", "repo", "prompt.md", "EXECUTE_AGENT")
            )

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload["watcher_state"], "ENGINEERING_RUN_STALE")
        self.assertEqual(payload["execution_liveness"]["state"], "STALE")
        self.assertNotEqual(payload["current_action"], "Engineeringuitvoering is actief.")

    def test_status_keeps_later_lifecycle_phase_visible_when_watcher_lags_merge_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            watcher = {
                "watcher_state": "WAITING_FOR_OPERATOR_MERGE",
                "run_id": "inbox-finalizing",
                "current_phase": "WAIT_FOR_OPERATOR_MERGE",
                "current_action": "Waiting for the operator to merge the pull request.",
            }
            (status / "status.json").write_text(json.dumps(watcher), encoding="utf-8")
            (status / "current.json").write_text(json.dumps({
                "run_id": "inbox-finalizing", "phase": "REPAIR_AGENT", "pull_request": 896,
            }), encoding="utf-8")
            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState(
                    "inbox-finalizing", "repo", "prompt.md", "REPAIR_AGENT",
                    transaction_kind="FINALIZATION", implementation_pull_request=895,
                    finalization_pull_request=896, pull_request=896,
                )
            )

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload["watcher_state"], "ENGINEERING_RUN_STALE")
        self.assertEqual(payload["current_phase"], "REPAIR_AGENT")
        self.assertEqual(payload["lifecycle"]["current_step"], "REPAIR_AGENT")
        self.assertNotIn("merge the pull request", payload["current_action"])

    def test_status_ignores_a_stale_nonterminal_live_projection_after_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            watcher = {"watcher_state": "WATCHER_IDLE", "run_id": None, "last_executed_run": "inbox-done"}
            (status / "status.json").write_text(json.dumps(watcher), encoding="utf-8")
            (status / "current.json").write_text(
                json.dumps({"run_id": "inbox-done", "phase": "EXECUTE_AGENT"}), encoding="utf-8"
            )
            checkpoint = root / ".engineering" / "engineering-runs"
            checkpoint.mkdir(parents=True)
            (checkpoint / "inbox-done.json").write_text(json.dumps({"phase": "COMPLETE"}), encoding="utf-8")

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload, watcher)

    def test_status_ignores_a_stale_nonterminal_live_projection_after_watcher_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            watcher = {
                "watcher_state": "JOB_FAILED",
                "run_id": None,
                "last_executed_run": "inbox-failed",
                "last_executed_phase": "FAILED",
            }
            (status / "status.json").write_text(json.dumps(watcher), encoding="utf-8")
            (status / "current.json").write_text(
                json.dumps({"run_id": "inbox-failed", "phase": "WAIT_FOR_TERMINAL_EVIDENCE"}),
                encoding="utf-8",
            )

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload, watcher)

    def test_status_ignores_a_live_projection_for_a_different_watcher_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            watcher = {"watcher_state": "RUNNER_STARTING", "run_id": "inbox-current"}
            (status / "status.json").write_text(json.dumps(watcher), encoding="utf-8")
            (status / "current.json").write_text(
                json.dumps({"run_id": "inbox-stale", "phase": "INITIALIZE"}),
                encoding="utf-8",
            )

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload, watcher)

    def test_status_prefers_a_live_lease_over_an_idle_watcher_for_an_older_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            watcher = {
                "watcher_state": "WATCHER_IDLE",
                "run_id": None,
                "last_executed_run": "inbox-older",
                "last_executed_phase": "BLOCKED",
            }
            (status / "status.json").write_text(json.dumps(watcher), encoding="utf-8")
            (status / "current.json").write_text(
                json.dumps({"run_id": "inbox-live", "phase": "EXECUTE_AGENT"}), encoding="utf-8"
            )
            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState("inbox-live", "repo", "prompt.md", "EXECUTE_AGENT")
            )
            acquire(root, "inbox-live", identity="test-host", instance_id="test-instance")

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(payload["run_id"], "inbox-live")

    def test_status_keeps_pr_handoff_visible_while_checks_are_polled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "status.json").write_text(
                json.dumps({"watcher_state": "WATCHER_IDLE", "run_id": None}), encoding="utf-8"
            )
            (status / "current.json").write_text(
                json.dumps(
                    {
                        "run_id": "inbox-pr-checks",
                        "phase": "WAIT_FOR_TERMINAL_EVIDENCE",
                        "pull_request": 840,
                        "transaction_kind": "IMPLEMENTATION",
                    }
                ),
                encoding="utf-8",
            )
            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState(
                    "inbox-pr-checks", "pcvantol/djconnect", "prompt.md",
                    "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=840,
                )
            )
            acquire(root, "inbox-pr-checks", identity="test-host", instance_id="test-instance")

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload["watcher_state"], "WAITING_FOR_OPERATOR_MERGE")
        self.assertEqual(payload["current_phase"], "WAIT_FOR_OPERATOR_MERGE")
        self.assertEqual(payload["pull_request"], 840)

    def test_status_recovers_bounded_prompt_context_for_a_restarted_merge_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "implementation.md"
            prompt.write_text("# Restore the merge hand-off\n\nPrivate prompt body", encoding="utf-8")
            status = root / ".engineering" / "status"
            status.mkdir(parents=True)
            (status / "status.json").write_text(
                json.dumps({"watcher_state": "WAITING_FOR_OPERATOR_MERGE", "run_id": "inbox-prompt"}),
                encoding="utf-8",
            )
            (status / "current.json").write_text(
                json.dumps({"run_id": "inbox-prompt", "phase": "WAIT_FOR_OPERATOR_MERGE", "pull_request": 841}),
                encoding="utf-8",
            )
            StateStore(root / ".engineering" / "engineering-runs").save(
                TransactionState(
                    "inbox-prompt", "pcvantol/djconnect", str(prompt), "WAIT_FOR_OPERATOR_MERGE",
                    pull_request=841, finalization_pull_request=841, transaction_kind="FINALIZATION",
                )
            )
            acquire(root, "inbox-prompt", identity="test-host", instance_id="test-instance")

            payload = json.loads(dashboard_state.status(root))

        self.assertEqual(payload["submitted_filename"], "implementation.md")
        self.assertEqual(payload["prompt_title"], "Restore the merge hand-off")

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
                process_metrics_reader=lambda _: b"{}",
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
