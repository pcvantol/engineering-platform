from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import ANY, patch

from engineering_platform import inbox_watcher
from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.host_preflight import HostPreflightCheck, HostPreflightResult
from engineering_platform.workspace_preflight import WorkspacePreflightCheck, WorkspacePreflightResult
from engineering_platform.capability_preflight import CapabilityCheck, CapabilityPreflightResult
from engineering_platform.execution_timing import phase_spans, timing_summary
from engineering_platform.execution_models import PullRequestEvidence
from engineering_platform.storage import open_storage, store_projection
from engineering_platform.workspace_inbox_api import build_human_envelope
from engineering_platform.telemetry import wait_for_pending_telemetry


_INHERITED_RUNNER_ENVIRONMENT = (
    "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_SCHEMA",
    "DJCONNECT_ENGINEERING_ADMITTED_STORAGE_ROOT",
    "DJCONNECT_ENGINEERING_VALIDATION_RUN_ID",
    inbox_watcher.BACKGROUND_RUN_ID_ENVIRONMENT,
    inbox_watcher.BACKGROUND_JOB_ID_ENVIRONMENT,
)


class InboxWatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        # A real detached watcher passes these values to its child process.
        # Each test instead owns a new repository and explicitly creates any
        # lifecycle identity it needs.
        self.inherited_runner_environment = {
            key: os.environ.pop(key, None) for key in _INHERITED_RUNNER_ENVIRONMENT
        }
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "cloud"
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir(parents=True)
        self.runtime = self.repo / "managed-codex" / "bin" / "codex"
        self.managed_runtime_prefix = self.repo / "managed-codex"
        self.managed_runtime = self.managed_runtime_prefix / "bin" / "codex"
        self.managed_runtime.parent.mkdir(parents=True)
        self.managed_runtime.write_text("#!/bin/sh\n", encoding="utf-8")
        self.managed_runtime.chmod(0o700)
        self.preflight = patch(
            "engineering_platform.inbox_watcher.execute_host_preflight",
            return_value=HostPreflightResult(
                "PASS", "Engineering Platform", "2.0.0", "2026.12", None, None, "now", 1, ()
            ),
        )
        self.preflight.start()
        self.workspace_preflight = patch(
            "engineering_platform.inbox_watcher.execute_workspace_preflight",
            return_value=WorkspacePreflightResult("PASS", "DJConnect", "repo", "main", "MANAGED", "now", 1, ()),
        )
        self.workspace_preflight.start()
        self.capability_preflight = patch(
            "engineering_platform.inbox_watcher.execute_capability_preflight",
            return_value=CapabilityPreflightResult("PASS", "now", 1, (), "RETRYABLE", None, "Capability admission passed."),
        )
        self.capability_preflight.start()
        self.dependabot_admission = patch(
            "engineering_platform.inbox_watcher._admit_dependabot_pull_requests", return_value=0
        )
        self.dependabot_admission.start()
        self.managed_runtime_prefix_patch = patch(
            "engineering_platform.platform_api.engineering_platform_codex_cli_prefix",
            return_value=self.managed_runtime_prefix,
        )
        self.managed_runtime_prefix_patch.start()
        # CI deliberately has no Codex CLI.  Every watcher fixture receives a
        # harmless executable so the tests exercise watcher admission rather
        # than host installation.
        self.runtime_environment = patch.dict(
            os.environ,
            {inbox_watcher.RUNTIME_EXECUTABLE_ENVIRONMENT: str(self.runtime)},
        )
        self.runtime_environment.start()
        inbox = inbox_watcher.folders(self.root)["Inbox"]
        self.inbox = inbox

    def tearDown(self) -> None:
        self.preflight.stop()
        self.workspace_preflight.stop()
        self.capability_preflight.stop()
        self.dependabot_admission.stop()
        self.managed_runtime_prefix_patch.stop()
        self.runtime_environment.stop()
        wait_for_pending_telemetry()
        self.temp.cleanup()
        for key, value in self.inherited_runner_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_status_reconciliation_starts_a_reconciliation_transaction(self) -> None:
        prompt = self.repo / "reconciliation.md"
        prompt.write_text(
            "Status-Reconciliation-Of: inbox-abcdef123456\n\n# Reconcile status\n",
            encoding="utf-8",
        )
        completed = subprocess.CompletedProcess(("engineering-execution-host",), 0, "", "")

        with patch.object(inbox_watcher.LocalProcessProvider, "execute", return_value=completed) as execute:
            inbox_watcher._execute_runner_command(self.repo, prompt, "inbox-reconciliation")

        arguments = execute.call_args.args[1]
        self.assertEqual(arguments[:3], [sys.executable, "-m", "engineering_platform"])
        self.assertIn("--transaction-kind", arguments)
        self.assertEqual(arguments[arguments.index("--transaction-kind") + 1], "RECONCILIATION")

    def test_watcher_ready_record_identifies_the_resolved_inbox_route(self) -> None:
        inbox_watcher.publish_ready_record(self.repo, self.root)

        ready = inbox_watcher.load_projection(self.repo, inbox_watcher.WATCHER_READY_PROJECTION)

        self.assertEqual(ready["inbox_path"], str((self.root / "Inbox").resolve()))
        self.assertEqual(ready["pid"], os.getpid())
        self.assertIsInstance(ready["started_at"], str)

    def test_preflight_failure_keeps_the_specific_bounded_runner_reason(self) -> None:
        completed = subprocess.CompletedProcess(("engineering-execution-host",), 2, "BLOCKED: working tree is not clean\n", "")

        self.assertEqual(
            inbox_watcher._runner_failure_detail(completed),
            "BLOCKED: working tree is not clean",
        )

    def test_source_revision_fails_closed_for_git_failures_and_invalid_output(self) -> None:
        with patch.object(
            inbox_watcher.LocalProcessProvider,
            "execute",
            side_effect=(
                subprocess.CompletedProcess(("git",), 1, "", "not a repository"),
                subprocess.CompletedProcess(("git",), 0, "not-a-sha\n", ""),
                subprocess.CompletedProcess(("git",), 0, "a" * 40 + "\n", ""),
            ),
        ):
            self.assertIsNone(inbox_watcher._source_revision(self.repo))
            self.assertIsNone(inbox_watcher._source_revision(self.repo))
            self.assertEqual(inbox_watcher._source_revision(self.repo), "a" * 40)

    def test_persisted_producer_prefers_central_submission_and_falls_back_for_legacy_prompt(self) -> None:
        central_submission = {
            "producer_id": "agent-delivery",
            "producer_type": "EXTERNAL",
            "producer_version": "1.0",
            "correlation_id": "corr-123",
            "mission_id": "mission-456",
            "engineering_action_id": "action-789",
            "contract_version": "1.0",
        }
        with patch(
            "engineering_platform.inbox_watcher.load_submission_for_run",
            return_value=central_submission,
        ):
            producer = inbox_watcher._persisted_producer_for_run(self.repo, "inbox-central", "# legacy")
        self.assertEqual(producer.producer_id, "agent-delivery")
        self.assertEqual(producer.producer_type, "EXTERNAL")
        self.assertEqual(producer.producer_version, "1.0")
        self.assertEqual(producer.correlation_id, "corr-123")
        self.assertEqual(producer.mission_id, "mission-456")
        self.assertEqual(producer.engineering_action_id, "action-789")
        self.assertEqual(producer.execution_constraint_version, "1.0")

        with patch(
            "engineering_platform.inbox_watcher.load_submission_for_run",
            side_effect=inbox_watcher.EngineeringStorageError("CENTRAL unavailable"),
        ):
            legacy = inbox_watcher._persisted_producer_for_run(self.repo, "inbox-legacy", "# legacy")
        self.assertEqual(legacy.producer_id, "legacy")
        self.assertEqual(legacy.producer_type, "HUMAN")

    def test_configured_scan_interval_uses_central_value_and_safe_fallback(self) -> None:
        with patch(
            "engineering_platform.inbox_watcher.dashboard_configuration",
            return_value={"inbox_scan_interval_seconds": "2.5"},
        ):
            self.assertEqual(inbox_watcher._configured_scan_interval(self.repo, 15), 2.5)
        with patch(
            "engineering_platform.inbox_watcher.dashboard_configuration",
            side_effect=inbox_watcher.EngineeringStorageError("CENTRAL unavailable"),
        ):
            self.assertEqual(inbox_watcher._configured_scan_interval(self.repo, 1), 5)

    def test_terminal_report_runtime_metadata_is_bounded_and_optional(self) -> None:
        self.assertEqual(inbox_watcher._report_runtime_metadata(None), {})
        self.assertEqual(inbox_watcher._report_runtime_metadata(self.repo / "missing-report.md"), {})

        report = self.repo / "terminal-report.md"
        report.write_text(
            "\n".join(
                (
                    "- Runtime Provider: `codex`",
                    "- AI Model: `gpt-5`",
                    "- Reasoning Profile: `high`",
                    "- Configuration Profile: `managed`",
                    "- Ignored: `not-runtime-metadata`",
                )
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            inbox_watcher._report_runtime_metadata(report),
            {
                "runtime_provider": "codex",
                "runtime_model": "gpt-5",
                "reasoning_profile": "high",
                "configuration_profile": "managed",
            },
        )

    def test_telemetry_values_accept_only_run_bound_non_negative_evidence(self) -> None:
        run_id = "inbox-telemetry"
        runs = self.repo / ".engineering" / "engineering-runs"
        status = self.repo / ".engineering" / "status"
        runs.mkdir(parents=True)
        status.mkdir(parents=True)
        (runs / f"{run_id}.json").write_text(
            json.dumps({"agent_execution_seconds": 12.5, "repository": "pcvantol/qualification"}),
            encoding="utf-8",
        )
        (status / "codex_usage.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                }
            ),
            encoding="utf-8",
        )

        seconds, usage, repository = inbox_watcher._telemetry_values(self.repo, run_id)

        self.assertEqual(seconds, 12.5)
        self.assertEqual(repository, "pcvantol/qualification")
        self.assertEqual(usage, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    def test_status_reconciliation_only_queues_a_verified_reconciliation_request(self) -> None:
        run_id = "inbox-status-drift"
        StateStore(self.repo / ".engineering" / "engineering-runs").save(TransactionState(
            run_id, "pcvantol/djconnect", "prompt.md", "BLOCKED", terminal=True,
            terminal_condition="external_blocked",
            diagnostic="Pre-flight is NO-GO: rolling status records still describe Finalization as in progress.",
        ))

        self.assertEqual(
            inbox_watcher.status_reconciliation_preview(self.repo, run_id),
            {"run_id": run_id, "reason": "merged_status_records_stale"},
        )
        outcome = inbox_watcher.submit_status_reconciliation(self.repo, self.root, run_id)
        self.assertEqual(outcome["run_id"], run_id)
        prompt = (self.inbox / outcome["filename"]).read_text(encoding="utf-8")
        self.assertIn(f"Status-Reconciliation-Of: {run_id}", prompt)
        self.assertIn("governance-only Reconciliation", prompt)
        with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "staat al in de wachtrij"):
            inbox_watcher.submit_status_reconciliation(self.repo, self.root, run_id)

    def test_status_reconciliation_requeue_gets_a_new_job_identity_after_archive(self) -> None:
        run_id = "inbox-status-requeue"
        StateStore(self.repo / ".engineering" / "engineering-runs").save(TransactionState(
            run_id, "pcvantol/djconnect", "prompt.md", "BLOCKED", terminal=True,
            terminal_condition="external_blocked",
            diagnostic="Rolling status records are stale.",
        ))
        first = inbox_watcher.submit_status_reconciliation(self.repo, self.root, run_id)
        first_prompt = self.inbox / first["filename"]
        first_content = first_prompt.read_text(encoding="utf-8")
        first_prompt.unlink()
        second = inbox_watcher.submit_status_reconciliation(self.repo, self.root, run_id)
        second_content = (self.inbox / second["filename"]).read_text(encoding="utf-8")
        self.assertNotEqual(first_content, second_content)
        self.assertIn("Status-Reconciliation-Request:", second_content)

    def test_status_reconciliation_accepts_the_triggering_stale_rolling_record_diagnostic(self) -> None:
        run_id = "inbox-triggering-shape"
        StateStore(self.repo / ".engineering" / "engineering-runs").save(TransactionState(
            run_id, "pcvantol/djconnect", "prompt.md", "BLOCKED", terminal=True,
            terminal_condition="external_blocked",
            diagnostic=(
                "PR #865 is merged and current main is clean, but the rolling records "
                "still state that PR #866 finalization is pending despite merged finalization PR #867."
            ),
        ))

        self.assertEqual(
            inbox_watcher.status_reconciliation_preview(self.repo, run_id),
            {"run_id": run_id, "reason": "merged_status_records_stale"},
        )

    def test_status_reconciliation_rejects_a_run_with_its_own_pr_evidence(self) -> None:
        run_id = "inbox-own-pr-evidence"
        StateStore(self.repo / ".engineering" / "engineering-runs").save(TransactionState(
            run_id, "pcvantol/djconnect", "prompt.md", "BLOCKED", terminal=True,
            terminal_condition="external_blocked", implementation_pull_request=866,
            diagnostic="Rolling records still state that Finalization is pending.",
        ))

        with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "niet in aanmerking"):
            inbox_watcher.status_reconciliation_preview(self.repo, run_id)

    def test_verified_status_reconciliation_can_pass_its_blocked_predecessor_gate(self) -> None:
        run_id = "inbox-status-drift"
        StateStore(self.repo / ".engineering" / "engineering-runs").save(TransactionState(
            run_id, "pcvantol/djconnect", "prompt.md", "BLOCKED", terminal=True,
            terminal_condition="external_blocked",
            diagnostic="Pre-flight is NO-GO: rolling status records still describe Finalization as in progress.",
        ))
        status_directory = self.repo / ".engineering" / "status"
        status_directory.mkdir(parents=True)
        (status_directory / "status.json").write_text(
            json.dumps(
                {
                    "last_executed_run": run_id,
                    "last_executed_phase": "BLOCKED",
                    "last_executed_filename": "blocked.txt",
                    "last_executed_title": "Blocked predecessor",
                }
            ),
            encoding="utf-8",
        )
        outcome = inbox_watcher.submit_status_reconciliation(self.repo, self.root, run_id)
        reconciliation = self.inbox / outcome["filename"]
        content = reconciliation.read_text(encoding="utf-8")

        admission = inbox_watcher._admit_queue_candidate(
            self.repo,
            [(reconciliation, content)],
            child_run_id=None,
            child_job_id=None,
            logger=logging.getLogger("test"),
        )

        self.assertEqual(admission.source, reconciliation)
        self.assertEqual(admission.content, content)

    def test_status_reconciliation_marker_without_verified_drift_stays_blocked(self) -> None:
        run_id = "inbox-blocked123"
        reconciliation = self.inbox / "unverified-reconciliation.md"
        content = f"Status-Reconciliation-Of: {run_id}\n\n# Unverified request"
        reconciliation.write_text(content, encoding="utf-8")
        status_directory = self.repo / ".engineering" / "status"
        status_directory.mkdir(parents=True)
        (status_directory / "status.json").write_text(
            json.dumps(
                {
                    "last_executed_run": run_id,
                    "last_executed_phase": "BLOCKED",
                    "last_executed_filename": "blocked.txt",
                    "last_executed_title": "Blocked predecessor",
                }
            ),
            encoding="utf-8",
        )

        admission = inbox_watcher._admit_queue_candidate(
            self.repo,
            [(reconciliation, content)],
            child_run_id=None,
            child_job_id=None,
            logger=logging.getLogger("test"),
        )

        self.assertIsNone(admission.source)
        self.assertEqual(json_status(self.repo)["watcher_state"], "WAITING_FOR_PREDECESSOR")

    @unittest.skip("checkout-bound watcher execution is formally retired")
    def test_watcher_run_logs_lifecycle_identity_on_orderly_shutdown(self) -> None:
        lifecycle_context = {
            "application_version": inbox_watcher.WATCHER_VERSION,
            "git_commit": "abc123def456",
            "launchd_label": inbox_watcher.LABEL,
            "launch_agent_path": "/tmp/inbox.plist",
        }
        with (
            patch("engineering_platform.inbox_watcher.provision_workspace"),
            patch("engineering_platform.inbox_watcher.cloud_root", return_value=self.root),
            patch(
                "engineering_platform.inbox_watcher.component_logger",
                return_value=logging.getLogger("test"),
            ) as logger,
            patch("engineering_platform.inbox_watcher.component_lifecycle_context", return_value=lifecycle_context),
            patch("engineering_platform.inbox_watcher.shutdown_signal_logging", return_value=nullcontext()),
            patch("engineering_platform.inbox_watcher.single_instance", return_value=nullcontext()),
            patch("engineering_platform.inbox_watcher.time.sleep", side_effect=KeyboardInterrupt),
            patch("engineering_platform.inbox_watcher.log_event") as log_event,
        ):
            self.assertEqual(
                inbox_watcher.main(["run", "--repo", str(self.repo), "--icloud-root", str(self.root)]),
                0,
            )

        self.assertEqual(logger.call_args_list[0].args, (self.repo.resolve(), "inbox"))
        self.assertEqual(log_event.call_args_list[0].args[2], "watcher_started")
        self.assertEqual(log_event.call_args_list[-1].args[2], "watcher_shutdown_completed")
        self.assertEqual(log_event.call_args_list[-1].kwargs["context"], lifecycle_context)

    @unittest.skip("checkout-bound watcher execution is formally retired")
    def test_watcher_restarts_after_a_completed_cycle_when_source_revision_changes(self) -> None:
        lifecycle_context = {
            "application_version": inbox_watcher.WATCHER_VERSION,
            "git_commit": "abc123def456",
            "launchd_label": inbox_watcher.LABEL,
            "launch_agent_path": "/tmp/inbox.plist",
        }
        with (
            patch("engineering_platform.inbox_watcher.provision_workspace"),
            patch("engineering_platform.inbox_watcher.cloud_root", return_value=self.root),
            patch("engineering_platform.inbox_watcher.component_logger", return_value=logging.getLogger("test")),
            patch("engineering_platform.inbox_watcher.component_lifecycle_context", return_value=lifecycle_context),
            patch("engineering_platform.inbox_watcher.shutdown_signal_logging", return_value=nullcontext()),
            patch("engineering_platform.inbox_watcher.single_instance", return_value=nullcontext()),
            patch("engineering_platform.inbox_watcher.once") as once,
            patch("engineering_platform.inbox_watcher._source_revision", side_effect=["a" * 40, "b" * 40]),
            patch("engineering_platform.inbox_watcher.log_event") as log_event,
        ):
            self.assertEqual(
                inbox_watcher.main(["run", "--repo", str(self.repo), "--icloud-root", str(self.root)]),
                0,
            )

        once.assert_called_once_with(self.repo.resolve(), self.root, 15.0, background=True)
        self.assertIn(
            "watcher_source_revision_changed",
            [call.args[2] for call in log_event.call_args_list],
        )

    @unittest.skip("checkout-bound watcher execution is formally retired")
    def test_watcher_defers_source_restart_while_execution_ownership_is_active(self) -> None:
        lifecycle_context = {
            "application_version": inbox_watcher.WATCHER_VERSION,
            "git_commit": "abc123def456",
            "launchd_label": inbox_watcher.LABEL,
            "launch_agent_path": "/tmp/inbox.plist",
        }
        with (
            patch("engineering_platform.inbox_watcher.provision_workspace"),
            patch("engineering_platform.inbox_watcher.cloud_root", return_value=self.root),
            patch("engineering_platform.inbox_watcher.component_logger", return_value=logging.getLogger("test")),
            patch("engineering_platform.inbox_watcher.component_lifecycle_context", return_value=lifecycle_context),
            patch("engineering_platform.inbox_watcher.shutdown_signal_logging", return_value=nullcontext()),
            patch("engineering_platform.inbox_watcher.single_instance", return_value=nullcontext()),
            patch("engineering_platform.inbox_watcher.once"),
            patch("engineering_platform.inbox_watcher._active_transaction", return_value=True),
            patch("engineering_platform.inbox_watcher._source_revision", side_effect=["a" * 40, "b" * 40]),
            patch("engineering_platform.inbox_watcher.time.sleep", side_effect=KeyboardInterrupt),
            patch("engineering_platform.inbox_watcher.log_event") as log_event,
        ):
            self.assertEqual(
                inbox_watcher.main(["run", "--repo", str(self.repo), "--icloud-root", str(self.root)]),
                0,
            )
        self.assertIn(
            "watcher_source_revision_restart_deferred",
            [call.args[2] for call in log_event.call_args_list],
        )
        connection = open_storage(self.repo)
        try:
            row = connection.execute(
                "SELECT payload FROM execution_projections WHERE projection_name='watcher_restart_pending'"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row[0])["state"], "restart_pending_after_active_execution")

    @unittest.skip("checkout-bound watcher execution is formally retired")
    def test_watcher_projects_dashboard_migration_block_instead_of_stale_merge_wait(self) -> None:
        from engineering_platform.platform_bootstrap import WorkspaceMigrationBlockedError

        with patch(
            "engineering_platform.inbox_watcher.provision_workspace",
            side_effect=WorkspaceMigrationBlockedError("dashboard"),
        ):
            self.assertEqual(inbox_watcher.main(["once", "--repo", str(self.repo)]), 1)

        projected = json_status(self.repo)
        self.assertEqual(projected["watcher_state"], "WATCHER_WORKSPACE_MIGRATION_BLOCKED")
        self.assertEqual(projected["current_action"], "workspace_migration_blocked_by_active_dashboard")
        self.assertIn("dashboardactiviteit", projected["diagnostic"])

    def test_launch_agent_uses_a_shell_exec_launcher_for_the_selected_runtime(self) -> None:
        with patch("engineering_platform.inbox_watcher.Path.home", return_value=Path(self.temp.name)):
            agent = inbox_watcher.launch_agent(self.repo)

        rendered = agent.read_text(encoding="utf-8")
        self.assertIn("/bin/zsh", rendered)
        self.assertIn("-lc", rendered)
        self.assertIn("exec", rendered)
        self.assertIn(str(self.repo), rendered)
        self.assertIn("DJCONNECT_ENGINEERING_CODEX_EXECUTABLE", rendered)
        self.assertNotIn("StandardOutPath", rendered)
        self.assertNotIn("StandardErrorPath", rendered)

    def test_launch_agent_keeps_the_persisted_log_level_over_an_inherited_value(self) -> None:
        with patch.dict(os.environ, {inbox_watcher.LOG_LEVEL_ENVIRONMENT: "DEBUG"}), patch(
            "engineering_platform.inbox_watcher.Path.home", return_value=Path(self.temp.name)
        ):
            connection = open_storage(self.repo)
            try:
                connection.execute(
                    "INSERT INTO engineering_metadata(key, value) VALUES (?, ?)",
                    ("dashboard_configuration.log_level", '"INFO"'),
                )
                connection.commit()
            finally:
                connection.close()

            rendered = inbox_watcher.launch_agent(self.repo).read_text(encoding="utf-8")

        self.assertIn(
            f"<key>{inbox_watcher.LOG_LEVEL_ENVIRONMENT}</key><string>INFO</string>",
            rendered,
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

    def test_invalid_json_envelope_fails_closed_before_an_inbox_claim(self) -> None:
        source = self.inbox / "producer.json"
        source.write_text('{"contract":', encoding="utf-8")
        with patch.dict(os.environ, {
            inbox_watcher.BACKGROUND_RUN_ID_ENVIRONMENT: "",
            inbox_watcher.BACKGROUND_JOB_ID_ENVIRONMENT: "",
        }):
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 1)
        self.assertTrue(source.exists())
        self.assertEqual(json_status(self.repo)["watcher_state"], "INVALID_PRODUCER_SUBMISSION")
        self.assertFalse(list(inbox_watcher.local_folders(self.repo)["Running"].iterdir()))

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

    def test_titleless_structured_submission_uses_safe_queue_metadata(self) -> None:
        prompt = self.inbox / "queued.json"
        prompt.write_text(json.dumps({
            "contract": {"name": "djconnect.producer_submission", "version": "1.0"},
            "submission": {"id": "human-queue-title", "submitted_at": "2026-08-30T12:00:00Z"},
            "producer": {"id": "human:operator", "type": "HUMAN"},
            "prompt": {"text": "Sensitive prompt body must not reach the queue UI."},
            "execution_context": {"context_version": "1.0", "action_intent": "MUTATING_DELIVERY"},
        }), encoding="utf-8")

        item = inbox_watcher._queue_items([(prompt, prompt.read_text(encoding="utf-8"))])[0]

        self.assertEqual(item["title"], "Structured submission")
        self.assertEqual(item["title_kind"], "producer_submission")
        self.assertEqual(item["producer_type"], "HUMAN")
        self.assertEqual(item["action_intent"], "MUTATING_DELIVERY")
        self.assertNotIn("Sensitive prompt body", str(item))
        self.assertNotIn('\"contract\"', str(item))

    def test_defer_queued_prompt_moves_only_the_selected_waiting_inbox_file(self) -> None:
        selected = self.inbox / "defer-me.md"
        selected.write_text("# Later uitvoeren\n", encoding="utf-8")
        waiting = self.inbox / "keep-waiting.md"
        waiting.write_text("# Blijft actief\n", encoding="utf-8")

        outcome = inbox_watcher.defer_queued_prompt(self.repo, self.root, selected.name)

        self.assertFalse(selected.exists())
        deferred = self.inbox / "_deferred" / outcome["deferred_filename"]
        self.assertTrue(deferred.exists())
        self.assertEqual(deferred.read_text(encoding="utf-8"), "# Later uitvoeren\n")
        self.assertEqual([path.name for path in inbox_watcher.discover(self.root)], [waiting.name])

    def test_defer_queued_prompt_preserves_an_existing_deferred_file(self) -> None:
        selected = self.inbox / "defer-me.md"
        selected.write_text("# Nieuwe uitvoering\n", encoding="utf-8")
        deferred_folder = self.inbox / "_deferred"
        deferred_folder.mkdir()
        original = deferred_folder / selected.name
        original.write_text("# Eerder uitgestelde uitvoering\n", encoding="utf-8")

        outcome = inbox_watcher.defer_queued_prompt(self.repo, self.root, selected.name)

        self.assertFalse(selected.exists())
        self.assertNotEqual(outcome["deferred_filename"], selected.name)
        self.assertEqual(original.read_text(encoding="utf-8"), "# Eerder uitgestelde uitvoering\n")
        self.assertEqual(
            (deferred_folder / outcome["deferred_filename"]).read_text(encoding="utf-8"),
            "# Nieuwe uitvoering\n",
        )

    def test_defer_queued_prompt_rejects_paths_and_missing_items(self) -> None:
        for filename in ("../outside.md", "missing.md"):
            with self.assertRaises(inbox_watcher.RetrySubmissionError):
                inbox_watcher.defer_queued_prompt(self.repo, self.root, filename)

    def test_launch_path_preserves_codex_location(self) -> None:
        with patch("engineering_platform.inbox_watcher.shutil.which", return_value="/opt/homebrew/bin/codex"):
            self.assertEqual(inbox_watcher.launch_path().split(":")[0], "/opt/homebrew/bin")

    def test_terminal_checkpoint_overrides_stale_live_status(self) -> None:
        status = self.repo / ".engineering/status"
        checkpoint = self.repo / ".engineering/engineering-runs"
        status.mkdir(parents=True)
        checkpoint.mkdir(parents=True)
        (status / "current.json").write_text('{"run_id":"inbox-stale","phase":"INITIALIZE"}', encoding="utf-8")
        (checkpoint / "inbox-stale.json").write_text('{"phase":"BLOCKED"}', encoding="utf-8")
        self.assertFalse(inbox_watcher._active_transaction(self.repo))

    def test_terminal_watcher_status_overrides_stale_live_checkpoint(self) -> None:
        status = self.repo / ".engineering/status"
        status.mkdir(parents=True)
        (status / "current.json").write_text(
            '{"run_id":"inbox-stale","phase":"WAIT_FOR_TERMINAL_EVIDENCE"}', encoding="utf-8"
        )
        (status / "status.json").write_text(
            '{"last_executed_run":"inbox-stale","last_executed_phase":"FAILED"}', encoding="utf-8"
        )

        self.assertFalse(inbox_watcher._active_transaction(self.repo))

    def test_operator_merge_wait_holds_inbox_and_can_be_explicitly_aborted(self) -> None:
        from engineering_platform.agent_state import StateStore, TransactionState

        run_id = "inbox-merge-wait"
        source = inbox_watcher.local_folders(self.repo)["Running"] / "merge-wait__prompt.md"
        source.write_text("# Merge wait\n", encoding="utf-8")
        StateStore(self.repo / ".engineering" / "engineering-runs").save(
            TransactionState(
                run_id=run_id,
                repository="pcvantol/djconnect",
                prompt_path=str(source),
                phase="WAIT_FOR_OPERATOR_MERGE",
                pull_request=832,
                waiting_for_merge_since="2026-08-16T09:00:00+00:00",
            )
        )
        inbox_watcher.status(
            self.repo,
            "WAITING_FOR_OPERATOR_MERGE",
            run_id=run_id,
            job_id="merge-wait",
            queued_jobs=0,
            queue_items=[],
        )
        with open_storage(self.repo) as connection:
            store_projection(
                connection,
                "live_status",
                {"run_id": run_id, "phase": "WAIT_FOR_OPERATOR_MERGE"},
            )

        self.assertTrue(inbox_watcher._active_transaction(self.repo))
        outcome = inbox_watcher.abort_operator_merge_wait(self.repo, run_id)

        self.assertTrue(outcome["dismissed"])
        state = StateStore(self.repo / ".engineering" / "engineering-runs").load(run_id)
        self.assertEqual(state.phase, "FAILED")
        self.assertTrue(state.terminal)
        self.assertFalse(source.exists())
        self.assertTrue((inbox_watcher.local_folders(self.repo)["Failed"] / "merge-wait__prompt.md").exists())
        self.assertFalse(inbox_watcher._active_transaction(self.repo))

    def test_lease_lost_finalization_replaces_idle_projection_with_durable_active_state(self) -> None:
        run_id = "inbox-lease-lost-finalization"
        state = TransactionState(
            run_id, "pcvantol/djconnect", "prompt.md", "FINALIZE_AGENT",
            transaction_kind="FINALIZATION", implementation_pull_request=944,
            finalization_pull_request=945, next_action="recover_finalization_evidence",
        )
        StateStore(self.repo / ".engineering" / "engineering-runs").save(state)
        inbox_watcher.status(self.repo, "WATCHER_IDLE", queued_jobs=0, queue_items=[])

        inbox_watcher._publish_active_queue(self.repo, [])

        snapshot = inbox_watcher.load_projection(self.repo, "watcher_status")
        self.assertEqual(snapshot["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(snapshot["run_id"], run_id)
        self.assertEqual(snapshot["current_phase"], "FINALIZE_AGENT")
        self.assertEqual(snapshot["finalization_pr"], 945)

    def test_detached_runner_checkpoint_replaces_runner_starting_projection(self) -> None:
        run_id = "inbox-detached-review"
        state = TransactionState(
            run_id, "pcvantol/djconnect", "prompt.md", "CAPABILITY_REVIEW",
            next_action="review_capabilities",
        )
        StateStore(self.repo / ".engineering" / "engineering-runs").save(state)
        inbox_watcher.status(
            self.repo,
            "RUNNER_STARTING",
            run_id=run_id,
            job_id="detached-review",
            queued_jobs=0,
            queue_items=[],
        )

        inbox_watcher._publish_active_queue(self.repo, [])

        snapshot = inbox_watcher.load_projection(self.repo, "watcher_status")
        self.assertEqual(snapshot["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(snapshot["run_id"], run_id)
        self.assertEqual(snapshot["current_phase"], "CAPABILITY_REVIEW")
        self.assertEqual(snapshot["current_action"], "review_capabilities")

    def test_dismissed_stale_checkpoint_does_not_reclaim_live_projection(self) -> None:
        from engineering_platform.prompt_history import record_prompt_execution
        from engineering_platform.storage import record_execution_dismissal

        run_id = "inbox-dismissed-stale-checkpoint"
        record_prompt_execution(
            self.repo, run_id=run_id, terminal_state="FAILED",
            prompt_title="Historical failed execution", executed_at="2026-08-30T11:47:14Z",
        )
        record_execution_dismissal(
            self.repo, run_id=run_id, terminal_state="FAILED",
            dismissed_at="2026-08-30T12:00:00Z", dismissed_by="dashboard_operator",
        )
        StateStore(self.repo / ".engineering" / "engineering-runs").save(
            TransactionState(
                run_id, "pcvantol/djconnect", "prompt.md", "QUALITY_CONTROL_AGENT",
                next_action="autonomous_refactor_and_quality_control",
            )
        )
        with open_storage(self.repo) as connection:
            store_projection(
                connection,
                "live_status",
                {"run_id": run_id, "phase": "QUALITY_CONTROL_AGENT"},
            )
        inbox_watcher.status(
            self.repo, "WATCHER_IDLE", queued_jobs=0, queue_items=[], run_id=None,
        )

        self.assertIsNone(inbox_watcher._nonterminal_transaction_state(self.repo))
        self.assertFalse(inbox_watcher._active_transaction(self.repo))
        self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "WATCHER_IDLE")
        self.assertIsNone(snapshot["run_id"])

    def test_operator_merge_wait_is_rate_limited_and_projects_prior_job_context(self) -> None:
        from engineering_platform.agent_state import StateStore, TransactionState

        run_id = "inbox-merge-poll"
        state = TransactionState(
            run_id=run_id,
            repository="pcvantol/djconnect",
            prompt_path="/tmp/merge-poll.md",
            phase="WAIT_FOR_OPERATOR_MERGE",
            implementation_pull_request=832,
            waiting_for_merge_since="2026-08-16T09:00:00+00:00",
        )
        StateStore(self.repo / ".engineering" / "engineering-runs").save(state)
        inbox_watcher.status(
            self.repo,
            "RUNNER_STARTING",
            run_id=run_id,
            job_id="merge-poll",
            submitted_filename="merge-poll.md",
            prompt_title="Merge poll",
        )

        self.assertEqual(inbox_watcher._operator_merge_wait(self.repo), state)
        self.assertFalse(inbox_watcher._operator_merge_poll_due(self.repo, run_id))
        with open_storage(self.repo) as connection:
            store_projection(
                connection,
                "watcher_status",
                {
                    "run_id": run_id,
                    "last_update": "2020-01-01T00:00:00+00:00",
                    "job_id": "merge-poll",
                    "submitted_filename": "merge-poll.md",
                    "prompt_title": "Merge poll",
                },
            )
        self.assertTrue(inbox_watcher._operator_merge_poll_due(self.repo, run_id))

        inbox_watcher._publish_operator_merge_wait(self.repo, state, queue_items=[{"filename": "later.md"}], queue_depth=1)

        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "WAITING_FOR_OPERATOR_MERGE")
        self.assertEqual(snapshot["job_id"], "merge-poll")
        self.assertEqual(snapshot["submitted_filename"], "merge-poll.md")
        self.assertEqual(snapshot["prompt_title"], "Merge poll")
        self.assertEqual(snapshot["implementation_pr"], 832)
        self.assertEqual(snapshot["queue_depth"], 1)

    def test_direct_operator_merge_status_check_requires_remote_merge_and_ancestry(self) -> None:
        run_id = "inbox-direct-merge-check"
        source = inbox_watcher.local_folders(self.repo)["Running"] / "direct-check.md"
        source.write_text("# Direct merge check\n", encoding="utf-8")
        waiting = TransactionState(
            run_id=run_id, repository="pcvantol/djconnect", prompt_path=str(source),
            phase="WAIT_FOR_OPERATOR_MERGE", implementation_pull_request=832,
        )
        StateStore(self.repo / ".engineering" / "engineering-runs").save(waiting)
        inbox_watcher._publish_operator_merge_wait(self.repo, waiting)
        open_pr = PullRequestEvidence(832, "OPEN", True, True, "a" * 40)
        with patch("engineering_platform.inbox_watcher.GhCliClient") as github:
            github.return_value.pull_request.return_value = open_pr
            outcome = inbox_watcher.check_operator_merge_status(self.repo, run_id)
        self.assertFalse(outcome["verified"])
        self.assertEqual(outcome["reason"], "pull_request_not_merged")
        self.assertEqual(outcome["pull_request"], 832)

        merged_pr = PullRequestEvidence(832, "MERGED", True, True, "a" * 40)
        with (
            patch("engineering_platform.inbox_watcher.GhCliClient") as github,
            patch("engineering_platform.inbox_watcher.SubprocessRepositoryClient") as repository,
        ):
            github.return_value.pull_request.return_value = merged_pr
            repository.return_value.remote_main_contains.return_value = True
            outcome = inbox_watcher.check_operator_merge_status(self.repo, run_id)

        self.assertTrue(outcome["verified"])
        self.assertEqual(outcome["continuation"], "queued")
        self.assertEqual(outcome["pull_request"], 832)
        repository.return_value.refresh_main_reference.assert_called_once_with(self.repo)
        resumed = StateStore(self.repo / ".engineering" / "engineering-runs").load(run_id)
        self.assertEqual(resumed.next_action, "resume_verified_merge")
        self.assertTrue(inbox_watcher._operator_merge_poll_due(self.repo, run_id))
        published = json_status(self.repo)
        self.assertEqual(published["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(published["current_phase"], "FINALIZE_AGENT")
        self.assertEqual(published["current_action"], "create_finalization")

    def test_direct_operator_merge_status_check_fails_closed_when_evidence_is_incomplete(self) -> None:
        run_id = "inbox-merge-evidence"
        source = inbox_watcher.local_folders(self.repo)["Running"] / "merge-evidence.md"
        source.write_text("# Merge evidence\n", encoding="utf-8")
        store = StateStore(self.repo / ".engineering" / "engineering-runs")

        with self.assertRaises(inbox_watcher.RetrySubmissionError):
            inbox_watcher.check_operator_merge_status(self.repo, "not-a-run-id")
        self.assertEqual(
            inbox_watcher.check_operator_merge_status(self.repo, run_id),
            {"verified": False, "reason": "not_waiting"},
        )

        store.save(TransactionState(
            run_id=run_id, repository="pcvantol/djconnect", prompt_path=str(source),
            phase="WAIT_FOR_OPERATOR_MERGE",
        ))
        self.assertEqual(
            inbox_watcher.check_operator_merge_status(self.repo, run_id),
            {"verified": False, "reason": "pull_request_unavailable"},
        )

        store.save(TransactionState(
            run_id=run_id, repository="pcvantol/djconnect", prompt_path=str(source),
            phase="WAIT_FOR_OPERATOR_MERGE", implementation_pull_request=832,
        ))
        with patch("engineering_platform.inbox_watcher.GhCliClient") as github:
            github.return_value.pull_request.side_effect = RuntimeError("GitHub unavailable")
            outcome = inbox_watcher.check_operator_merge_status(self.repo, run_id)
        self.assertFalse(outcome["verified"])
        self.assertEqual(outcome["reason"], "github_cli_unavailable")
        self.assertEqual(outcome["pull_request"], 832)

        missing_commit = PullRequestEvidence(832, "MERGED", True, True, None)
        with patch("engineering_platform.inbox_watcher.GhCliClient") as github:
            github.return_value.pull_request.return_value = missing_commit
            self.assertEqual(
                inbox_watcher.check_operator_merge_status(self.repo, run_id),
                {"verified": False, "reason": "merge_commit_unavailable", "pull_request": 832,
                 "last_successful_github_check_at": None},
            )

        merged_pr = PullRequestEvidence(832, "MERGED", True, True, "a" * 40)
        with (
            patch("engineering_platform.inbox_watcher.GhCliClient") as github,
            patch("engineering_platform.inbox_watcher.SubprocessRepositoryClient") as repository,
        ):
            github.return_value.pull_request.return_value = merged_pr
            repository.return_value.remote_main_contains.return_value = False
            self.assertEqual(
                inbox_watcher.check_operator_merge_status(self.repo, run_id),
                {"verified": False, "reason": "merge_not_in_origin_main", "pull_request": 832,
                 "last_successful_github_check_at": None},
            )
            repository.return_value.refresh_main_reference.side_effect = RuntimeError("git unavailable")
            self.assertEqual(
                inbox_watcher.check_operator_merge_status(self.repo, run_id),
                {"verified": False, "reason": "main_ancestry_unavailable", "pull_request": 832,
                 "last_successful_github_check_at": ANY},
            )

    def test_direct_merge_check_persists_safe_failure_category_and_last_success(self) -> None:
        run_id = "inbox-merge-check-metadata"
        source = inbox_watcher.local_folders(self.repo)["Running"] / "metadata.md"
        source.write_text("# Merge metadata\n", encoding="utf-8")
        state = TransactionState(
            run_id=run_id, repository="pcvantol/djconnect", prompt_path=str(source),
            phase="WAIT_FOR_OPERATOR_MERGE", implementation_pull_request=832,
        )
        StateStore(self.repo / ".engineering" / "engineering-runs").save(state)
        inbox_watcher._publish_operator_merge_wait(self.repo, state)
        open_pr = PullRequestEvidence(832, "OPEN", True, True, "a" * 40)
        with patch("engineering_platform.inbox_watcher.GhCliClient") as github:
            github.return_value.pull_request.return_value = open_pr
            inbox_watcher.check_operator_merge_status(self.repo, run_id)
        with patch("engineering_platform.inbox_watcher.GhCliClient") as github:
            github.return_value.pull_request.side_effect = RuntimeError("authentication required")
            outcome = inbox_watcher.check_operator_merge_status(self.repo, run_id)
        self.assertEqual(outcome["reason"], "github_authentication_unavailable")
        self.assertIsInstance(outcome["last_successful_github_check_at"], str)
        watcher = inbox_watcher.load_projection(self.repo, "watcher_status")
        self.assertEqual(watcher["merge_status_check"]["failure_category"], "github_authentication_unavailable")
        self.assertIsInstance(watcher["merge_status_check"]["last_successful_github_check_at"], str)

    def test_operator_merge_wait_finalization_archives_completed_prompt_and_report(self) -> None:
        from engineering_platform.agent_state import StateStore, TransactionState

        run_id = "inbox-merge-complete"
        source = inbox_watcher.local_folders(self.repo)["Running"] / "merge-complete__prompt.md"
        source.write_text("# Merge complete\n", encoding="utf-8")
        state = TransactionState(
            run_id=run_id,
            repository="pcvantol/djconnect",
            prompt_path=str(source),
            phase="COMPLETE",
            terminal=True,
            implementation_pull_request=832,
        )
        StateStore(self.repo / ".engineering" / "engineering-runs").save(state)
        report = self.repo / ".engineering" / "reports" / f"report_{run_id}.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Completed\n", encoding="utf-8")
        inbox_watcher.status(
            self.repo,
            "WAITING_FOR_OPERATOR_MERGE",
            run_id=run_id,
            job_id="merge-complete",
            prompt_title="Merged prompt",
        )

        inbox_watcher._finalize_operator_merge_wait(self.repo, state)

        self.assertFalse(source.exists())
        self.assertTrue((inbox_watcher.local_folders(self.repo)["Completed"] / "merge-complete__prompt.md").exists())
        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "JOB_COMPLETED")
        self.assertEqual(snapshot["last_executed_run"], run_id)
        self.assertEqual(snapshot["last_executed_title"], "Merged prompt")

    def test_operator_merge_wait_keeps_its_submitted_title_when_a_newer_run_is_current(self) -> None:
        run_id = "inbox-merge-title"
        source = inbox_watcher.local_folders(self.repo)["Running"] / "merge-title__prompt.md"
        source.write_text("# Durable merge title\n", encoding="utf-8")
        state = TransactionState(
            run_id=run_id,
            repository="pcvantol/djconnect",
            prompt_path=str(source),
            phase="COMPLETE",
            terminal=True,
            implementation_pull_request=832,
        )
        StateStore(self.repo / ".engineering" / "engineering-runs").save(state)
        report = self.repo / ".engineering" / "reports" / f"report_{run_id}.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Completed\n", encoding="utf-8")
        inbox_watcher.status(
            self.repo,
            "RUNNER_STARTING",
            run_id="inbox-newer-run",
            prompt_title="Newer prompt",
        )

        inbox_watcher._finalize_operator_merge_wait(self.repo, state)

        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["last_executed_run"], run_id)
        self.assertEqual(snapshot["last_executed_title"], "Durable merge title")

    def test_operator_merge_wait_remains_queue_owner_until_its_poll_is_due(self) -> None:
        from engineering_platform.agent_state import StateStore, TransactionState

        run_id = "inbox-merge-held"
        source = inbox_watcher.local_folders(self.repo)["Running"] / "merge-held__prompt.md"
        source.write_text("# Waiting for a merge\n", encoding="utf-8")
        StateStore(self.repo / ".engineering" / "engineering-runs").save(
            TransactionState(
                run_id=run_id,
                repository="pcvantol/djconnect",
                prompt_path=str(source),
                phase="WAIT_FOR_OPERATOR_MERGE",
                implementation_pull_request=832,
            )
        )
        inbox_watcher.status(
            self.repo,
            "WAITING_FOR_OPERATOR_MERGE",
            run_id=run_id,
            job_id="merge-held",
            queued_jobs=0,
            queue_items=[],
        )

        with patch("engineering_platform.inbox_watcher._execute_runner_command") as execute_runner:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        execute_runner.assert_not_called()
        self.assertEqual(json_status(self.repo)["watcher_state"], "WAITING_FOR_OPERATOR_MERGE")
        self.assertTrue(source.exists())

    def test_operator_merge_wait_poll_resumes_and_finalizes_the_merged_run(self) -> None:
        from engineering_platform.agent_state import StateStore, TransactionState

        run_id = "inbox-merge-resumed"
        source = inbox_watcher.local_folders(self.repo)["Running"] / "merge-resumed__prompt.md"
        source.write_text("# Resume after merge\n", encoding="utf-8")
        waiting = TransactionState(
            run_id=run_id,
            repository="pcvantol/djconnect",
            prompt_path=str(source),
            phase="WAIT_FOR_OPERATOR_MERGE",
            implementation_pull_request=832,
        )
        store = StateStore(self.repo / ".engineering" / "engineering-runs")
        store.save(waiting)
        inbox_watcher.status(
            self.repo,
            "WAITING_FOR_OPERATOR_MERGE",
            run_id=run_id,
            job_id="merge-resumed",
            queued_jobs=0,
            queue_items=[],
        )
        with open_storage(self.repo) as connection:
            store_projection(
                connection,
                "watcher_status",
                {
                    "run_id": run_id,
                    "job_id": "merge-resumed",
                    "last_update": "2020-01-01T00:00:00+00:00",
                },
            )

        def mark_complete(*_: object) -> None:
            store.save(replace(waiting, phase="COMPLETE", terminal=True))

        with patch("engineering_platform.inbox_watcher._execute_runner_command", side_effect=mark_complete) as execute_runner:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        execute_runner.assert_called_once_with(self.repo, source, run_id)
        self.assertFalse(source.exists())
        self.assertTrue((inbox_watcher.local_folders(self.repo)["Completed"] / "merge-resumed__prompt.md").exists())
        self.assertEqual(json_status(self.repo)["watcher_state"], "JOB_COMPLETED")

    def test_verified_merge_resumes_before_inbox_scan(self) -> None:
        from engineering_platform.agent_state import StateStore, TransactionState

        run_id = "inbox-verified-merge-first"
        source = inbox_watcher.local_folders(self.repo)["Running"] / "verified-merge.md"
        source.write_text("# Resume verified merge\n", encoding="utf-8")
        waiting = TransactionState(
            run_id=run_id,
            repository="pcvantol/djconnect",
            prompt_path=str(source),
            phase="WAIT_FOR_OPERATOR_MERGE",
            implementation_pull_request=832,
            next_action="resume_verified_merge",
        )
        store = StateStore(self.repo / ".engineering" / "engineering-runs")
        store.save(waiting)

        def begin_finalization(*_: object) -> None:
            store.save(replace(
                waiting,
                phase="FINALIZE_AGENT",
                transaction_kind="FINALIZATION",
                next_action="create_finalization",
            ))

        with (
            patch("engineering_platform.inbox_watcher._scan_queue", side_effect=AssertionError("Inbox scan must wait")),
            patch("engineering_platform.inbox_watcher._execute_runner_command", side_effect=begin_finalization) as execute_runner,
        ):
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        execute_runner.assert_called_once_with(self.repo, source, run_id)
        published = json_status(self.repo)
        self.assertEqual(published["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(published["current_phase"], "FINALIZE_AGENT")

    def test_operator_merge_wait_replaces_its_status_after_merge_starts_finalization(self) -> None:
        from engineering_platform.agent_state import StateStore, TransactionState

        run_id = "inbox-merge-finalizing"
        source = inbox_watcher.local_folders(self.repo)["Running"] / "merge-finalizing__prompt.md"
        source.write_text("# Finalize after merge\n", encoding="utf-8")
        waiting = TransactionState(
            run_id=run_id,
            repository="pcvantol/djconnect",
            prompt_path=str(source),
            phase="WAIT_FOR_OPERATOR_MERGE",
            implementation_pull_request=840,
        )
        store = StateStore(self.repo / ".engineering" / "engineering-runs")
        store.save(waiting)
        inbox_watcher.status(
            self.repo,
            "WAITING_FOR_OPERATOR_MERGE",
            run_id=run_id,
            job_id="merge-finalizing",
            queued_jobs=0,
            queue_items=[],
        )
        with open_storage(self.repo) as connection:
            store_projection(
                connection,
                "watcher_status",
                {
                    "run_id": run_id,
                    "job_id": "merge-finalizing",
                    "last_update": "2020-01-01T00:00:00+00:00",
                },
            )

        def begin_finalization(*_: object) -> None:
            store.save(replace(
                waiting,
                phase="FINALIZE_AGENT",
                transaction_kind="FINALIZATION",
                next_action="create_finalization",
                waiting_for_merge_since=None,
            ))

        with patch("engineering_platform.inbox_watcher._execute_runner_command", side_effect=begin_finalization):
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        published = json_status(self.repo)
        self.assertEqual(published["watcher_state"], "ENGINEERING_RUN_ACTIVE")
        self.assertEqual(published["current_phase"], "FINALIZE_AGENT")
        self.assertEqual(published["current_action"], "create_finalization")
        self.assertEqual(published["implementation_pr"], 840)
        self.assertNotEqual(published["watcher_state"], "WAITING_FOR_OPERATOR_MERGE")

    def test_operator_merge_wait_rejects_invalid_or_nonwaiting_runs(self) -> None:
        with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "run-ID is ongeldig"):
            inbox_watcher.abort_operator_merge_wait(self.repo, "invalid")
        with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "wacht niet"):
            inbox_watcher.abort_operator_merge_wait(self.repo, "inbox-not-waiting")

    def test_dead_detached_runner_does_not_hold_the_inbox(self) -> None:
        run_id = "inbox-abandoned"
        inbox_watcher.status(
            self.repo,
            "RUNNER_STARTING",
            run_id=run_id,
            runner_pid=12345,
        )
        connection = open_storage(self.repo)
        try:
            store_projection(
                connection,
                "live_status",
                {"run_id": run_id, "phase": "INITIALIZE"},
            )
        finally:
            connection.close()

        with patch("engineering_platform.inbox_watcher.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(inbox_watcher._active_transaction(self.repo))

    def test_exited_detached_runner_is_reaped_and_does_not_hold_the_inbox(self) -> None:
        with (
            patch("engineering_platform.inbox_watcher.os.waitpid", return_value=(12345, 0)),
            patch("engineering_platform.inbox_watcher.os.kill") as kill,
        ):
            self.assertFalse(inbox_watcher._detached_runner_is_alive({"runner_pid": 12345}))
        kill.assert_not_called()

    def test_nonterminal_finalization_without_lease_keeps_the_inbox_owned(self) -> None:
        run_id = "inbox-stale-lease"
        inbox_watcher.status(self.repo, "ENGINEERING_RUN_ACTIVE", run_id=run_id)
        connection = open_storage(self.repo)
        try:
            store_projection(connection, "live_status", {"run_id": run_id, "phase": "EXECUTE_AGENT"})
            connection.execute(
                "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                (run_id, json.dumps(TransactionState(
                    run_id, "pcvantol/djconnect", "prompt.md", "FINALIZE_AGENT",
                    transaction_kind="FINALIZATION",
                ).to_dict()), "FINALIZE_AGENT", "2026-01-01T00:00:00+00:00"),
            )
        finally:
            connection.close()
        self.assertTrue(inbox_watcher._active_transaction(self.repo))

    def test_expired_legacy_runner_start_does_not_hold_the_inbox(self) -> None:
        self.assertFalse(
            inbox_watcher._detached_runner_is_alive(
                {"last_update": "2020-01-01T00:00:00+00:00"}
            )
        )

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
        with patch("engineering_platform.inbox_watcher.subprocess.run", side_effect=(files, branch)):
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

    def test_reconciles_a_corrected_terminal_report_missing_from_history(self) -> None:
        """A prior schema mismatch must not hide its terminal failure forever."""
        report = self.repo / ".engineering" / "reports" / "corrected_inbox-schema-skew.md"
        report.parent.mkdir(parents=True)
        report.write_text(
            "\n".join(
                (
                    "# Engineering Report",
                    "- Run ID: `inbox-schema-skew`",
                    "- Terminal state: `FAILED`",
                    "",
                    "## Diagnostics",
                    "Engineering storage schema is newer than this Engineering Platform supports.",
                )
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        history = __import__("engineering_platform.prompt_history", fromlist=["prompt_history"]).prompt_history(self.repo)
        entry = next(item for item in history if item["run_id"] == "inbox-schema-skew")
        self.assertEqual(entry["status"], "FAILED")
        self.assertTrue(entry["report_available"])

    def test_complete_job_is_serialized_and_archived(self) -> None:
        (self.inbox / "job.md").write_text("# prompt", encoding="utf-8")
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
        with patch("engineering_platform.inbox_watcher._allocate_run_id", return_value=run_id), patch("engineering_platform.inbox_watcher.subprocess.run") as run:
            run.return_value = __import__("subprocess").CompletedProcess((), 0)
            code = inbox_watcher.once(self.repo, self.root, 0)
        self.assertEqual(code, 0)
        self.assertEqual(
            run.call_args.args[0][-4:],
            ["--run-id", run_id, "--admitted-storage-schema", str(inbox_watcher.ENGINEERING_STORAGE_SCHEMA_VERSION)],
        )
        self.assertEqual(len(list(inbox_watcher.local_folders(self.repo)["Completed"].glob("*__job.md"))), 1)
        self.assertFalse((self.root / "Reports").exists())
        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "JOB_COMPLETED")
        self.assertEqual(snapshot["last_executed_filename"], "job.md")
        self.assertEqual(snapshot["last_executed_title"], "prompt")
        self.assertEqual(snapshot["last_executed_run"], run_id)
        self.assertEqual(snapshot["last_executed_phase"], "COMPLETE")
        self.assertFalse(old_log.exists())

    def test_structured_human_envelope_preserves_the_persisted_action_intent_for_the_watcher(self) -> None:
        envelope = build_human_envelope(
            prompt="# Validate\n\nRun required controls.",
            title="Required controls",
            producer_identity="operator-peter",
            action_intent="VALIDATION_ONLY",
            validation_profile="DASHBOARD",
            submission_id="human-watcher-001",
        )
        source = self.inbox / "structured-human.json"
        source.write_text(json.dumps(envelope), encoding="utf-8")
        self.assertEqual(inbox_watcher._prompt_title(source.read_text(encoding="utf-8"), source.name), "Required controls")
        run_id = "inbox-structured-human"
        report = self.repo / ".engineering" / "reports" / f"report_{run_id}.md"
        report.parent.mkdir(parents=True)
        report.write_text("- Terminal state: `COMPLETE`\nCOMPLETE — delivered\n", encoding="utf-8")
        checkpoint = self.repo / ".engineering" / "engineering-runs" / f"{run_id}.json"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(json.dumps({"phase": "COMPLETE"}), encoding="utf-8")

        with patch("engineering_platform.inbox_watcher._allocate_run_id", return_value=run_id), patch(
            "engineering_platform.inbox_watcher.subprocess.run", return_value=subprocess.CompletedProcess((), 0)
        ):
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        wait_for_pending_telemetry()

        with open_storage(self.repo) as connection:
            row = connection.execute(
                "SELECT producer_id,producer_type,contract_version,execution_context_snapshot,execution_run_id FROM execution_submissions WHERE submission_id=?",
                ("human-watcher-001",),
            ).fetchone()
            run = connection.execute(
                "SELECT producer_id,producer_type,execution_constraint_version FROM execution_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual(row[:3], ("human:operator-peter", "HUMAN", "1.0"))
        self.assertEqual(json.loads(row[3])["action_intent"], "VALIDATION_ONLY")
        self.assertEqual(json.loads(row[3])["validation_profile"]["tier"], "DASHBOARD")
        self.assertEqual(row[4], run_id)
        self.assertEqual(run, ("human:operator-peter", "HUMAN", "1.0"))

    def test_queue_wait_uses_persisted_eligibility_and_ends_when_execution_is_claimed(self) -> None:
        prompt = self.inbox / "timed-job.md"
        prompt.write_text("# prompt", encoding="utf-8")
        run_id = "inbox-queue-timing"
        checkpoint = self.repo / ".engineering" / "engineering-runs" / f"{run_id}.json"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(json.dumps({"phase": "COMPLETE"}), encoding="utf-8")
        report = self.repo / ".engineering" / "reports" / f"report_{run_id}.md"
        report.parent.mkdir(parents=True)
        report.write_text("- Terminal state: `COMPLETE`\nCOMPLETE — delivered\n", encoding="utf-8")
        observed_submission = []

        def host_preflight(*_: object, **__: object) -> HostPreflightResult:
            with open_storage(self.repo) as connection:
                observed_submission.extend(connection.execute(
                    "SELECT received_at FROM execution_submissions"
                ).fetchall())
            return HostPreflightResult(
                "PASS", "Engineering Platform", "2.0.0", "2026.12", None, None, "now", 1, ()
            )

        with patch("engineering_platform.inbox_watcher._allocate_run_id", return_value=run_id), patch(
            "engineering_platform.inbox_watcher.execute_host_preflight", side_effect=host_preflight
        ), patch("engineering_platform.inbox_watcher.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess((), 0)
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        self.assertEqual(len(observed_submission), 1)
        spans = {span["phase_name"]: span for span in phase_spans(self.repo, run_id)}
        self.assertIn("QUEUE_WAIT", spans)
        self.assertIn("TOTAL_EXECUTION", spans)
        self.assertEqual(spans["QUEUE_WAIT"]["completed_at"], spans["TOTAL_EXECUTION"]["started_at"])
        summary = timing_summary(self.repo, run_id)
        self.assertEqual(summary["total_wall_time_ms"], spans["TOTAL_EXECUTION"]["duration_ms"])

    def test_background_watcher_detaches_runner_and_keeps_admission_active(self) -> None:
        (self.inbox / "job.md").write_text("# prompt", encoding="utf-8")
        run_id = "inbox-detached-run"
        with patch("engineering_platform.inbox_watcher._allocate_run_id", return_value=run_id), patch(
            "engineering_platform.inbox_watcher.subprocess.Popen"
        ) as popen:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0, background=True), 0)

        command = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        self.assertIn("engineering_platform.inbox_watcher", command)
        self.assertIn("once", command)
        self.assertEqual(environment[inbox_watcher.BACKGROUND_RUN_ID_ENVIRONMENT], run_id)
        self.assertTrue(environment[inbox_watcher.BACKGROUND_JOB_ID_ENVIRONMENT])
        self.assertTrue((self.inbox / "job.md").exists())
        self.assertTrue(inbox_watcher._active_transaction(self.repo))
        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "RUNNER_STARTING")
        self.assertEqual(snapshot["run_id"], run_id)

    def test_active_detached_runner_keeps_scanning_and_publishes_later_inbox_prompts(self) -> None:
        (self.inbox / "running.md").write_text("# Running prompt", encoding="utf-8")
        run_id = "inbox-detached-run"
        with patch("engineering_platform.inbox_watcher._allocate_run_id", return_value=run_id), patch(
            "engineering_platform.inbox_watcher.subprocess.Popen"
        ):
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0, background=True), 0)

        queued = self.inbox / "later.md"
        queued.write_text("# Later prompt", encoding="utf-8")

        self.assertEqual(inbox_watcher.once(self.repo, self.root, 0, background=True), 0)

        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "RUNNER_STARTING")
        self.assertEqual(snapshot["run_id"], run_id)
        self.assertEqual(snapshot["queue_depth"], 2)
        self.assertEqual(
            {item["filename"] for item in snapshot["queue_items"]},
            {"later.md", "running.md"},
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
            self.assertEqual(
                arguments[-4:],
                [
                    "--run-id",
                    "inbox-detached-run",
                    "--admitted-storage-schema",
                    str(inbox_watcher.ENGINEERING_STORAGE_SCHEMA_VERSION),
                ],
            )
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
            patch("engineering_platform.inbox_watcher._allocate_run_id", return_value="inbox-detached-run"),
            patch("engineering_platform.inbox_watcher.subprocess.Popen", side_effect=detach_runner),
            patch("engineering_platform.inbox_watcher.subprocess.run", side_effect=run_command),
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
        with patch("engineering_platform.inbox_watcher._allocate_run_id", return_value=run_id), patch(
            "engineering_platform.inbox_watcher.terminalize_after_host_exit", return_value=None
        ) as reconcile, patch("engineering_platform.inbox_watcher.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ("engineering-execution-host",), 2, "", "Engineering Platform upgrade required."
            )
            code = inbox_watcher.once(self.repo, self.root, 0)

        self.assertEqual(code, 2)
        reconcile.assert_called_once_with(self.repo, run_id)
        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "JOB_FAILED")
        self.assertEqual(snapshot["last_executed_run"], run_id)
        self.assertEqual(snapshot["last_executed_phase"], "FAILED")
        report = self.repo / ".engineering" / "reports" / f"corrected_{run_id}.md"
        self.assertTrue(report.exists())
        self.assertTrue(inbox_watcher._report_matches_terminal_phase(report, "FAILED"))

    def test_corrected_provider_interruption_report_exposes_checkpoint_reason(self) -> None:
        report = inbox_watcher._corrected_terminal_report(
            "inbox-provider-interrupted", "FAILED", "Provider turn interrupted.",
            terminal_condition="provider_turn_interrupted",
        )
        self.assertIn("- Terminal reason: `provider_turn_interrupted`", report)
        self.assertIn("Provider turn interrupted.", report)

    def test_failed_host_preflight_prevents_inbox_claim(self) -> None:
        prompt = self.inbox / "host-failure.md"
        prompt.write_text("# Host failure", encoding="utf-8")
        failed = HostPreflightResult(
            "FAIL", "Engineering Platform", "2.0.0", "2026.12", None, None, "now", 1, ()
        )
        with patch("engineering_platform.inbox_watcher.execute_host_preflight", return_value=failed), patch(
            "engineering_platform.inbox_watcher.subprocess.run"
        ) as run:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 1)
        self.assertTrue(prompt.exists())
        self.assertFalse(list(inbox_watcher.local_folders(self.repo)["Running"].iterdir()))
        run.assert_not_called()
        self.assertEqual(json_status(self.repo)["watcher_state"], "HOST_PREFLIGHT_FAILED")

    def test_failed_workspace_preflight_prevents_inbox_claim(self) -> None:
        prompt = self.inbox / "workspace-failure.md"
        prompt.write_text("# Workspace failure", encoding="utf-8")
        failed = WorkspacePreflightResult(
            "FAIL",
            "DJConnect",
            "repo",
            "main",
            "MANAGED",
            "now",
            1,
            (
                WorkspacePreflightCheck(
                    "git_index_lock_transaction",
                    "FAIL",
                    "Git index lock already exists.",
                    "Stop competing Git processes and restore write access to the repository index before retrying.",
                ),
            ),
        )
        with patch("engineering_platform.inbox_watcher.execute_workspace_preflight", return_value=failed), patch(
            "engineering_platform.inbox_watcher.subprocess.run"
        ) as run, patch("engineering_platform.inbox_watcher.log_event") as log_event:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 1)
        self.assertTrue(prompt.exists())
        self.assertFalse(list(inbox_watcher.local_folders(self.repo)["Running"].iterdir()))
        run.assert_not_called()
        self.assertEqual(json_status(self.repo)["watcher_state"], "WORKSPACE_PREFLIGHT_FAILED")
        log_event.assert_any_call(
            ANY,
            logging.ERROR,
            "workspace_preflight_failed",
            run_id=ANY,
            diagnostic=(
                "git_index_lock_transaction: Git index lock already exists. "
                "Required action: Stop competing Git processes and restore write access "
                "to the repository index before retrying."
            ),
        )

    def test_failed_capability_preflight_prevents_inbox_claim(self) -> None:
        prompt = self.inbox / "capability-failure.md"
        prompt.write_text("# Capability failure", encoding="utf-8")
        failed = CapabilityPreflightResult("FAIL", "now", 1, (), "RETRYABLE_AFTER_HOST_REPAIR", "CAPABILITY", "Repair host.")
        with patch("engineering_platform.inbox_watcher.execute_capability_preflight", return_value=failed), patch(
            "engineering_platform.inbox_watcher.subprocess.run"
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
        self.assertEqual(inbox_watcher._prompt_title("no title", "fallback.md"), "no title")
        self.assertEqual(inbox_watcher._prompt_title("# Visible title\nbody", "fallback.md"), "Visible title")
        self.assertEqual(
            inbox_watcher._prompt_title(
                "TITLE\nRun Qualification Evidence Closure v2\n\nWerk de evidence bij.",
                "fallback.md",
            ),
            "Run Qualification Evidence Closure v2",
        )
        self.assertEqual(
            inbox_watcher._prompt_title(
                "DASHBOARD VALIDATION PROOF V5\n\nPurpose\n\nExecute a fresh proof.",
                "fallback.md",
            ),
            "DASHBOARD VALIDATION PROOF V5",
        )
        self.assertEqual(
            inbox_watcher._prompt_title(
                "Execution Mode: Managed\n\nRUN QUALIFICATION EVIDENCE CLOSURE V2\n\nContext",
                "fallback.md",
            ),
            "RUN QUALIFICATION EVIDENCE CLOSURE V2",
        )
        self.assertEqual(
            inbox_watcher._prompt_title(
                "Execution Mode: Genesis\n\nCreate a new workspace\n\nContext",
                "fallback.md",
            ),
            "Create a new workspace",
        )

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

    @patch("engineering_platform.inbox_watcher.LaunchdProvider")
    def test_main_install_uninstall_status_and_doctor_are_local_only(self, launchd: object) -> None:
        with tempfile.TemporaryDirectory() as home, patch(
            "engineering_platform.inbox_watcher.Path.home", return_value=Path(home)
        ):
            (self.repo / ".gitignore").write_text(".engineering/\n", encoding="utf-8")
            self.assertEqual(
                inbox_watcher.main(["install", "--repo", str(self.repo), "--icloud-root", str(self.root)]),
                2,
            )
            launchd.return_value.install.assert_not_called()
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

    def test_retired_operational_commands_fail_before_workspace_or_storage_access(self) -> None:
        with patch("engineering_platform.inbox_watcher.provision_workspace") as provision:
            for command in ("once", "run", "install"):
                self.assertEqual(inbox_watcher.main([command, "--repo", str(self.repo)]), 2)
        provision.assert_not_called()

    def test_blocked_predecessor_holds_later_inbox_prompts_without_claiming_them(self) -> None:
        (self.inbox / "next.md").write_text("# Later prompt", encoding="utf-8")
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

        with patch("engineering_platform.inbox_watcher.subprocess.run") as run:
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        snapshot = json_status(self.repo)
        run.assert_not_called()
        self.assertTrue((self.inbox / "next.md").exists())
        self.assertEqual(snapshot["watcher_state"], "WAITING_FOR_PREDECESSOR")
        self.assertEqual(snapshot["current_phase"], "WAITING_FOR_PREDECESSOR")
        self.assertEqual(snapshot["blocking_predecessor_run"], "inbox-blocked123")
        self.assertEqual(snapshot["blocking_predecessor_title"], "Blocked predecessor")
        self.assertIn("Retry-Of: inbox-blocked123", snapshot["predecessor_recovery_action"])
        self.assertEqual(snapshot["queue_items"][0]["filename"], "next.md")
        self.assertEqual(snapshot["queue_items"][0]["title"], "Later prompt")

    def test_dismissed_blocked_predecessor_does_not_block_unrelated_admission(self) -> None:
        from engineering_platform.prompt_history import record_prompt_execution
        from engineering_platform.storage import record_execution_dismissal

        run_id = "inbox-dismissed-blocked"
        record_prompt_execution(
            self.repo, run_id=run_id, terminal_state="BLOCKED",
            prompt_title="Historical blocked predecessor", executed_at="2026-08-18T12:00:00Z",
        )
        record_execution_dismissal(
            self.repo, run_id=run_id, terminal_state="BLOCKED",
            dismissed_at="2026-08-18T12:01:00Z", dismissed_by="dashboard_operator",
        )
        (self.inbox / "next.md").write_text("# Unrelated future work", encoding="utf-8")
        status_directory = self.repo / ".engineering" / "status"
        status_directory.mkdir(parents=True)
        (status_directory / "status.json").write_text(json.dumps({
            "last_executed_run": run_id, "last_executed_phase": "BLOCKED",
            "last_executed_filename": "blocked.txt", "last_executed_title": "Historical blocked predecessor",
            "blocking_predecessor_run": run_id, "blocking_predecessor_phase": "BLOCKED",
        }), encoding="utf-8")

        admitted_run_id = "inbox-unrelated-admitted"
        checkpoint = self.repo / ".engineering" / "engineering-runs"
        checkpoint.mkdir(parents=True)
        (checkpoint / f"{admitted_run_id}.json").write_text(
            json.dumps({"phase": "COMPLETE"}), encoding="utf-8",
        )
        report_dir = self.repo / ".engineering" / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / f"report_{admitted_run_id}.md").write_text(
            inbox_watcher._corrected_terminal_report(admitted_run_id, "COMPLETE", None),
            encoding="utf-8",
        )
        with patch("engineering_platform.inbox_watcher._allocate_run_id", return_value=admitted_run_id), patch("engineering_platform.inbox_watcher.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess((), 0)
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        self.assertTrue(run.called)
        snapshot = json_status(self.repo)
        self.assertIsNone(snapshot["blocking_predecessor_run"])
        history = __import__("engineering_platform.prompt_history", fromlist=["prompt_history"]).prompt_history(self.repo)
        historical = next(item for item in history if item["run_id"] == run_id)
        self.assertEqual(historical["status"], "BLOCKED")
        self.assertTrue(historical["dismissed"])
        self.assertIsNone(historical["retry_of"])

    def test_missing_dismissal_evidence_fails_closed(self) -> None:
        self.assertTrue(inbox_watcher.is_active_blocking_predecessor(self.repo, "inbox-unresolved", "BLOCKED"))

    def test_status_publication_reconciles_stale_dismissed_blocker_fields(self) -> None:
        from engineering_platform.prompt_history import record_prompt_execution
        from engineering_platform.storage import record_execution_dismissal

        run_id = "inbox-dismissed-status"
        inbox_watcher.status(
            self.repo, "WAITING_FOR_PREDECESSOR", queued_jobs=1,
            blocking_predecessor_run=run_id, blocking_predecessor_phase="BLOCKED",
            blocking_predecessor_filename="historical.md", blocking_predecessor_title="Historical",
        )
        record_prompt_execution(
            self.repo, run_id=run_id, terminal_state="BLOCKED",
            prompt_title="Historical", executed_at="2026-08-18T12:00:00Z",
        )
        record_execution_dismissal(
            self.repo, run_id=run_id, terminal_state="BLOCKED",
            dismissed_at="2026-08-18T12:01:00Z", dismissed_by="dashboard_operator",
        )

        inbox_watcher.status(self.repo, "WATCHER_IDLE", queued_jobs=1)

        snapshot = json_status(self.repo)
        self.assertIsNone(snapshot["blocking_predecessor_run"])
        self.assertIsNone(snapshot["predecessor_recovery_action"])

    def test_idle_status_surfaces_an_undismissed_terminal_queue_gate(self) -> None:
        run_id = "inbox-terminal-gate"
        status_directory = self.repo / ".engineering" / "status"
        status_directory.mkdir(parents=True)
        (status_directory / "status.json").write_text(
            json.dumps({
                "last_executed_run": run_id,
                "last_executed_phase": "BLOCKED",
                "last_executed_filename": "blocked.md",
                "last_executed_title": "Blocked prompt",
            }),
            encoding="utf-8",
        )

        inbox_watcher.status(self.repo, "WATCHER_IDLE", queued_jobs=0, queue_items=[])

        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["watcher_state"], "WAITING_FOR_PREDECESSOR")
        self.assertEqual(snapshot["current_phase"], "WAITING_FOR_PREDECESSOR")
        self.assertEqual(snapshot["blocking_predecessor_run"], run_id)
        self.assertEqual(snapshot["queue_depth"], 0)

    def test_explicit_retry_of_blocked_predecessor_precedes_later_prompts(self) -> None:
        later = self.inbox / "later.md"
        later.write_text("# Later prompt", encoding="utf-8")
        retry = self.inbox / "retry.md"
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

        with patch("engineering_platform.inbox_watcher._allocate_run_id", return_value=retry_run_id), patch("engineering_platform.inbox_watcher.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess((), 0)
            self.assertEqual(inbox_watcher.once(self.repo, self.root, 0), 0)

        self.assertEqual(
            run.call_args.args[0][-4:],
            [
                "--run-id",
                retry_run_id,
                "--admitted-storage-schema",
                str(inbox_watcher.ENGINEERING_STORAGE_SCHEMA_VERSION),
            ],
        )
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

    def test_retry_admission_preflight_blocks_before_queue_submission_and_reports_recovery(self) -> None:
        original = "# Blocked prompt\n\nKeep this evidence immutable."
        archived = self.repo / ".engineering" / "inbox" / "Failed" / "blocked__preflight.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text(original, encoding="utf-8")
        _, run_id, _ = inbox_watcher._job_id(archived, original)
        failed = WorkspacePreflightResult(
            "FAIL", "DJConnect", "repo", "main", "MANAGED", "now", 1,
            (WorkspacePreflightCheck(
                "git_index_lock_transaction", "FAIL", "Git cannot create the repository index lock.", "Restore Git metadata write access."
            ),),
        )

        with patch("engineering_platform.inbox_watcher.execute_workspace_preflight", return_value=failed):
            with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "Preflight mislukt.*index lock"):
                inbox_watcher.retry_admission_preflight(self.repo, run_id)

        self.assertFalse(list(self.inbox.iterdir()))

    def test_retry_admission_preflight_reports_each_preflight_failure_and_keeps_inbox_empty(self) -> None:
        original = "# Blocked prompt"
        archived = self.repo / ".engineering" / "inbox" / "Failed" / "blocked__preflight.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text(original, encoding="utf-8")
        _, run_id, _ = inbox_watcher._job_id(archived, original)
        host_failure = HostPreflightResult(
            "FAIL", "Engineering Platform", "2.0.0", "2026.12", None, None, "now", 1,
            (HostPreflightCheck("git_metadata", "FAIL", "Git metadata is read-only.", "Restore write access."),),
        )
        capability_failure = CapabilityPreflightResult(
            "FAIL", "now", 1,
            (CapabilityCheck("provider", "FAIL", "Required provider is unavailable.", "Repair the provider."),),
            "RETRYABLE_AFTER_HOST_REPAIR", "CAPABILITY", "Repair the provider.",
        )
        with patch("engineering_platform.inbox_watcher.execute_host_preflight", return_value=host_failure):
            with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "Git metadata is read-only"):
                inbox_watcher.retry_admission_preflight(self.repo, run_id)
        with patch("engineering_platform.inbox_watcher.execute_capability_preflight", return_value=capability_failure):
            with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "Required provider is unavailable"):
                inbox_watcher.retry_admission_preflight(self.repo, run_id)
        self.assertFalse(list(self.inbox.iterdir()))

    def test_predecessor_retry_preflight_requires_a_blocker_and_returns_its_run_id(self) -> None:
        with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "geen geblokkeerde"):
            inbox_watcher.predecessor_retry_admission_preflight(self.repo)
        predecessor = {"run_id": "inbox-blocked"}
        with (
            patch("engineering_platform.inbox_watcher._blocking_predecessor", return_value=predecessor),
            patch("engineering_platform.inbox_watcher.retry_admission_preflight") as preflight,
        ):
            self.assertEqual(inbox_watcher.predecessor_retry_admission_preflight(self.repo), "inbox-blocked")
            preflight.assert_called_once_with(self.repo, "inbox-blocked")

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

    def test_dismiss_terminal_execution_persists_immutable_handling_and_blocks_retry(self) -> None:
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
        self.assertEqual(outcome["terminal_state"], "BLOCKED")
        self.assertEqual(outcome["handling_state"], "DISMISSED")
        with self.assertRaisesRegex(inbox_watcher.RetrySubmissionError, "al afgesloten"):
            inbox_watcher.submit_execution_retry(self.repo, self.root, run_id)
        history = __import__("engineering_platform.prompt_history", fromlist=["prompt_history"]).prompt_history(self.repo)
        self.assertTrue(history[0]["dismissed"])
        self.assertEqual(history[0]["status"], "BLOCKED")

    def test_dismisses_an_older_terminal_execution_without_erasing_newer_status(self) -> None:
        from engineering_platform.prompt_history import record_prompt_execution

        older_run = "inbox-historical-failed"
        newer_run = "inbox-newer-complete"
        record_prompt_execution(
            self.repo, run_id=older_run, terminal_state="FAILED",
            prompt_title="Historical failure", executed_at="2026-08-15T20:00:00Z",
        )
        status = self.repo / ".engineering" / "status"
        status.mkdir(parents=True, exist_ok=True)
        (status / "status.json").write_text(
            json.dumps(
                {
                    "watcher_state": "WATCHER_IDLE",
                    "last_executed_run": newer_run,
                    "last_executed_phase": "COMPLETE",
                    "last_executed_title": "Newer complete execution",
                    "queue_depth": 0,
                    "queue_items": [],
                }
            ),
            encoding="utf-8",
        )

        outcome = inbox_watcher.dismiss_execution(self.repo, older_run)

        self.assertTrue(outcome["dismissed"])
        snapshot = json_status(self.repo)
        self.assertEqual(snapshot["last_executed_run"], newer_run)
        history = __import__("engineering_platform.prompt_history", fromlist=["prompt_history"]).prompt_history(self.repo)
        older = next(item for item in history if item["run_id"] == older_run)
        self.assertTrue(older["dismissed"])
        self.assertFalse(older["can_retry"])

    def test_dismissal_explains_that_another_active_execution_must_finish_first(self) -> None:
        run_id = "inbox-dismissed"
        runs = self.repo / ".engineering" / "engineering-runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / f"{run_id}.json").write_text(json.dumps({"phase": "FAILED"}), encoding="utf-8")
        status = self.repo / ".engineering" / "status"
        status.mkdir(parents=True, exist_ok=True)
        (status / "status.json").write_text(
            json.dumps({"watcher_state": "ENGINEERING_RUN_ACTIVE", "run_id": "inbox-new-run"}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            inbox_watcher.RetrySubmissionError,
            "nadat de andere actieve uitvoering is afgerond",
        ):
            inbox_watcher.dismiss_execution(self.repo, run_id)

def json_status(repo: Path) -> dict[str, object]:
    import json
    return json.loads((repo / ".engineering" / "status" / "status.json").read_text(encoding="utf-8"))
