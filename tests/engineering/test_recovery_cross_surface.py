from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from engineering_platform import server_console_services as dashboard, dashboard_state
from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.execution_lifecycle import projection as lifecycle_projection
from engineering_platform.execution_models import AgentResult
from engineering_platform.execution_reporting import (
    _execution_receipt_projection,
    generate_terminal_report,
)
from engineering_platform.managed_autonomy import (
    append_action,
    append_pr_check_observation,
    append_validation_observation,
    record_gate,
    terminal_snapshot,
)
from engineering_platform.platform_version import EngineeringPlatformManifest
from engineering_platform.prompt_history import record_prompt_execution
from engineering_platform.producer import ProducerMetadata
from engineering_platform.provider_recovery import (
    claim_replacement_launch,
    create_recovery_available,
    load_recovery_state,
    persist_recovery_agent_result,
    record_provider_started,
    record_replacement_terminal,
    transition_recovery_state,
)
from engineering_platform.provider_usage import AUTHORITATIVE, ProviderInvocation, persist_provider_invocation
from engineering_platform.storage import (
    record_run_qualification_context,
    record_submission,
    record_validation_control_result,
    record_validation_profile,
    store_projection,
)


class RecoveredRunCrossSurfaceTests(unittest.TestCase):
    run_id = "inbox-recovered-cross-surface"
    submission_id = "submission-human-icloud-recovered"
    producer_id = "human:icloud-operator"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(("git", "init", "--initial-branch=main", str(self.root)), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(self.root), "config", "user.email", "test@example.invalid"), check=True)
        subprocess.run(("git", "-C", str(self.root), "config", "user.name", "Test"), check=True)
        (self.root / "evidence.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.root), "add", "evidence.txt"), check=True)
        subprocess.run(("git", "-C", str(self.root), "commit", "-m", "baseline"), check=True, capture_output=True)
        self.commit = subprocess.run(
            ("git", "-C", str(self.root), "rev-parse", "HEAD"), check=True, text=True, capture_output=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fixture(self, *, qualified: bool) -> TransactionState:
        state = TransactionState(
            self.run_id, "pcvantol/djconnect", str(self.root / "prompt.md"), "COMPLETE",
            branch="codex/recovered", last_verified_sha=self.commit,
            implementation_branch="codex/recovered", implementation_pull_request=101,
            implementation_merge_commit=self.commit, finalization_branch="codex/recovered-finalization",
            finalization_pull_request=102, finalization_merge_commit=self.commit, terminal=True,
        )
        Path(state.prompt_path).write_text("stale recovery state: EXHAUSTED\n", encoding="utf-8")
        StateStore(self.root / ".engineering" / "engineering-runs").save(state)
        record_submission(
            self.root, submission_id=self.submission_id, producer_id=self.producer_id,
            producer_type="HUMAN", producer_version="1.0", contract_version="1.0",
            prompt_content="structured human submission", prompt_metadata={}, target_identity={},
            original_envelope={}, received_at="2026-08-30T00:00:00+00:00", link_run_id=self.run_id,
            execution_context={"context_version": "1.0", "action_intent": "MUTATING_DELIVERY", "ingress": "icloud_text"},
        )
        record_run_qualification_context(
            self.root, run_id=self.run_id, submission_id=self.submission_id, fresh_submission=True,
            retry_parent_run_id=None, resume_parent_run_id=None, recorded_at="2026-08-30T00:00:00+00:00",
        )
        record_validation_profile(
            self.root, run_id=self.run_id, selected_validation_tier="DOCUMENTATION",
            validation_profile_version="1.0", required_validation_controls=("git_diff_check",),
            recorded_at="2026-08-30T00:00:00+00:00",
        )
        record_validation_control_result(
            self.root, run_id=self.run_id, validation_id="git_diff_check", category="repository",
            control_identity="git diff --check", required_for_profile=True, execution_status="EXECUTED",
            result="PASS" if qualified else "FAIL", evidence_ref="local",
            observed_at="2026-08-30T00:00:01+00:00", currentness=1,
        )
        for action in ("IMPLEMENTATION", "POST_IMPLEMENTATION_MERGE", "FINALIZATION", "RECONCILIATION", "CLEANUP"):
            append_action(self.root, run_id=self.run_id, action=action, authority="AUTONOMOUS_EP_ACTION")
        for gate, pr in (("IMPLEMENTATION_MERGE_APPROVAL", 101), ("FINALIZATION_MERGE_APPROVAL", 102)):
            record_gate(self.root, run_id=self.run_id, gate_type=gate, status="SATISFIED", related_pr=pr, phase="MERGE", resolution_actor="operator")
        append_action(self.root, run_id=self.run_id, action="GITHUB_REQUIRED_CHECK", authority="EXTERNAL_PLATFORM_EVENT", actor="github", evidence_ref="check")
        append_validation_observation(self.root, run_id=self.run_id, control="git_diff_check", state="PASS" if qualified else "FAIL", required=True, currentness=2)
        for role, pr in (("IMPLEMENTATION", 101), ("FINALIZATION", 102)):
            append_pr_check_observation(self.root, run_id=self.run_id, pr_number=pr, pr_role=role, pr_state="MERGED", merge_commit=self.commit, required_checks_state="PASS", evidence_ref="github", currentness=1)
        original = persist_provider_invocation(self.root, ProviderInvocation(
            run_id=self.run_id, ordinal=1, provider="codex_cli", model="gpt-5.6-terra", model_authority=AUTHORITATIVE,
            phase="PROVIDER_EXECUTION", role="IMPLEMENTATION", started_at="2026-08-30T00:00:00+00:00", completed_at="2026-08-30T00:00:01+00:00", duration_ms=1000, usage={},
        ))
        recovery = create_recovery_available(self.root, run_id=self.run_id, triggering_invocation_id=original, lifecycle_phase="EXECUTE_AGENT", branch="codex/recovered", worktree_identity=str(self.root), lease_id=None)
        self.assertTrue(transition_recovery_state(self.root, run_id=self.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING"))
        claim = claim_replacement_launch(self.root, run_id=self.run_id)
        self.assertIsNotNone(claim)
        self.assertTrue(record_provider_started(self.root, run_id=self.run_id, receipt_id=str(claim["receipt_id"]), pid=os.getpid(), process_group=os.getpgrp()))
        replacement = str(recovery["replacement_invocation_id"])
        result = persist_recovery_agent_result(self.root, run_id=self.run_id, invocation_id=replacement, result=AgentResult("COMPLETE", branch="codex/recovered"))
        self.assertTrue(record_replacement_terminal(self.root, run_id=self.run_id, outcome="SUCCESS", result_evidence_ref=result))
        persist_provider_invocation(self.root, ProviderInvocation(
            run_id=self.run_id, ordinal=2, provider="codex_cli", model="gpt-5.6-terra", model_authority=AUTHORITATIVE,
            phase="PROVIDER_EXECUTION", role="IMPLEMENTATION", started_at="2026-08-30T00:00:02+00:00", completed_at="2026-08-30T00:00:03+00:00", duration_ms=1000, usage={}, invocation_id=replacement,
        ))
        snapshot = terminal_snapshot(self.root, run_id=self.run_id, execution_outcome="COMPLETE", implementation_pr=101, finalization_pr=102, repository_state="MERGED_RECONCILED", workspace_state="WORKSPACE_READY", main_origin_sync="YES", worktree_state="CLEAN", active_blocker="NONE", recovery_required="NO", lineage_available=True, persist=True)
        self.assertEqual(
            snapshot["run_qualification"], "QUALIFIED" if qualified else "NOT_QUALIFIED",
            snapshot["qualification_failure_reasons"],
        )
        with dashboard.open_storage(self.root) as connection:
            connection.execute(
                "INSERT INTO execution_validation_command_invocations "
                "(run_id,validation_id,command_id,category,control_identity,required_for_profile,started_at,currentness,evidence_ref) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (self.run_id, "git_diff_check", "validation-cross-surface", "repository", "git diff --check", 1, "2026-08-30T00:00:04+00:00", 3, "command_invocation"),
            )
            connection.execute(
                "INSERT INTO execution_validation_command_terminals "
                "(run_id,command_id,completed_at,duration_ms,exit_code,result,evidence_ref) VALUES(?,?,?,?,?,?,?)",
                (self.run_id, "validation-cross-surface", "2026-08-30T00:00:05+00:00", 1000, 0, "PASS", "terminal"),
            )
        record_prompt_execution(self.root, run_id=self.run_id, terminal_state="COMPLETE", prompt_title="Recovered submission", executed_at="2026-08-30T00:00:05+00:00")
        with dashboard.open_storage(self.root) as connection:
            store_projection(connection, "live_status", {"run_id": self.run_id, "phase": "COMPLETE", "repository_state": "MERGED_RECONCILED", "workspace_state": "WORKSPACE_READY"})
        return state

    def test_recovered_terminal_run_has_one_consistent_cross_surface_projection(self) -> None:
        state = self._fixture(qualified=True)
        recovery = load_recovery_state(self.root, self.run_id)
        self.assertIsNotNone(recovery)
        receipt = "\n".join(_execution_receipt_projection(
            self.root, state, ProducerMetadata(self.producer_id, "HUMAN", "1.0"),
        ))
        report = generate_terminal_report(self.root, state, manifest=EngineeringPlatformManifest.load(Path(__file__).parents[2] / "src" / "engineering_platform" / "ENGINEERING_PLATFORM_VERSION.json")).read_text(encoding="utf-8")
        dashboard_payload = json.loads(dashboard_state.status(self.root))
        history = json.loads(dashboard._prompt_history_detail(self.root, self.run_id))
        self.assertIn("- Recovery State: `RECOVERED`", receipt)
        self.assertIn("- Recovery State: `RECOVERED`", report)
        self.assertIn(f"- Recovery Triggering Invocation ID: `{recovery['triggering_invocation_id']}`", receipt)
        self.assertIn(f"- Recovery Replacement Invocation ID: `{recovery['replacement_invocation_id']}`", receipt)
        self.assertIn("- Recovery Attempt / Budget: `1/1`", receipt)
        self.assertIn(f"- Producer ID: `{self.producer_id}`", receipt)
        self.assertIn(f"- Submission ID: `{self.submission_id}`", receipt)
        self.assertIn(f"- Producer ID: `{self.producer_id}`", report)
        self.assertIn("- Required Validation State: `PASS`", receipt)
        self.assertIn("- Run Qualification: `QUALIFIED`", receipt)
        self.assertIn("- Receipt Status: `COMPLETE`", receipt)
        self.assertEqual(dashboard_payload["lifecycle"]["recovery"]["state"], "RECOVERED")
        self.assertEqual(dashboard_payload["lifecycle"]["recovery"]["result"], "SUCCESS")
        self.assertEqual(dashboard_payload["lifecycle"]["recovery"]["triggering_invocation_id"], recovery["triggering_invocation_id"])
        self.assertEqual(dashboard_payload["lifecycle"]["recovery"]["replacement_invocation_id"], recovery["replacement_invocation_id"])
        self.assertEqual(dashboard_payload["lifecycle"]["recovery"]["recovery_ordinal"], 1)
        self.assertEqual(dashboard_payload["lifecycle"]["recovery"]["maximum_attempts"], 1)
        self.assertEqual(dashboard_payload["lifecycle"]["qualification"]["required_validation_state"], "PASS")
        self.assertEqual(dashboard_payload["lifecycle"]["qualification"]["run_qualification"], "QUALIFIED")
        self.assertEqual(history["lifecycle"]["recovery"]["state"], "RECOVERED")
        self.assertEqual(history["lifecycle"]["qualification"]["run_qualification"], "QUALIFIED")
        self.assertEqual(history["history"]["run_id"], self.run_id)
        self.assertEqual(history["history"]["submission_id"], self.submission_id)
        self.assertEqual(history["history"]["producer_id"], self.producer_id)
        self.assertIsNone(history["history"]["retry_of"])
        self.assertEqual(history["history"]["execution_activity_summary"]["activity"]["overall_activity_total"], 3)
        self.assertEqual(lifecycle_projection(self.root, self.run_id)["terminal_state"], "COMPLETE")
        history_rows = json.loads(dashboard._prompt_history(self.root))["runs"]
        self.assertEqual(len(history_rows), 1)
        self.assertEqual(history_rows[0]["run_id"], self.run_id)
        self.assertEqual(history_rows[0]["submission_id"], self.submission_id)

    def test_recovered_but_not_qualified_stays_not_qualified_in_dashboard_projection(self) -> None:
        self._fixture(qualified=False)
        payload = json.loads(dashboard_state.status(self.root))
        self.assertEqual(payload["lifecycle"]["recovery"]["state"], "RECOVERED")
        self.assertEqual(payload["lifecycle"]["qualification"]["run_qualification"], "NOT_QUALIFIED")
