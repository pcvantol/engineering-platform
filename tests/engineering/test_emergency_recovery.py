from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engineering_platform import emergency_recovery
from engineering_platform.prompt_history import prompt_history, record_prompt_execution
from engineering_platform.storage import record_emergency_recovery


BASE = "a" * 40
STATE = SimpleNamespace(
    pull_request=None, implementation_pull_request=None,
    finalization_pull_request=None, reconciliation_pull_request=None,
)


class EmergencyRecoveryTest(unittest.TestCase):
    def test_central_recovery_rejects_missing_or_cross_project_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "engineering.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE ep_execution_runs(run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO ep_execution_runs(run_id,project_id) VALUES(?,?)",
                    ("inbox-abcdef12", "project-beta"),
                )
            with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "geen geldig project"):
                emergency_recovery._require_central_project_ownership(database, None, "inbox-abcdef12")
            with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "behoort niet"):
                emergency_recovery._require_central_project_ownership(database, "project-alpha", "inbox-abcdef12")
    def test_preview_requires_an_exact_live_run_with_a_clean_baseline(self) -> None:
        live = {
            "run_id": "inbox-abcdef12",
            "execution_mode": "MANAGED",
            "workspace_recovery": {
                "baseline_branch": "main",
                "baseline_head": BASE,
                "baseline_clean": True,
                "preexisting_branches": ["main"],
            },
        }
        with (
            patch.object(emergency_recovery, "load_projection", return_value=live),
            patch.object(emergency_recovery, "liveness", return_value={"state": "LIVE"}),
            patch.object(emergency_recovery.StateStore, "load", return_value=STATE),
            patch.object(emergency_recovery, "_git", side_effect=["codex/new-work", BASE]),
            patch.object(emergency_recovery, "_runner", return_value=(123, 456)),
            patch.object(emergency_recovery, "_host_pid", return_value=789),
            patch.object(emergency_recovery, "_process_command", side_effect=["codex exec", "python -m engineering_platform.execution_host"]),
        ):
            preview = emergency_recovery.preview(Path("/workspace"), "inbox-abcdef12")

        self.assertEqual(
            preview,
            {
                "available": True,
                "run_id": "inbox-abcdef12",
                "branch": "codex/new-work",
                "baseline_branch": "main",
                "rollback_changes": True,
                "remove_branch": True,
            },
        )

    def test_preview_refuses_a_branch_with_commits(self) -> None:
        live = {
            "run_id": "inbox-abcdef12",
            "execution_mode": "MANAGED",
            "workspace_recovery": {
                "baseline_branch": "main",
                "baseline_head": BASE,
                "baseline_clean": True,
                "preexisting_branches": ["main"],
            },
        }
        with (
            patch.object(emergency_recovery, "load_projection", return_value=live),
            patch.object(emergency_recovery, "liveness", return_value={"state": "LIVE"}),
            patch.object(emergency_recovery.StateStore, "load", return_value=STATE),
            patch.object(emergency_recovery, "_git", side_effect=["codex/new-work", "b" * 40]),
        ):
            preview = emergency_recovery.preview(Path("/workspace"), "inbox-abcdef12")

        self.assertEqual(preview, {"available": False})

    def test_preview_refuses_a_run_that_already_has_a_pull_request(self) -> None:
        live = {"run_id": "inbox-abcdef12", "execution_mode": "MANAGED"}
        state = SimpleNamespace(
            pull_request=999, implementation_pull_request=None,
            finalization_pull_request=None, reconciliation_pull_request=None,
        )
        with (
            patch.object(emergency_recovery, "load_projection", return_value=live),
            patch.object(emergency_recovery, "liveness", return_value={"state": "LIVE"}),
            patch.object(emergency_recovery.StateStore, "load", return_value=state),
        ):
            preview = emergency_recovery.preview(Path("/workspace"), "inbox-abcdef12")

        self.assertEqual(preview, {"available": False})

    def test_preview_remains_available_while_the_host_has_no_codex_child(self) -> None:
        live = {
            "run_id": "inbox-abcdef12", "execution_mode": "MANAGED",
            "workspace_recovery": {"baseline_branch": "main", "baseline_head": BASE, "baseline_clean": True, "preexisting_branches": ["main"]},
        }
        with (
            patch.object(emergency_recovery, "load_projection", return_value=live),
            patch.object(emergency_recovery, "liveness", return_value={"state": "LIVE"}),
            patch.object(emergency_recovery.StateStore, "load", return_value=STATE),
            patch.object(emergency_recovery, "_git", side_effect=["main", BASE]),
            patch.object(emergency_recovery, "_runner", return_value=None),
            patch.object(emergency_recovery, "_host_pid", return_value=789),
            patch.object(emergency_recovery, "_process_command", return_value="python -m engineering_platform.execution_host"),
        ):
            preview = emergency_recovery.preview(Path("/workspace"), "inbox-abcdef12")

        self.assertTrue(preview["available"])
        self.assertFalse(preview["remove_branch"])

    def test_emergency_recovery_never_runs_git_rollback_before_the_host_stops(self) -> None:
        plan = emergency_recovery.RecoveryPlan("inbox-abcdef12", "codex/new-work", "main", BASE, 456, 789)
        with (
            patch.object(emergency_recovery, "_plan", return_value=plan),
            patch.object(emergency_recovery, "_stop", side_effect=emergency_recovery.EmergencyRecoveryError("host remains live")),
            patch.object(emergency_recovery, "_git") as git,
        ):
            with self.assertRaisesRegex(emergency_recovery.EmergencyRecoveryError, "host remains live"):
                emergency_recovery.execute(Path("/workspace"), "inbox-abcdef12")

        git.assert_not_called()

    def test_historical_prompt_projects_emergency_stop_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_prompt_execution(
                root, run_id="inbox-abcdef12", terminal_state="FAILED",
                prompt_title="Interrupted work", executed_at="2026-08-24T20:00:00+00:00",
            )
            record_emergency_recovery(
                root, run_id="inbox-abcdef12", cancelled_at="2026-08-24T20:01:00+00:00",
                rolled_back=True, removed_branch="codex/new-work",
            )
            history = prompt_history(root)

        self.assertEqual(history[0]["status"], "CANCELLED")
        self.assertEqual(history[0]["emergency_cancelled_at"], "2026-08-24T20:01:00+00:00")
