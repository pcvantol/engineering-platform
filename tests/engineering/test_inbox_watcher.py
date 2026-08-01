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

    def test_queue_projection_contains_only_filename_title_and_modified_time(self) -> None:
        prompt = self.inbox / "queued.md"
        prompt.write_text("# Queue title\nSensitive prompt body", encoding="utf-8")
        items = inbox_watcher._queue_items([(prompt, prompt.read_text(encoding="utf-8"))])

        self.assertEqual(items[0]["filename"], "queued.md")
        self.assertEqual(items[0]["title"], "Queue title")
        self.assertIn("T", items[0]["modified_at"])
        self.assertNotIn("Sensitive prompt body", str(items))

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
        self.assertEqual(len(list(inbox_watcher.local_folders(self.repo)["Completed"].glob("*__job.txt"))), 1)
        self.assertFalse((self.root / "Reports").exists())
        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "JOB_COMPLETED")
        self.assertEqual(snapshot["last_executed_filename"], "job.txt")
        self.assertEqual(snapshot["last_executed_title"], "prompt")
        self.assertEqual(snapshot["last_executed_run"], run_id)
        self.assertEqual(snapshot["last_executed_phase"], "COMPLETE")
        self.assertFalse(old_log.exists())

    def test_status_helpers_keep_previous_context_and_bound_details(self) -> None:
        status = self.repo / ".djconnect" / "status"
        status.mkdir(parents=True)
        (status / "status.json").write_text(
            json.dumps({"submitted_filename": "old.md", "last_executed_run": "inbox-old"}),
            encoding="utf-8",
        )
        self.assertEqual(
            inbox_watcher._previous_prompt_context(self.repo),
            {"submitted_filename": "old.md", "last_executed_run": "inbox-old"},
        )
        self.assertEqual(inbox_watcher._safe_detail("line one\nline two"), "line one line two")
        self.assertEqual(inbox_watcher._prompt_title("no title", "fallback.md"), "fallback.md")
        self.assertEqual(inbox_watcher._prompt_title("# Visible title\nbody", "fallback.md"), "Visible title")

    def test_lock_recovers_only_stale_owner_and_prevents_parallel_owner(self) -> None:
        with inbox_watcher._lock(self.repo):
            with self.assertRaisesRegex(RuntimeError, "another watcher"):
                with inbox_watcher._lock(self.repo):
                    pass
        lock = self.repo / ".djconnect/engineering-inbox.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("99999999", encoding="utf-8")
        with inbox_watcher._lock(self.repo):
            self.assertTrue(lock.exists())
        self.assertFalse(lock.exists())

    def test_runner_evidence_helpers_and_empty_inbox_are_safe(self) -> None:
        self.assertEqual(inbox_watcher._runner_result(self.repo, "inbox-none"), (None, None))
        self.assertIsNone(inbox_watcher._report(self.repo, "inbox-none"))
        self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)
        self.assertEqual(json_status(self.repo)["watcher_state"], "WATCHER_IDLE")
        runs = self.repo / ".djconnect/engineering-runs"
        runs.mkdir(parents=True)
        (runs / "inbox-evidence.json").write_text(
            '{"phase":"FAILED","diagnostic":"bounded"}', encoding="utf-8"
        )
        self.assertEqual(inbox_watcher._runner_result(self.repo, "inbox-evidence"), ("FAILED", "bounded"))

    @patch("tools.engineering.inbox_watcher.LaunchdProvider")
    def test_main_install_uninstall_status_and_doctor_are_local_only(self, launchd: object) -> None:
        with tempfile.TemporaryDirectory() as home, patch(
            "tools.engineering.inbox_watcher.Path.home", return_value=Path(home)
        ), patch("tools.engineering.inbox_watcher.PlatformConfiguration.load"):
            (self.repo / ".gitignore").write_text(".djconnect/\n", encoding="utf-8")
            self.assertEqual(
                inbox_watcher.main(["install", "--repo", str(self.repo), "--icloud-root", str(self.root)]),
                0,
            )
            launchd.return_value.install.assert_called_once()
            self.assertEqual(
                inbox_watcher.main(["status", "--repo", str(self.repo), "--icloud-root", str(self.root)]),
                0,
            )
            self.assertEqual(
                inbox_watcher.main(["uninstall", "--repo", str(self.repo), "--icloud-root", str(self.root)]),
                0,
            )
            launchd.return_value.uninstall.assert_called_once()
            self.assertEqual(
                inbox_watcher.main(["doctor", "--repo", str(self.repo), "--icloud-root", str(self.root)]),
                1,
            )

    def test_blocked_predecessor_holds_later_inbox_prompts_without_claiming_them(self) -> None:
        (self.inbox / "next.txt").write_text("# Later prompt", encoding="utf-8")
        status_directory = self.repo / ".djconnect" / "status"
        status_directory.mkdir(parents=True)
        (status_directory / "status.json").write_text(
            json.dumps(
                {
                    "last_executed_run": "inbox-blocked123",
                    "last_executed_phase": "BLOCKED",
                    "last_executed_filename": "blocked.txt",
                    "last_executed_title": "Blocked predecessor",
                }
            ),
            encoding="utf-8",
        )

        with patch("tools.engineering.inbox_watcher.subprocess.run") as run:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        snapshot = json_status(self.repo)
        run.assert_not_called()
        self.assertTrue((self.inbox / "next.txt").exists())
        self.assertEqual(snapshot["watcher_state"], "WAITING_FOR_PREDECESSOR")
        self.assertEqual(snapshot["current_phase"], "WAITING_FOR_PREDECESSOR")
        self.assertEqual(snapshot["blocking_predecessor_run"], "inbox-blocked123")
        self.assertEqual(snapshot["blocking_predecessor_title"], "Blocked predecessor")
        self.assertIn("Retry-Of: inbox-blocked123", snapshot["predecessor_recovery_action"])
        self.assertEqual(snapshot["queue_items"][0]["filename"], "next.txt")
        self.assertEqual(snapshot["queue_items"][0]["title"], "Later prompt")

    def test_explicit_retry_of_blocked_predecessor_precedes_later_prompts(self) -> None:
        later = self.inbox / "later.txt"
        later.write_text("# Later prompt", encoding="utf-8")
        retry = self.inbox / "retry.txt"
        retry_content = "Retry-Of: inbox-blocked123\n# Corrected predecessor"
        retry.write_text(retry_content, encoding="utf-8")
        now = time.time_ns()
        os.utime(later, ns=(now, now))
        os.utime(retry, ns=(now + 1_000_000, now + 1_000_000))
        status_directory = self.repo / ".djconnect" / "status"
        status_directory.mkdir(parents=True)
        (status_directory / "status.json").write_text(
            json.dumps(
                {
                    "last_executed_run": "inbox-blocked123",
                    "last_executed_phase": "FAILED",
                    "last_executed_filename": "blocked.txt",
                    "last_executed_title": "Blocked predecessor",
                }
            ),
            encoding="utf-8",
        )
        _, retry_run_id, _ = inbox_watcher._job_id(retry, retry_content)
        checkpoint = self.repo / ".djconnect/engineering-runs"
        checkpoint.mkdir(parents=True)
        (checkpoint / f"{retry_run_id}.json").write_text(json.dumps({"phase": "COMPLETE"}), encoding="utf-8")
        report_dir = self.repo / ".djconnect/reports"
        report_dir.mkdir(parents=True)
        (report_dir / f"report_{retry_run_id}.md").write_text(
            inbox_watcher._corrected_terminal_report(retry_run_id, "COMPLETE", None),
            encoding="utf-8",
        )

        with patch("tools.engineering.inbox_watcher.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess((), 0)
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        self.assertEqual(run.call_args.args[0][-1], retry_run_id)
        self.assertTrue(later.exists())
        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "JOB_COMPLETED")
        self.assertEqual(snapshot["last_executed_run"], retry_run_id)
        self.assertIsNone(snapshot["blocking_predecessor_run"])
        self.assertIsNone(snapshot["predecessor_recovery_action"])

    def test_migration_moves_legacy_archives_and_removes_iCloud_status(self) -> None:
        (self.root / "Completed").mkdir()
        (self.root / "Reports").mkdir()
        (self.root / "Completed" / "old.txt").write_text("# old", encoding="utf-8")
        (self.root / "Reports" / "old.md").write_text("# report", encoding="utf-8")
        (self.root / "status.json").write_text('{"watcher_state":"WATCHER_IDLE"}', encoding="utf-8")
        migrated = inbox_watcher.migrate_icloud_archives(self.repo, self.root)
        self.assertEqual(migrated, {"moved": 3, "deleted_duplicates": 0})
        self.assertTrue((self.repo / ".djconnect" / "inbox" / "Completed" / "old.txt").exists())
        self.assertTrue((self.repo / ".djconnect" / "reports" / "old.md").exists())
        self.assertTrue((self.repo / ".djconnect" / "status" / "status.json").exists())
        self.assertFalse((self.root / "Completed").exists())
        self.assertFalse((self.root / "Reports").exists())
        self.assertFalse((self.root / "status.json").exists())


def json_status(repo: Path) -> dict[str, object]:
    import json
    return json.loads((repo / ".djconnect" / "status" / "status.json").read_text(encoding="utf-8"))
