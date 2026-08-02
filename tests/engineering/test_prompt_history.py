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
