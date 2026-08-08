"""Regression coverage for the private SQLite prompt-history projection."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.engineering.prompt_history import (
    prompt_history,
    record_terminal_report,
    record_prompt_execution,
    report_for_prompt_history,
)
from tools.engineering.storage import record_submission


class PromptHistoryTest(unittest.TestCase):
    def test_projects_persisted_submission_and_execution_context_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_submission(
                root, submission_id="submission-history", producer_id="forge", producer_type="FORGE",
                producer_version="1.0", contract_version="1.0", prompt_content="prompt",
                prompt_metadata={}, target_identity={}, original_envelope="{}",
                received_at="2026-08-07T08:00:00Z", link_run_id="inbox-history",
                execution_context={"context_version": "1.0", "mission_title": "Aurora"},
            )
            record_prompt_execution(
                root, run_id="inbox-history", terminal_state="COMPLETE", prompt_title="History projection",
                executed_at="2026-08-07T09:00:00Z",
            )
            entry = prompt_history(root)[0]
            self.assertEqual(entry["submission_id"], "submission-history")
            self.assertEqual(entry["producer_submission_contract_version"], "1.0")
            self.assertEqual(entry["execution_context_version"], "1.0")
            self.assertEqual(entry["execution_context"], {"context_version": "1.0", "mission_title": "Aurora"})
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
                        "submission_id": None,
                        "producer_submission_contract_version": None,
                        "execution_context_version": None,
                        "execution_context": None,
                        "dismissed": False,
                        "handling_state": "OPEN",
                        "dismissed_at": None,
                        "dismissed_by": None,
                        "retry_child_run_id": None,
                        "retry_status": None,
                        "queued_retry_child": False,
                        "active_retry_child": False,
                        "can_retry": False,
                        "retry_chain": ["inbox-abc123"],
                        "current_active_run": "inbox-abc123",
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

    def test_terminal_report_reconciles_an_earlier_history_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / ".engineering" / "reports" / "complete.md"
            report.parent.mkdir(parents=True)
            report.write_text(
                "\n".join(
                    (
                        "# Engineering Report",
                        "# Engineering Platform Increment — Reconciled Producer Submission Envelope",
                        "- Objective: Reconciled Producer Submission Envelope",
                        "- Timestamp: 2026-08-08T08-06-11Z",
                        "- Run ID: `inbox-reconciled`",
                        "- Terminal state: `COMPLETE`",
                        "- Finalization Merge Commit: `abcdef1`",
                    )
                ),
                encoding="utf-8",
            )
            record_prompt_execution(
                root,
                run_id="inbox-reconciled",
                terminal_state="FAILED",
                prompt_title="Earlier incomplete projection",
                executed_at="2026-08-08T07:00:00Z",
            )

            record_terminal_report(root, report)

            entry = prompt_history(root)[0]
            self.assertEqual(entry["status"], "COMPLETE")
            self.assertEqual(entry["title"], "Reconciled Producer Submission Envelope")
            self.assertEqual(entry["executed_at"], "2026-08-08T08:06:11Z")
            self.assertEqual(entry["git_commit"], "abcdef1")
            self.assertTrue(entry["report_available"])

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
            parent = record_prompt_execution(
                root,
                run_id="inbox-original",
                terminal_state="BLOCKED",
                prompt_title="Original prompt",
                executed_at="2026-08-03T11:00:00Z",
            )
            self.assertIsNone(parent)
            entries = {item["run_id"]: item for item in prompt_history(root)}
            self.assertEqual(entries["inbox-original"]["retry_child_run_id"], "inbox-retry123")
            self.assertEqual(entries["inbox-original"]["current_active_run"], "inbox-retry123")
            self.assertEqual(entries["inbox-retry123"]["retry_chain"], ["inbox-original", "inbox-retry123"])

    def test_retry_action_projection_uses_queued_active_and_terminal_child_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_prompt_execution(
                root,
                run_id="inbox-parent",
                terminal_state="BLOCKED",
                prompt_title="Blocked parent",
                executed_at="2026-08-06T12:00:00Z",
            )
            parent = prompt_history(root)[0]
            self.assertTrue(parent["can_retry"])

            queued = prompt_history(
                root,
                queued_retry_children=[
                    {
                        "retry_of": "inbox-parent",
                        "status": "QUEUED",
                        "retry_timestamp": "2026-08-06T12:01:00Z",
                    }
                ],
            )[0]
            self.assertFalse(queued["can_retry"])
            self.assertTrue(queued["queued_retry_child"])
            self.assertIsNone(queued["retry_child_run_id"])
            self.assertEqual(queued["current_active_run"], "inbox-parent")

            job = root / ".engineering" / "inbox-processing" / "retry" / "job.json"
            job.parent.mkdir(parents=True)
            job.write_text(
                '{"run_id":"inbox-active-child","retry":{"retry_of":"inbox-parent","retry_timestamp":"2026-08-06T12:01:00Z"}}',
                encoding="utf-8",
            )
            status = root / ".engineering" / "status" / "status.json"
            status.parent.mkdir(parents=True)
            status.write_text('{"run_id":"inbox-active-child"}', encoding="utf-8")
            active = prompt_history(root)[0]
            self.assertFalse(active["can_retry"])
            self.assertTrue(active["active_retry_child"])
            self.assertEqual(active["retry_child_run_id"], "inbox-active-child")

            record_prompt_execution(
                root,
                run_id="inbox-terminal-child",
                terminal_state="COMPLETE",
                prompt_title="Retry child",
                executed_at="2026-08-06T12:02:00Z",
                retry_of="inbox-parent",
                original_run_id="inbox-parent",
                retry_generation=1,
                retry_timestamp="2026-08-06T12:01:00Z",
            )
            parent = {entry["run_id"]: entry for entry in prompt_history(root)}["inbox-parent"]
            self.assertFalse(parent["can_retry"])
            self.assertFalse(parent["queued_retry_child"])
            self.assertFalse(parent["active_retry_child"])
            self.assertEqual(parent["retry_child_run_id"], "inbox-terminal-child")

    def test_failed_terminal_run_without_child_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_prompt_execution(
                root,
                run_id="inbox-failed-parent",
                terminal_state="FAILED",
                prompt_title="Failed parent",
                executed_at="2026-08-06T12:00:00Z",
            )
            entry = prompt_history(root)[0]
            self.assertTrue(entry["can_retry"])
            self.assertIsNone(entry["retry_child_run_id"])
