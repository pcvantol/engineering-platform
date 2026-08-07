from __future__ import annotations

from contextlib import nullcontext
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tools.engineering import inbox_watcher
from tools.engineering.host_preflight import HostPreflightResult
from tools.engineering.workspace_preflight import WorkspacePreflightResult
from tools.engineering.capability_preflight import CapabilityPreflightResult
from tools.engineering.telemetry import wait_for_pending_telemetry


class InboxWatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "cloud"
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "tools/engineering").mkdir(parents=True)
        (self.repo / "tools/engineering/engineering-execution-host").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.repo / "tools/engineering/ENGINEERING_PLATFORM_CONFIG.json").write_text(
            (Path(__file__).resolve().parents[2] / "tools/engineering/ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.preflight = patch(
            "tools.engineering.inbox_watcher.execute_host_preflight",
            return_value=HostPreflightResult("PASS", "Engineering Platform", "1.5.0", "2026.12", "now", 1, ()),
        )
        self.preflight.start()
        self.workspace_preflight = patch(
            "tools.engineering.inbox_watcher.execute_workspace_preflight",
            return_value=WorkspacePreflightResult("PASS", "DJConnect", "repo", "main", "MANAGED", "now", 1, ()),
        )
        self.workspace_preflight.start()
        self.capability_preflight = patch(
            "tools.engineering.inbox_watcher.execute_capability_preflight",
            return_value=CapabilityPreflightResult("PASS", "now", 1, (), "RETRYABLE", None, "Capability admission passed."),
        )
        self.capability_preflight.start()
        inbox = inbox_watcher.folders(self.root)["Inbox"]
        self.inbox = inbox

    def tearDown(self) -> None:
        self.preflight.stop()
        self.workspace_preflight.stop()
        self.capability_preflight.stop()
        wait_for_pending_telemetry()
        self.temp.cleanup()

    def test_preflight_failure_keeps_the_specific_bounded_runner_reason(self) -> None:
        completed = subprocess.CompletedProcess(("engineering-execution-host",), 2, "BLOCKED: working tree is not clean\n", "")

        self.assertEqual(
            inbox_watcher._runner_failure_detail(completed),
            "BLOCKED: working tree is not clean",
        )

    def test_watcher_run_logs_lifecycle_identity_on_orderly_shutdown(self) -> None:
        lifecycle_context = {
            "application_version": inbox_watcher.WATCHER_VERSION,
            "git_commit": "abc123def456",
            "launchd_label": inbox_watcher.LABEL,
            "launch_agent_path": "/tmp/inbox.plist",
        }
        with (
            patch("tools.engineering.inbox_watcher.provision_workspace"),
            patch("tools.engineering.inbox_watcher.cloud_root", return_value=self.root),
            patch(
                "tools.engineering.inbox_watcher.component_logger",
                return_value=logging.getLogger("test"),
            ) as logger,
            patch("tools.engineering.inbox_watcher.component_lifecycle_context", return_value=lifecycle_context),
            patch("tools.engineering.inbox_watcher.shutdown_signal_logging", return_value=nullcontext()),
            patch("tools.engineering.inbox_watcher.single_instance", return_value=nullcontext()),
            patch("tools.engineering.inbox_watcher.time.sleep", side_effect=KeyboardInterrupt),
            patch("tools.engineering.inbox_watcher.log_event") as log_event,
        ):
            self.assertEqual(
                inbox_watcher.main(["run", "--repo", str(self.repo), "--icloud-root", str(self.root)]),
                0,
            )

        self.assertEqual(logger.call_args_list[0].args, (self.repo.resolve(), "inbox"))
        self.assertEqual(log_event.call_args_list[0].args[2], "watcher_started")
        self.assertEqual(log_event.call_args_list[-1].args[2], "watcher_shutdown_completed")
        self.assertEqual(log_event.call_args_list[-1].kwargs["context"], lifecycle_context)

    def test_launch_agent_uses_a_shell_exec_launcher_for_the_selected_runtime(self) -> None:
        with patch("tools.engineering.inbox_watcher.Path.home", return_value=Path(self.temp.name)):
            agent = inbox_watcher.launch_agent(self.repo)

        rendered = agent.read_text(encoding="utf-8")
        self.assertIn("/bin/zsh", rendered)
        self.assertIn("-lc", rendered)
        self.assertIn("exec", rendered)
        self.assertIn(str(self.repo), rendered)
        self.assertNotIn("StandardOutPath", rendered)
        self.assertNotIn("StandardErrorPath", rendered)

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

    def test_queue_scan_and_admission_keep_the_oldest_prompt_together(self) -> None:
        oldest = self.inbox / "first.md"
        newest = self.inbox / "second.md"
        oldest.write_text("# First", encoding="utf-8")
        newest.write_text("# Second", encoding="utf-8")
        base = time.time_ns()
        os.utime(oldest, ns=(base, base))
        os.utime(newest, ns=(base + 1_000_000, base + 1_000_000))

        candidates = inbox_watcher._scan_queue(self.root, 0)
        admission = inbox_watcher._admit_queue_candidate(
            self.repo,
            candidates,
            child_run_id=None,
            child_job_id=None,
            logger=logging.getLogger("test"),
        )

        self.assertEqual([path.name for path, _ in candidates], ["first.md", "second.md"])
        self.assertEqual(admission.source, oldest)
        self.assertEqual(admission.content, "# First")

    def test_detached_runner_admission_targets_its_exact_job(self) -> None:
        prompt = self.inbox / "job.md"
        prompt.write_text("# Prompt", encoding="utf-8")
        candidates = inbox_watcher._scan_queue(self.root, 0)
        job_id, _, _ = inbox_watcher._job_id(prompt, "# Prompt")

        admission = inbox_watcher._admit_queue_candidate(
            self.repo,
            candidates,
            child_run_id="inbox-detached-run",
            child_job_id=job_id,
            logger=logging.getLogger("test"),
        )

        self.assertEqual(admission.source, prompt)
        self.assertEqual(admission.content, "# Prompt")
        self.assertEqual(admission.exit_code, 0)

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
        status = self.repo / ".engineering/status"
        checkpoint = self.repo / ".engineering/engineering-runs"
        status.mkdir(parents=True)
        checkpoint.mkdir(parents=True)
        (status / "current.json").write_text('{"run_id":"inbox-stale","phase":"INITIALIZE"}', encoding="utf-8")
        (checkpoint / "inbox-stale.json").write_text('{"phase":"BLOCKED"}', encoding="utf-8")
        self.assertFalse(inbox_watcher._active_transaction(self.repo))

    def test_terminal_workspace_snapshot_uses_the_genesis_checkout_and_git_index(self) -> None:
        target = self.repo.parent / "forge"
        checkpoint = self.repo / ".engineering" / "engineering-runs" / "inbox-snapshot.json"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(
            json.dumps(
                {
                    "execution_mode": "GENESIS",
                    "genesis_repository_path": str(target),
                }
            ),
            encoding="utf-8",
        )
        files = subprocess.CompletedProcess(("git",), 0, b"README.md\0src/app.py\0", b"")
        branch = subprocess.CompletedProcess(("git",), 0, "forge-phase-e", "")
        with patch("tools.engineering.inbox_watcher.subprocess.run", side_effect=(files, branch)):
            checkout, tracked_files, target_branch = inbox_watcher._terminal_workspace_snapshot(
                self.repo, "inbox-snapshot"
            )
        self.assertEqual(checkout, str(target.resolve()))
        self.assertEqual(tracked_files, 2)
        self.assertEqual(target_branch, "forge-phase-e")

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
        report_dir = self.repo / ".engineering/reports"
        report_dir.mkdir(parents=True)
        (report_dir / f"report_{run_id}.md").write_text("# report", encoding="utf-8")
        checkpoint = self.repo / ".engineering/engineering-runs"
        checkpoint.mkdir(parents=True)
        (checkpoint / f"{run_id}.json").write_text(json.dumps({"phase": "COMPLETE"}), encoding="utf-8")
        old_log = self.repo / ".engineering/logs/codex" / f"{run_id}.log"
        old_log.parent.mkdir(parents=True)
        old_log.write_text("previous attempt", encoding="utf-8")
        with patch("tools.engineering.inbox_watcher._allocate_run_id", return_value=run_id), patch("tools.engineering.inbox_watcher.subprocess.run") as run:
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

    def test_background_watcher_detaches_runner_and_keeps_admission_active(self) -> None:
        (self.inbox / "job.txt").write_text("# prompt", encoding="utf-8")
        run_id = "inbox-detached-run"
        with patch("tools.engineering.inbox_watcher._allocate_run_id", return_value=run_id), patch(
            "tools.engineering.inbox_watcher.subprocess.Popen"
        ) as popen:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0, background=True), 0)

        command = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        self.assertIn("tools.engineering.inbox_watcher", command)
        self.assertIn("once", command)
        self.assertEqual(environment[inbox_watcher.BACKGROUND_RUN_ID_ENVIRONMENT], run_id)
        self.assertTrue(environment[inbox_watcher.BACKGROUND_JOB_ID_ENVIRONMENT])
        self.assertTrue((self.inbox / "job.txt").exists())
        self.assertTrue(inbox_watcher._active_transaction(self.repo))
        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "RUNNER_STARTING")
        self.assertEqual(snapshot["run_id"], run_id)

    def test_active_detached_runner_keeps_scanning_and_publishes_later_inbox_prompts(self) -> None:
        (self.inbox / "running.txt").write_text("# Running prompt", encoding="utf-8")
        run_id = "inbox-detached-run"
        with patch("tools.engineering.inbox_watcher._allocate_run_id", return_value=run_id), patch(
            "tools.engineering.inbox_watcher.subprocess.Popen"
        ):
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0, background=True), 0)

        queued = self.inbox / "later.txt"
        queued.write_text("# Later prompt", encoding="utf-8")

        self.assertEqual(inbox_watcher.once(self.repo, self.root, 0, background=True), 0)

        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "RUNNER_STARTING")
        self.assertEqual(snapshot["run_id"], run_id)
        self.assertEqual(snapshot["queue_depth"], 2)
        self.assertEqual(
            {item["filename"] for item in snapshot["queue_items"]},
            {"later.txt", "running.txt"},
        )

    def test_detached_runner_execution_keeps_scanning_when_a_second_prompt_arrives(self) -> None:
        """The polling watcher must observe later Inbox work during a real child run."""
        running = self.inbox / "running.md"
        running.write_text("# Running prompt", encoding="utf-8")
        runner_started = threading.Event()
        release_runner = threading.Event()
        child_finished = threading.Event()
        child_results: list[int] = []

        def run_command(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(arguments[-2:], ["--run-id", "inbox-detached-run"])
            # The child reads its identity before invoking the host.  Removing
            # it here lets the concurrently polling parent stay a parent.
            os.environ.pop(inbox_watcher.BACKGROUND_RUN_ID_ENVIRONMENT, None)
            os.environ.pop(inbox_watcher.BACKGROUND_JOB_ID_ENVIRONMENT, None)
            runner_started.set()
            self.assertTrue(release_runner.wait(timeout=5))
            checkpoint = self.repo / ".engineering" / "engineering-runs" / "inbox-detached-run.json"
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_text(json.dumps({"phase": "COMPLETE"}), encoding="utf-8")
            report = self.repo / ".engineering" / "reports" / "report_inbox-detached-run.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                "- Terminal state: `COMPLETE`\nCOMPLETE — delivered\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        def detach_runner(*_: object, **kwargs: object) -> object:
            environment = dict(kwargs["env"])

            def execute_child() -> None:
                with patch.dict(os.environ, environment, clear=False):
                    child_results.append(inbox_watcher.once(self.repo, self.root, 0))
                child_finished.set()

            threading.Thread(target=execute_child, daemon=True).start()
            return object()

        with (
            patch("tools.engineering.inbox_watcher._allocate_run_id", return_value="inbox-detached-run"),
            patch("tools.engineering.inbox_watcher.subprocess.Popen", side_effect=detach_runner),
            patch("tools.engineering.inbox_watcher.subprocess.run", side_effect=run_command),
        ):
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0, background=True), 0)
            self.assertTrue(runner_started.wait(timeout=5))

            later = self.inbox / "later.md"
            later.write_text("# Later prompt", encoding="utf-8")
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0, background=True), 0)

            snapshot = json_status(self.repo)
            self.assertEqual(snapshot["run_id"], "inbox-detached-run")
            self.assertEqual(snapshot["queue_depth"], 1)
            self.assertEqual([item["filename"] for item in snapshot["queue_items"]], ["later.md"])

            release_runner.set()
            self.assertTrue(child_finished.wait(timeout=5))

        self.assertEqual(child_results, [0])
        self.assertTrue((self.inbox / "later.md").exists())

    def test_detached_runner_does_not_hold_the_polling_watcher_lock(self) -> None:
        lock_path = self.repo / ".engineering" / "engineering-inbox.lock"

        with patch.dict(
            os.environ,
            {inbox_watcher.BACKGROUND_RUN_ID_ENVIRONMENT: "inbox-detached-run"},
            clear=False,
        ):
            with inbox_watcher._lock(self.repo):
                self.assertFalse(lock_path.exists())

        self.assertFalse(lock_path.exists())

    def test_preflight_failure_delivers_a_terminal_report_and_failed_phase(self) -> None:
        prompt = self.inbox / "preflight.md"
        prompt.write_text("# Preflight prompt", encoding="utf-8")
        _, run_id, _ = inbox_watcher._job_id(prompt, prompt.read_text(encoding="utf-8"))
        with patch("tools.engineering.inbox_watcher._allocate_run_id", return_value=run_id), patch("tools.engineering.inbox_watcher.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ("engineering-execution-host",), 2, "", "Engineering Platform upgrade required."
            )
            code = inbox_watcher.once(self.repo, self.root, 0)

        self.assertEqual(code, 2)
        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "JOB_FAILED")
        self.assertEqual(snapshot["last_executed_run"], run_id)
        self.assertEqual(snapshot["last_executed_phase"], "FAILED")
        report = self.repo / ".engineering" / "reports" / f"corrected_{run_id}.md"
        self.assertTrue(report.exists())
        self.assertTrue(inbox_watcher._report_matches_terminal_phase(report, "FAILED"))

    def test_failed_host_preflight_prevents_inbox_claim(self) -> None:
        prompt = self.inbox / "host-failure.md"
        prompt.write_text("# Host failure", encoding="utf-8")
        failed = HostPreflightResult("FAIL", "Engineering Platform", "1.5.0", "2026.12", "now", 1, ())
        with patch("tools.engineering.inbox_watcher.execute_host_preflight", return_value=failed), patch(
            "tools.engineering.inbox_watcher.subprocess.run"
        ) as run:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 1)
        self.assertTrue(prompt.exists())
        self.assertFalse(list(inbox_watcher.local_folders(self.repo)["Running"].iterdir()))
        run.assert_not_called()
        self.assertEqual(json_status(self.repo)["watcher_state"], "HOST_PREFLIGHT_FAILED")

    def test_failed_workspace_preflight_prevents_inbox_claim(self) -> None:
        prompt = self.inbox / "workspace-failure.md"
        prompt.write_text("# Workspace failure", encoding="utf-8")
        failed = WorkspacePreflightResult("FAIL", "DJConnect", "repo", "main", "MANAGED", "now", 1, ())
        with patch("tools.engineering.inbox_watcher.execute_workspace_preflight", return_value=failed), patch(
            "tools.engineering.inbox_watcher.subprocess.run"
        ) as run:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 1)
        self.assertTrue(prompt.exists())
        self.assertFalse(list(inbox_watcher.local_folders(self.repo)["Running"].iterdir()))
        run.assert_not_called()
        self.assertEqual(json_status(self.repo)["watcher_state"], "WORKSPACE_PREFLIGHT_FAILED")

    def test_failed_capability_preflight_prevents_inbox_claim(self) -> None:
        prompt = self.inbox / "capability-failure.md"
        prompt.write_text("# Capability failure", encoding="utf-8")
        failed = CapabilityPreflightResult("FAIL", "now", 1, (), "RETRYABLE_AFTER_HOST_REPAIR", "CAPABILITY", "Repair host.")
        with patch("tools.engineering.inbox_watcher.execute_capability_preflight", return_value=failed), patch(
            "tools.engineering.inbox_watcher.subprocess.run"
        ) as run:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 1)
        self.assertTrue(prompt.exists())
        self.assertFalse(list(inbox_watcher.local_folders(self.repo)["Running"].iterdir()))
        run.assert_not_called()
        self.assertEqual(json_status(self.repo)["watcher_state"], "CAPABILITY_PREFLIGHT_FAILED")

    def test_status_helpers_keep_previous_context_and_bound_details(self) -> None:
        status = self.repo / ".engineering" / "status"
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
        lock = self.repo / ".engineering/engineering-inbox.lock"
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
        runs = self.repo / ".engineering/engineering-runs"
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
            (self.repo / ".gitignore").write_text(".engineering/\n", encoding="utf-8")
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
        status_directory = self.repo / ".engineering" / "status"
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
        status_directory = self.repo / ".engineering" / "status"
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
        checkpoint = self.repo / ".engineering/engineering-runs"
        checkpoint.mkdir(parents=True)
        (checkpoint / f"{retry_run_id}.json").write_text(json.dumps({"phase": "COMPLETE"}), encoding="utf-8")
        report_dir = self.repo / ".engineering/reports"
        report_dir.mkdir(parents=True)
        (report_dir / f"report_{retry_run_id}.md").write_text(
            inbox_watcher._corrected_terminal_report(retry_run_id, "COMPLETE", None),
            encoding="utf-8",
        )

        with patch("tools.engineering.inbox_watcher._allocate_run_id", return_value=retry_run_id), patch("tools.engineering.inbox_watcher.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess((), 0)
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        self.assertEqual(run.call_args.args[0][-1], retry_run_id)
        self.assertTrue(later.exists())
        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "JOB_COMPLETED")
        self.assertEqual(snapshot["last_executed_run"], retry_run_id)
        self.assertIsNone(snapshot["blocking_predecessor_run"])
        self.assertIsNone(snapshot["predecessor_recovery_action"])

    def test_owner_triggered_retry_resubmits_only_the_blocking_prompt(self) -> None:
        original = "# Blocked predecessor\n\nKeep this prompt intact."
        archived = inbox_watcher.local_folders(self.repo)["Failed"] / "blocked__blocked.txt"
        archived.write_text(original, encoding="utf-8")
        _, run_id, _ = inbox_watcher._job_id(archived, original)
        status_directory = self.repo / ".engineering" / "status"
        status_directory.mkdir(parents=True)
        (status_directory / "status.json").write_text(
            json.dumps(
                {
                    "last_executed_run": run_id,
                    "last_executed_phase": "FAILED",
                    "last_executed_filename": "blocked.txt",
                    "last_executed_title": "Blocked predecessor",
                }
            ),
            encoding="utf-8",
        )
        (self.inbox / "later.txt").write_text("# Later prompt", encoding="utf-8")

        outcome = inbox_watcher.submit_predecessor_retry(self.repo, self.root)

        submitted = self.inbox / outcome["filename"]
        self.assertTrue(submitted.is_file())
        retry = submitted.read_text(encoding="utf-8")
        self.assertIn(f"Retry-Of: {run_id}", retry)
        self.assertIn(original, retry)
        self.assertNotEqual(outcome["retry_run_id"], run_id)
        self.assertTrue((self.inbox / "later.txt").is_file())

    def test_owner_triggered_retry_refuses_duplicate_pending_resubmission(self) -> None:
        original = "# Blocked predecessor"
        archived = inbox_watcher.local_folders(self.repo)["Failed"] / "blocked__blocked.txt"
        archived.write_text(original, encoding="utf-8")
        _, run_id, _ = inbox_watcher._job_id(archived, original)
        status_directory = self.repo / ".engineering" / "status"
        status_directory.mkdir(parents=True)
        (status_directory / "status.json").write_text(
            json.dumps({"last_executed_run": run_id, "last_executed_phase": "BLOCKED"}),
            encoding="utf-8",
        )
        (self.inbox / "existing.md").write_text(f"Retry-Of: {run_id}\n# Existing retry", encoding="utf-8")
        (self.inbox / "later.md").write_text("# Dependent work", encoding="utf-8")

        with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "staat al in de wachtrij"):
            inbox_watcher.submit_predecessor_retry(self.repo, self.root)

    def test_queue_recovery_requires_waiting_dependent_work(self) -> None:
        original = "# Blocked predecessor"
        archived = inbox_watcher.local_folders(self.repo)["Failed"] / "blocked__blocked.txt"
        archived.write_text(original, encoding="utf-8")
        _, run_id, _ = inbox_watcher._job_id(archived, original)
        status_directory = self.repo / ".engineering" / "status"
        status_directory.mkdir(parents=True)
        (status_directory / "status.json").write_text(
            json.dumps({"last_executed_run": run_id, "last_executed_phase": "BLOCKED"}), encoding="utf-8"
        )
        with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "afhankelijke Inbox-werk"):
            inbox_watcher.submit_predecessor_retry(self.repo, self.root)

    def test_execution_retry_is_available_without_waiting_queue_and_records_lineage(self) -> None:
        original = "# Blocked prompt\n\nKeep this evidence immutable."
        archived = inbox_watcher.local_folders(self.repo)["Failed"] / "blocked__blocked.txt"
        archived.write_text(original, encoding="utf-8")
        _, run_id, _ = inbox_watcher._job_id(archived, original)
        checkpoint = self.repo / ".engineering" / "engineering-runs"
        checkpoint.mkdir(parents=True)
        (checkpoint / f"{run_id}.json").write_text(json.dumps({"phase": "BLOCKED"}), encoding="utf-8")

        outcome = inbox_watcher.submit_execution_retry(self.repo, self.root, run_id)

        retry = (self.inbox / str(outcome["filename"])).read_text(encoding="utf-8")
        self.assertIn(f"Retry-Of: {run_id}", retry)
        self.assertIn(f"Original-Run-ID: {run_id}", retry)
        self.assertIn("Retry-Generation: 1", retry)
        self.assertIn(original, retry)
        self.assertEqual(archived.read_text(encoding="utf-8"), original)
        self.assertNotEqual(outcome["retry_run_id"], run_id)

    def test_execution_retry_supports_failed_and_refuses_non_retryable_or_duplicate_execution(self) -> None:
        original = "# Failed prompt"
        archived = inbox_watcher.local_folders(self.repo)["Failed"] / "failed__failed.txt"
        archived.write_text(original, encoding="utf-8")
        _, run_id, _ = inbox_watcher._job_id(archived, original)
        checkpoint = self.repo / ".engineering" / "engineering-runs"
        checkpoint.mkdir(parents=True)
        (checkpoint / f"{run_id}.json").write_text(json.dumps({"phase": "FAILED"}), encoding="utf-8")
        outcome = inbox_watcher.submit_execution_retry(self.repo, self.root, run_id)
        self.assertTrue((self.inbox / str(outcome["filename"])).is_file())
        (checkpoint / f"{run_id}.json").write_text(json.dumps({"phase": "COMPLETE"}), encoding="utf-8")
        with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "geblokkeerde of mislukte"):
            inbox_watcher.submit_execution_retry(self.repo, self.root, run_id)
        (checkpoint / f"{run_id}.json").write_text(json.dumps({"phase": "BLOCKED"}), encoding="utf-8")
        with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "staat al in de wachtrij"):
            inbox_watcher.submit_execution_retry(self.repo, self.root, run_id)

    def test_dismiss_terminal_execution_clears_operational_state_and_preserves_audit(self) -> None:
        run_id = "inbox-dismissed"
        runs = self.repo / ".engineering" / "engineering-runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / f"{run_id}.json").write_text(json.dumps({"phase": "BLOCKED"}), encoding="utf-8")
        status = self.repo / ".engineering" / "status"
        status.mkdir(parents=True, exist_ok=True)
        (status / "status.json").write_text(json.dumps({"watcher_state": "JOB_BLOCKED", "last_executed_run": run_id, "last_executed_phase": "BLOCKED", "last_executed_title": "Blocked prompt", "queue_depth": 0, "queue_items": []}), encoding="utf-8")
        outcome = inbox_watcher.dismiss_execution(self.repo, run_id)
        self.assertTrue(outcome["dismissed"])
        self.assertEqual(json_status(self.repo)["watcher_state"], "WATCHER_IDLE")
        self.assertIsNone(json_status(self.repo)["last_executed_run"])
        audit = json.loads((status / "execution_dismissals.json").read_text(encoding="utf-8"))
        self.assertEqual(audit[-1]["run_id"], run_id)

    def test_migration_moves_legacy_archives_and_removes_iCloud_status(self) -> None:
        (self.root / "Completed").mkdir()
        (self.root / "Reports").mkdir()
        (self.root / "Completed" / "old.txt").write_text("# old", encoding="utf-8")
        (self.root / "Reports" / "old.md").write_text("# report", encoding="utf-8")
        (self.root / "status.json").write_text('{"watcher_state":"WATCHER_IDLE"}', encoding="utf-8")
        migrated = inbox_watcher.migrate_icloud_archives(self.repo, self.root)
        self.assertEqual(migrated, {"moved": 3, "deleted_duplicates": 0})
        self.assertTrue((self.repo / ".engineering" / "inbox" / "Completed" / "old.txt").exists())
        self.assertTrue((self.repo / ".engineering" / "reports" / "old.md").exists())
        self.assertTrue((self.repo / ".engineering" / "status" / "status.json").exists())
        self.assertFalse((self.root / "Completed").exists())
        self.assertFalse((self.root / "Reports").exists())
        self.assertFalse((self.root / "status.json").exists())


def json_status(repo: Path) -> dict[str, object]:
    import json
    return json.loads((repo / ".engineering" / "status" / "status.json").read_text(encoding="utf-8"))
