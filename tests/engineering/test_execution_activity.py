from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import json
import os
import subprocess
import unittest

from engineering_platform.execution_activity import (
    CODEX_COMMAND_DEFINITION,
    cumulative_activity,
    live_worktree_snapshot,
    persist_terminal_activity_summary,
    terminal_activity_summary,
    build_terminal_activity_summary,
)
from engineering_platform import server_console_services as dashboard
from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.execution_models import AgentResult
from engineering_platform.prompt_history import record_prompt_execution
from engineering_platform.provider_usage import AUTHORITATIVE, ProviderInvocation, persist_provider_invocation
from engineering_platform.storage import open_storage
from engineering_platform.provider_recovery import (
    claim_replacement_launch,
    create_recovery_available,
    load_recovery_state,
    persist_recovery_agent_result,
    record_provider_started,
    record_replacement_terminal,
    transition_recovery_state,
)


class ExecutionActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _provider(self, ordinal: int, role: str, phase: str) -> None:
        persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id="run-activity", ordinal=ordinal, provider="codex_cli", model="gpt-5.6-terra",
                model_authority=AUTHORITATIVE, phase=phase, role=role,
                started_at="2026-08-30T00:00:00+00:00", completed_at="2026-08-30T00:00:01+00:00",
                duration_ms=1000, usage={},
            ),
        )

    def test_cumulative_activity_separates_primary_reviewer_and_host_validation(self) -> None:
        self._provider(1, "IMPLEMENTATION", "PROVIDER_EXECUTION")
        self._provider(2, "reviewer:security", "CAPABILITY_REVIEW")
        with open_storage(self.root) as connection:
            connection.execute(
                "INSERT INTO execution_validation_command_invocations "
                "(run_id,validation_id,command_id,category,control_identity,required_for_profile,started_at,currentness,evidence_ref) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("run-activity", "unit", "command-1", "agent", "unit", 1, "2026-08-30T00:00:00+00:00", 0, "command_invocation"),
            )
        activity = cumulative_activity(self.root, "run-activity")
        self.assertEqual(activity["codex_command_definition"], CODEX_COMMAND_DEFINITION)
        self.assertEqual(activity["primary_codex_commands_total"], 1)
        self.assertEqual(activity["reviewer_codex_commands_total"], 1)
        self.assertEqual(activity["host_validation_commands_total"], 1)
        self.assertEqual(activity["overall_activity_total"], 3)

    def test_terminal_summary_is_insert_only_and_legacy_is_unavailable(self) -> None:
        self.assertIsNone(terminal_activity_summary(self.root, "historical-run"))
        with open_storage(self.root) as connection:
            connection.execute(
                "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                ("run-activity", "{}", "COMPLETE", "2026-08-30T00:00:00+00:00"),
            )
        first = persist_terminal_activity_summary(self.root, {"run_id": "run-activity", "summary_version": 1, "activity": {"overall_activity_total": 1}, "terminal_delivery_diff": {}})
        second = persist_terminal_activity_summary(self.root, {"run_id": "run-activity", "summary_version": 1, "activity": {"overall_activity_total": 99}, "terminal_delivery_diff": {}})
        self.assertEqual(first, second)
        self.assertEqual(second["activity"]["overall_activity_total"], 1)

    def test_prompt_history_projects_only_the_persisted_terminal_summary(self) -> None:
        record_prompt_execution(
            self.root, run_id="run-activity", terminal_state="COMPLETE",
            prompt_title="Activity", executed_at="2026-08-30T00:00:00+00:00",
        )
        StateStore(self.root / ".engineering" / "engineering-runs").save(
            TransactionState("run-activity", "pcvantol/djconnect", "prompt.md", "COMPLETE", terminal=True)
        )
        persist_terminal_activity_summary(
            self.root,
            {
                "run_id": "run-activity", "summary_version": 1,
                "activity": {"overall_activity_total": 3},
                "terminal_delivery_diff": {"total_unique_changed_paths": 2},
            },
        )
        payload = json.loads(dashboard._prompt_history_detail(self.root, "run-activity"))
        self.assertEqual(payload["history"]["execution_activity_summary"]["activity"]["overall_activity_total"], 3)
        self.assertEqual(payload["history"]["execution_activity_summary"]["terminal_delivery_diff"]["total_unique_changed_paths"], 2)

        record_prompt_execution(
            self.root, run_id="legacy-run", terminal_state="COMPLETE",
            prompt_title="Legacy", executed_at="2026-08-29T00:00:00+00:00",
        )
        StateStore(self.root / ".engineering" / "engineering-runs").save(
            TransactionState("legacy-run", "pcvantol/djconnect", "prompt.md", "COMPLETE", terminal=True)
        )
        legacy = json.loads(dashboard._prompt_history_detail(self.root, "legacy-run"))
        self.assertIsNone(legacy["history"]["execution_activity_summary"])

    def test_live_snapshot_is_volatile_and_never_delivery_evidence(self) -> None:
        snapshot = live_worktree_snapshot(self.root)
        self.assertTrue(snapshot["volatile"])
        self.assertEqual(snapshot["kind"], "LIVE_WORKTREE_SNAPSHOT")
        self.assertIn("never cumulative run delivery", snapshot["meaning"])

    def test_terminal_delivery_uses_the_evidence_bundle_range_and_proven_rename(self) -> None:
        subprocess.run(("git", "init", "--initial-branch=main", str(self.root)), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(self.root), "config", "user.email", "test@example.invalid"), check=True)
        subprocess.run(("git", "-C", str(self.root), "config", "user.name", "Test"), check=True)
        (self.root / "old.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.root), "add", "old.txt"), check=True)
        subprocess.run(("git", "-C", str(self.root), "commit", "-m", "baseline"), check=True, capture_output=True)
        baseline = subprocess.run(("git", "-C", str(self.root), "rev-parse", "HEAD"), check=True, text=True, capture_output=True).stdout.strip()
        (self.root / "old.txt").rename(self.root / "renamed.txt")
        (self.root / "added.txt").write_text("added\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.root), "add", "-A"), check=True)
        subprocess.run(("git", "-C", str(self.root), "commit", "-m", "delivery"), check=True, capture_output=True)
        target = subprocess.run(("git", "-C", str(self.root), "rev-parse", "HEAD"), check=True, text=True, capture_output=True).stdout.strip()
        bundle = SimpleNamespace(
            target_workspace=str(self.root), target_commit=target,
            transaction_baseline_sha=baseline,
            files_added=("added.txt",), files_modified=(), files_removed=(),
            files_renamed=(("old.txt", "renamed.txt"),),
            changed_files=("added.txt", "old.txt", "renamed.txt"),
        )
        state = SimpleNamespace(
            run_id="run-activity", implementation_merge_commit=target,
            finalization_merge_commit=None, implementation_pull_request=12,
            finalization_pull_request=None, reconciliation_pull_request=None,
        )

        summary = build_terminal_activity_summary(self.root, state, bundle)

        delivery = summary["terminal_delivery_diff"]
        self.assertEqual(delivery["transaction_baseline_sha"], baseline)
        self.assertEqual(delivery["terminal_target_sha"], target)
        self.assertEqual(delivery["added"], ["added.txt"])
        self.assertEqual(delivery["renamed"], [{"from": "old.txt", "to": "renamed.txt"}])
        self.assertEqual(delivery["total_unique_changed_paths"], 3)
        self.assertEqual(delivery["phase_attribution"]["implementation"]["renamed"], [{"from": "old.txt", "to": "renamed.txt"}])

    def test_recovered_provider_attempts_remain_separate_persisted_activity(self) -> None:
        run_id = "recovered-activity"
        StateStore(self.root / ".engineering" / "engineering-runs").save(
            TransactionState(run_id, "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT", branch="codex/recovered")
        )
        original_invocation = persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id=run_id, ordinal=1, provider="codex_cli", model="gpt-5.6-terra",
                model_authority=AUTHORITATIVE, phase="PROVIDER_EXECUTION", role="IMPLEMENTATION",
                started_at="2026-08-30T00:00:00+00:00", completed_at="2026-08-30T00:00:01+00:00",
                duration_ms=1000, usage={},
            ),
        )
        recovery = create_recovery_available(
            self.root, run_id=run_id, triggering_invocation_id=original_invocation,
            lifecycle_phase="EXECUTE_AGENT", branch="codex/recovered",
            worktree_identity=str(self.root), lease_id=None,
        )
        self.assertTrue(transition_recovery_state(
            self.root, run_id=run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING"
        ))
        claim = claim_replacement_launch(self.root, run_id=run_id)
        assert claim is not None
        self.assertTrue(record_provider_started(
            self.root, run_id=run_id, receipt_id=str(claim["receipt_id"]), pid=os.getpid(),
            process_group=os.getpgrp(),
        ))
        result_ref = persist_recovery_agent_result(
            self.root, run_id=run_id, invocation_id=str(recovery["replacement_invocation_id"]),
            result=AgentResult("COMPLETE", branch="codex/recovered"),
        )
        self.assertTrue(record_replacement_terminal(
            self.root, run_id=run_id, outcome="SUCCESS", result_evidence_ref=result_ref
        ))
        persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id=run_id, ordinal=2, provider="codex_cli", model="gpt-5.6-terra",
                model_authority=AUTHORITATIVE, phase="PROVIDER_EXECUTION", role="IMPLEMENTATION",
                started_at="2026-08-30T00:00:02+00:00", completed_at="2026-08-30T00:00:03+00:00",
                duration_ms=1000, usage={}, invocation_id=str(recovery["replacement_invocation_id"]),
            ),
        )
        with open_storage(self.root) as connection:
            connection.execute(
                "INSERT INTO execution_validation_command_invocations "
                "(run_id,validation_id,command_id,category,control_identity,required_for_profile,started_at,currentness,evidence_ref) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, "unit", "host-validation", "host", "unit", 1, "2026-08-30T00:00:00+00:00", 0, "command_invocation"),
            )
            usage_rows = connection.execute(
                "SELECT input_tokens,output_tokens FROM provider_invocations WHERE run_id=? ORDER BY ordinal", (run_id,)
            ).fetchall()
        self.assertEqual(load_recovery_state(self.root, run_id)["state"], "RECOVERED")
        self.assertEqual(usage_rows, [(None, None), (None, None)])
        activity = cumulative_activity(self.root, run_id)
        self.assertEqual(activity["primary_codex_commands_total"], 2)
        self.assertEqual(activity["host_validation_commands_total"], 1)
        self.assertEqual(activity["overall_activity_total"], 3)
        summary = persist_terminal_activity_summary(
            self.root,
            {"run_id": run_id, "summary_version": 1, "activity": activity, "terminal_delivery_diff": {}},
        )
        self.assertEqual(terminal_activity_summary(self.root, run_id), summary)
