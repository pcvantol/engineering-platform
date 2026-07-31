from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import json

from tools.engineering import inbox_watcher


class InboxWatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "cloud"
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "tools/engineering").mkdir(parents=True)
        (self.repo / "tools/engineering/dj-engineer").write_text("#!/bin/sh\n", encoding="utf-8")
        inbox = inbox_watcher.folders(self.root)["Inbox"]
        self.inbox = inbox

    def tearDown(self) -> None: self.temp.cleanup()

    def test_preflight_failure_keeps_the_specific_bounded_runner_reason(self) -> None:
        completed = subprocess.CompletedProcess(("dj-engineer",), 2, "BLOCKED: working tree is not clean\n", "")

        self.assertEqual(
            inbox_watcher._runner_failure_detail(completed),
            "BLOCKED: working tree is not clean",
        )

    def test_filename_neutral_markdown_prompts_are_discovered_oldest_first(self) -> None:
        oldest = self.inbox / "first-submission"
        newest = self.inbox / "project-brief.upload"
        oldest.write_text("# First prompt", encoding="utf-8")
        newest.write_text("# Second prompt", encoding="utf-8")
        base = time.time_ns()
        os.utime(oldest, ns=(base, base))
        os.utime(newest, ns=(base + 1_000_000, base + 1_000_000))
        self.assertEqual(
            [path.name for path in inbox_watcher.discover(self.root, 0)],
            ["first-submission", "project-brief.upload"],
        )

    def test_rejects_empty_hidden_binary_and_non_markdown_unknown_input(self) -> None:
        (self.inbox / "empty.md").write_text("", encoding="utf-8")
        (self.inbox / ".partial.txt").write_text("text", encoding="utf-8")
        (self.inbox / "binary.md").write_bytes(b"\xff")
        (self.inbox / "other.upload").write_text("plain opaque data", encoding="utf-8")
        self.assertEqual(inbox_watcher.discover(self.root, 0), [])

    def test_existing_txt_prompts_remain_compatible(self) -> None:
        (self.inbox / "legacy.txt").write_text("plain prompt text", encoding="utf-8")
        self.assertEqual([path.name for path in inbox_watcher.discover(self.root, 0)], ["legacy.txt"])

    def test_launch_path_preserves_codex_location(self) -> None:
        with patch("tools.engineering.inbox_watcher.shutil.which", return_value="/opt/homebrew/bin/codex"):
            self.assertEqual(inbox_watcher.launch_path().split(":")[0], "/opt/homebrew/bin")

    def test_terminal_checkpoint_overrides_stale_live_status(self) -> None:
        status = self.repo / ".djconnect/status"
        checkpoint = self.repo / ".djconnect/engineering-runs"
        status.mkdir(parents=True)
        checkpoint.mkdir(parents=True)
        (status / "current.json").write_text('{"run_id":"inbox-stale","phase":"INITIALIZE"}', encoding="utf-8")
        (checkpoint / "inbox-stale.json").write_text('{"phase":"BLOCKED"}', encoding="utf-8")
        self.assertFalse(inbox_watcher._active_transaction(self.repo))

    def test_contradictory_terminal_report_is_not_accepted_for_delivery(self) -> None:
        report = self.repo / "contradictory.md"
        report.write_text("- Terminal state: `BLOCKED`\nCOMPLETE — delivered\n", encoding="utf-8")
        self.assertFalse(inbox_watcher._report_matches_terminal_phase(report, "BLOCKED"))
        corrected = inbox_watcher._corrected_terminal_report("inbox-blocked", "BLOCKED", "Target is dirty.")
        self.assertIn("BLOCKED — no engineering changes were executed or delivered.", corrected)
        self.assertNotIn("COMPLETE —", corrected)

    def test_corrected_complete_report_matches_a_complete_checkpoint(self) -> None:
        report = self.repo / "corrected-complete.md"
        report.write_text(
            inbox_watcher._corrected_terminal_report("inbox-complete", "COMPLETE", None),
            encoding="utf-8",
        )
        self.assertTrue(inbox_watcher._report_matches_terminal_phase(report, "COMPLETE"))

    def test_complete_job_is_serialized_and_archived(self) -> None:
        (self.inbox / "job.txt").write_text("# prompt", encoding="utf-8")
        run_id = "inbox-0cff9d624c2412db"
        report_dir = self.repo / ".djconnect/reports"
        report_dir.mkdir(parents=True)
        (report_dir / f"report_{run_id}.md").write_text("# report", encoding="utf-8")
        checkpoint = self.repo / ".djconnect/engineering-runs"
        checkpoint.mkdir(parents=True)
        (checkpoint / f"{run_id}.json").write_text(json.dumps({"phase": "COMPLETE"}), encoding="utf-8")
        old_log = self.repo / ".djconnect/logs/codex" / f"{run_id}.log"
        old_log.parent.mkdir(parents=True)
        old_log.write_text("previous attempt", encoding="utf-8")
        with patch("tools.engineering.inbox_watcher.subprocess.run") as run:
            run.return_value = __import__("subprocess").CompletedProcess((), 0)
            code = inbox_watcher.once(self.repo, self.root, 0)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0][-2:], ["--run-id", run_id])
        self.assertEqual(len(list(inbox_watcher.folders(self.root)["Completed"].glob("*__job.txt"))), 1)
        self.assertEqual(len(list(inbox_watcher.folders(self.root)["Reports"].glob("*.md"))), 1)
        snapshot = json_status(self.root)
        self.assertEqual(snapshot["watcher_state"], "JOB_COMPLETED")
        self.assertEqual(snapshot["last_executed_filename"], "job.txt")
        self.assertEqual(snapshot["last_executed_title"], "prompt")
        self.assertEqual(snapshot["last_executed_run"], run_id)
        self.assertFalse(old_log.exists())


def json_status(root: Path) -> dict[str, object]:
    import json
    return json.loads((root / "status.json").read_text(encoding="utf-8"))
