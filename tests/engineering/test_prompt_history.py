"""Regression coverage for the private SQLite prompt-history projection."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.engineering.prompt_history import (
    prompt_history,
    record_prompt_execution,
    report_for_prompt_history,
)


class PromptHistoryTest(unittest.TestCase):
    def test_records_terminal_run_and_serves_only_its_local_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / ".engineering" / "reports" / "run.md"
            report.parent.mkdir(parents=True)
            report.write_text("# Engineering Report\n", encoding="utf-8")

            record_prompt_execution(
                root,
                run_id="inbox-abc123",
                terminal_state="COMPLETE",
                prompt_title="Een afgeronde prompt",
                executed_at="2026-08-02T14:00:00Z",
                report=report,
                git_commit="abc1234",
            )

            self.assertEqual(
                prompt_history(root),
                [
                    {
                        "run_id": "inbox-abc123",
                        "status": "COMPLETE",
                        "title": "Een afgeronde prompt",
                        "executed_at": "2026-08-02T14:00:00Z",
                        "git_commit": "abc1234",
                        "report_available": True,
                        "retry_of": None,
                        "original_run_id": None,
                        "retry_generation": None,
                        "retry_timestamp": None,
                        "target_checkout_path": None,
                        "tracked_file_count": None,
                        "target_branch": None,
                        "execution_mode": None,
                        "repository": None,
                        "producer_id": "legacy",
                        "producer_type": "HUMAN",
                        "producer_version": None,
                        "correlation_id": None,
                        "mission_id": None,
                        "engineering_action_id": None,
                        "execution_constraint_version": None,
                    }
                ],
            )
            self.assertEqual(report_for_prompt_history(root, "inbox-abc123"), b"# Engineering Report\n")
            self.assertIsNone(report_for_prompt_history(root, "../../not-a-run"))

    def test_backfills_legacy_report_and_normalizes_legacy_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / ".engineering" / "reports" / "legacy.md"
            report.parent.mkdir(parents=True)
            report.write_text(
                "\n".join(
                    (
                        "# Legacy prompt",
                        "- Objective: De oorspronkelijke prompttitel",
                        "- Timestamp: 2026-08-02T14-05-06Z",
                        "- Run ID: `inbox-legacy1`",
                        "- Terminal state: `BLOCKED`",
                        "- Genesis-commit: `abcdef1`",
                    )
                ),
                encoding="utf-8",
            )

            history = prompt_history(root)

            self.assertEqual(history[0]["title"], "De oorspronkelijke prompttitel")
            self.assertEqual(history[0]["status"], "BLOCKED")
            self.assertEqual(history[0]["executed_at"], "2026-08-02T14:05:06Z")
            self.assertEqual(history[0]["git_commit"], "abcdef1")
            self.assertTrue(history[0]["report_available"])

    def test_backfill_preserves_the_explicit_report_for_a_duplicate_legacy_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / ".engineering" / "reports"
            reports.mkdir(parents=True)
            actual = reports / "2026-08-03T19-40-44Z_inbox-duplicate.md"
            fallback = reports / "corrected_inbox-duplicate.md"
            actual.write_text(
                "# Engineering Report\n- Run ID: `inbox-duplicate`\n- Terminal state: `COMPLETE`\n",
                encoding="utf-8",
            )
            fallback.write_text(
                "# Engineering Report\n- Run ID: `inbox-duplicate`\n- Terminal state: `COMPLETE`\n",
                encoding="utf-8",
            )
            record_prompt_execution(
                root,
                run_id="inbox-duplicate",
                terminal_state="COMPLETE",
                prompt_title="Canonical report",
                executed_at="2026-08-03T19:40:44Z",
                report=actual,
            )

            self.assertEqual(prompt_history(root)[0]["report_available"], True)
            self.assertEqual(report_for_prompt_history(root, "inbox-duplicate"), actual.read_bytes())

    def test_rejects_non_terminal_or_invalid_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "terminal"):
                record_prompt_execution(
                    root,
                    run_id="../unsafe",
                    terminal_state="RUNNING",
                    prompt_title="unsafe",
                    executed_at="now",
                )

    def test_preserves_terminal_target_checkout_and_tracked_file_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "forge"
            checkout.mkdir()
            record_prompt_execution(
                root,
                run_id="inbox-workspace",
                terminal_state="COMPLETE",
                prompt_title="Workspace snapshot",
                executed_at="2026-08-04T12:00:00Z",
                target_checkout_path=checkout,
                tracked_file_count=1655,
                target_branch="forge-phase-evidence",
            )
            entry = prompt_history(root)[0]
            self.assertEqual(entry["target_checkout_path"], str(checkout.resolve()))
            self.assertEqual(entry["tracked_file_count"], 1655)
            self.assertEqual(entry["target_branch"], "forge-phase-evidence")

    def test_persists_retry_relationship_without_merging_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_prompt_execution(
                root,
                run_id="inbox-retry123",
                terminal_state="BLOCKED",
                prompt_title="Retry prompt",
                executed_at="2026-08-03T12:00:00Z",
                retry_of="inbox-original",
                original_run_id="inbox-original",
                retry_generation=1,
                retry_timestamp="2026-08-03T11:59:00Z",
            )
            entry = prompt_history(root)[0]
            self.assertEqual(entry["run_id"], "inbox-retry123")
            self.assertEqual(entry["retry_of"], "inbox-original")
            self.assertEqual(entry["original_run_id"], "inbox-original")
            self.assertEqual(entry["retry_generation"], 1)
