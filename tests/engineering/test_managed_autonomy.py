from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import os

from engineering_platform.managed_autonomy import (
    append_action,
    append_pr_check_observation,
    append_validation_observation,
    record_gate,
    terminal_snapshot,
)
from engineering_platform.storage import (
    record_run_qualification_context,
    record_validation_control_result,
    record_validation_profile,
)
from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.execution_models import AgentResult
from engineering_platform.provider_recovery import (
    claim_replacement_launch, create_recovery_available, load_recovery_state,
    persist_recovery_agent_result, record_provider_started, record_replacement_terminal,
    transition_recovery_state,
)
from engineering_platform.provider_usage import AUTHORITATIVE, ProviderInvocation, persist_provider_invocation


class ManagedAutonomyEvidenceTest(unittest.TestCase):
    @staticmethod
    def _recovered(root: Path, run: str, *, outcome: str = "SUCCESS") -> None:
        StateStore(root / ".engineering" / "engineering-runs").save(
            TransactionState(run, "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT", branch="codex/recovered")
        )
        original = persist_provider_invocation(root, ProviderInvocation(
            run_id=run, ordinal=1, provider="codex_cli", model="gpt-5.6-terra", model_authority=AUTHORITATIVE,
            phase="PROVIDER_EXECUTION", role="IMPLEMENTATION", started_at="2026-08-30T00:00:00+00:00",
            completed_at="2026-08-30T00:00:01+00:00", duration_ms=1000, usage={},
        ))
        recovery = create_recovery_available(root, run_id=run, triggering_invocation_id=original,
            lifecycle_phase="EXECUTE_AGENT", branch="codex/recovered", worktree_identity=str(root), lease_id=None)
        assert transition_recovery_state(root, run_id=run, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING")
        claim = claim_replacement_launch(root, run_id=run)
        assert claim is not None
        assert record_provider_started(root, run_id=run, receipt_id=str(claim["receipt_id"]), pid=os.getpid(), process_group=os.getpgrp())
        result = persist_recovery_agent_result(root, run_id=run, invocation_id=str(recovery["replacement_invocation_id"]), result=AgentResult("COMPLETE", branch="codex/recovered"))
        assert record_replacement_terminal(root, run_id=run, outcome=outcome, result_evidence_ref=result)
        persist_provider_invocation(root, ProviderInvocation(
            run_id=run, ordinal=2, provider="codex_cli", model="gpt-5.6-terra", model_authority=AUTHORITATIVE,
            phase="PROVIDER_EXECUTION", role="IMPLEMENTATION", started_at="2026-08-30T00:00:02+00:00",
            completed_at="2026-08-30T00:00:03+00:00", duration_ms=1000, usage={},
            invocation_id=str(recovery["replacement_invocation_id"]),
        ))

    def _qualified(self, root: Path, *, retry_parent: str | None = None, resume_parent: str | None = None) -> dict[str, object]:
        run = "inbox-managed-proof"
        record_run_qualification_context(
            root, run_id=run, submission_id="submission-managed-proof", fresh_submission=retry_parent is None and resume_parent is None,
            retry_parent_run_id=retry_parent, resume_parent_run_id=resume_parent, recorded_at="2026-08-28T00:00:00+00:00",
        )
        record_validation_profile(
            root, run_id=run, selected_validation_tier="DOCUMENTATION", validation_profile_version="1.0",
            required_validation_controls=("git_diff_check",), recorded_at="2026-08-28T00:00:00+00:00",
        )
        record_validation_control_result(
            root, run_id=run, validation_id="git_diff_check", category="repository",
            control_identity="git diff --check", required_for_profile=True, execution_status="EXECUTED",
            result="PASS", evidence_ref="local", observed_at="2026-08-28T00:00:01+00:00", currentness=1,
        )
        for action in (
            "IMPLEMENTATION",
            "POST_IMPLEMENTATION_MERGE",
            "FINALIZATION",
            "RECONCILIATION",
            "CLEANUP",
        ):
            append_action(root, run_id=run, action=action, authority="AUTONOMOUS_EP_ACTION")
        for gate, pr in (
            ("IMPLEMENTATION_MERGE_APPROVAL", 101),
            ("FINALIZATION_MERGE_APPROVAL", 102),
        ):
            record_gate(
                root,
                run_id=run,
                gate_type=gate,
                status="SATISFIED",
                related_pr=pr,
                phase="MERGE",
                resolution_actor="operator",
            )
        append_action(
            root,
            run_id=run,
            action="GITHUB_REQUIRED_CHECK",
            authority="EXTERNAL_PLATFORM_EVENT",
            actor="github",
            evidence_ref="check",
        )
        append_validation_observation(
            root, run_id=run, control="git_diff_check", state="PASS", required=True, currentness=2
        )
        for role, pr in (("IMPLEMENTATION", 101), ("FINALIZATION", 102)):
            append_pr_check_observation(
                root, run_id=run, pr_number=pr, pr_role=role, pr_state="MERGED",
                merge_commit="a" * 40, required_checks_state="PASS",
                evidence_ref="github", currentness=1,
            )
        return terminal_snapshot(
            root,
            run_id=run,
            execution_outcome="COMPLETE",
            implementation_pr=101,
            finalization_pr=102,
            repository_state="MERGED_RECONCILED",
            workspace_state="WORKSPACE_READY",
            main_origin_sync="YES",
            worktree_state="CLEAN",
            active_blocker="NONE",
            recovery_required="NO",
            lineage_available=True,
        )

    def test_expected_operator_gates_are_not_manual_interventions_and_qualify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._qualified(Path(directory))
        self.assertEqual(snapshot["managed_autonomy_qualification"], "QUALIFIED")
        self.assertEqual(snapshot["unplanned_manual_intervention_count"], 0)
        self.assertEqual(snapshot["expected_operator_gate_count"], 2)
        self.assertGreater(snapshot["autonomous_ep_action_count"], 0)
        self.assertGreater(snapshot["external_platform_event_count"], 0)
        self.assertEqual(snapshot["fresh_submission"], "YES")
        self.assertEqual(snapshot["retry_parent"], "NONE")
        self.assertEqual(snapshot["resume_parent"], "NONE")
        self.assertEqual(snapshot["pr_checks"]["IMPLEMENTATION"]["required_checks_state"], "PASS")
        self.assertEqual(snapshot["pr_checks"]["FINALIZATION"]["required_checks_state"], "PASS")
        self.assertEqual(snapshot["implementation_delivery"], "COMPLETE")
        self.assertEqual(snapshot["finalization_delivery"], "COMPLETE")
        self.assertEqual(snapshot["execution_mode"], "MANAGED")
        self.assertEqual(snapshot["required_validation_state"], "PASS")
        self.assertFalse(snapshot["validation_projection_conflict"])

    def test_recovered_run_qualification_uses_only_ordinary_terminal_gates(self) -> None:
        cases = (
            ("validation-fail", {"validation": "FAIL"}, "NOT_QUALIFIED"),
            ("implementation-incomplete", {"implementation": None}, "NOT_QUALIFIED"),
            ("finalization-incomplete", {"finalization": None}, "NOT_QUALIFIED"),
            ("reconciliation-incomplete", {"repository": "ACTIVE"}, "NOT_QUALIFIED"),
            ("complete", {}, "QUALIFIED"),
            ("recovered-alone", {"ordinary_evidence": False}, "NOT_QUALIFIED"),
        )
        for name, change, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = "inbox-managed-proof"
                self._recovered(root, run)
                self.assertEqual(load_recovery_state(root, run)["state"], "RECOVERED")
                if change.get("ordinary_evidence") is False:
                    snapshot = terminal_snapshot(root, run_id=run, execution_outcome="COMPLETE", implementation_pr=None, finalization_pr=None, repository_state="ACTIVE", workspace_state="ACTIVE", main_origin_sync="NO", worktree_state="DIRTY", active_blocker="UNKNOWN", recovery_required="YES", persist=True)
                else:
                    self._qualified(root)
                    if change.get("validation") == "FAIL":
                        record_validation_control_result(root, run_id=run, validation_id="git_diff_check", category="repository", control_identity="git diff --check", required_for_profile=True, execution_status="EXECUTED", result="FAIL", evidence_ref="local", observed_at="2026-08-30T00:01:00+00:00", currentness=3)
                    snapshot = terminal_snapshot(root, run_id=run, execution_outcome="COMPLETE", implementation_pr=change.get("implementation", 101), finalization_pr=change.get("finalization", 102), repository_state=change.get("repository", "MERGED_RECONCILED"), workspace_state="WORKSPACE_READY", main_origin_sync="YES", worktree_state="CLEAN", active_blocker="NONE", recovery_required="NO", lineage_available=True, persist=True)
                self.assertEqual(snapshot["run_qualification"], expected)

    def test_exhausted_provider_recovery_with_failed_terminal_cannot_qualify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = "exhausted-recovery"
            self._recovered(root, run, outcome="INTERRUPTED")
            self.assertEqual(load_recovery_state(root, run)["state"], "EXHAUSTED")
            snapshot = terminal_snapshot(
                root, run_id=run, execution_outcome="FAILED", implementation_pr=None,
                finalization_pr=None, repository_state="ACTIVE", workspace_state="ACTIVE",
                main_origin_sync="NO", worktree_state="DIRTY", active_blocker="UNKNOWN",
                recovery_required="YES", persist=True,
            )
        self.assertEqual(snapshot["run_qualification"], "NOT_QUALIFIED")
        self.assertEqual(snapshot["qualification_failure_reasons"], ["TERMINAL_EXECUTION_FAILED"])

    def test_mutating_delivery_requires_both_prs_and_authoritative_merges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._qualified(root)
            snapshot = terminal_snapshot(
                root, run_id="inbox-managed-proof", execution_outcome="COMPLETE",
                implementation_pr=None, finalization_pr=None,
                repository_state="MERGED_RECONCILED", workspace_state="WORKSPACE_READY",
                main_origin_sync="YES", worktree_state="CLEAN", active_blocker="NONE",
                recovery_required="NO", lineage_available=True,
            )
        self.assertEqual(snapshot["implementation_delivery"], "UNAVAILABLE")
        self.assertEqual(snapshot["finalization_delivery"], "UNAVAILABLE")
        self.assertEqual(snapshot["run_qualification"], "EVIDENCE_INSUFFICIENT")
        self.assertIn("IMPLEMENTATION_DELIVERY_UNPROVEN", snapshot["qualification_failure_reasons"])
        self.assertIn("FINALIZATION_DELIVERY_UNPROVEN", snapshot["qualification_failure_reasons"])

    def test_validation_only_qualifies_without_delivery_prs_when_required_controls_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = "validation-only-proof"
            record_run_qualification_context(root, run_id=run, submission_id="validation-only-submission", fresh_submission=True, retry_parent_run_id=None, resume_parent_run_id=None, recorded_at="2026-08-29T00:00:00+00:00")
            record_validation_profile(root, run_id=run, selected_validation_tier="DASHBOARD", validation_profile_version="1.0", required_validation_controls=("dashboard_browser",), recorded_at="2026-08-29T00:00:00+00:00")
            record_validation_control_result(root, run_id=run, validation_id="dashboard_browser", category="browser", control_identity="npm run test:engineering-dashboard", required_for_profile=True, execution_status="EXECUTED", result="PASS", evidence_ref="command_terminal", observed_at="2026-08-29T00:01:00+00:00", currentness=1)
            for action in ("VALIDATION_ONLY", "RECONCILIATION", "CLEANUP"):
                append_action(root, run_id=run, action=action, authority="AUTONOMOUS_EP_ACTION")
            snapshot = terminal_snapshot(root, run_id=run, execution_outcome="COMPLETE", implementation_pr=None, finalization_pr=None, repository_state="MERGED_RECONCILED", workspace_state="WORKSPACE_READY", main_origin_sync="YES", worktree_state="CLEAN", active_blocker="NONE", recovery_required="NO", lineage_available=True, action_intent="VALIDATION_ONLY")
        self.assertEqual(snapshot["run_qualification"], "QUALIFIED")
        self.assertEqual(snapshot["required_validation_state"], "PASS")
        self.assertIsNone(snapshot["implementation_pr"])
        self.assertIsNone(snapshot["finalization_pr"])

    def test_terminal_qualification_snapshot_persists_the_exact_control_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = "persisted-validation-proof"
            record_run_qualification_context(root, run_id=run, submission_id="persisted-submission", fresh_submission=True, retry_parent_run_id=None, resume_parent_run_id=None, recorded_at="2026-08-30T00:00:00+00:00")
            record_validation_profile(root, run_id=run, selected_validation_tier="GENERIC", validation_profile_version="1.0", required_validation_controls=("control",), recorded_at="2026-08-30T00:00:00+00:00")
            record_validation_control_result(root, run_id=run, validation_id="control", category="suite", control_identity="generic", required_for_profile=True, execution_status="EXECUTED", result="PASS", evidence_ref="terminal", observed_at="2026-08-30T00:01:00+00:00", currentness=1)
            for action in ("VALIDATION_ONLY", "CLEANUP"):
                append_action(root, run_id=run, action=action, authority="AUTONOMOUS_EP_ACTION")
            snapshot = terminal_snapshot(root, run_id=run, execution_outcome="COMPLETE", implementation_pr=None, finalization_pr=None, repository_state="MERGED_RECONCILED", workspace_state="WORKSPACE_READY", main_origin_sync="YES", worktree_state="CLEAN", active_blocker="NONE", recovery_required="NO", action_intent="VALIDATION_ONLY", persist=True)
        self.assertEqual(snapshot["required_validation_state"], "PASS")
        self.assertEqual(snapshot["cleanup_outcome"], "COMPLETED")
        self.assertEqual(snapshot["projection_conflicts"], [])
        self.assertEqual(snapshot["run_qualification"], "QUALIFIED")
        self.assertTrue(snapshot["required_control_snapshot_ref"].startswith("required-controls:sha256:"))

    def test_cleanup_failure_is_persisted_separately_from_the_terminal_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_action(root, run_id="cleanup-failure", action="CLEANUP", authority="AUTONOMOUS_EP_ACTION")
            snapshot = terminal_snapshot(root, run_id="cleanup-failure", execution_outcome="BLOCKED", implementation_pr=None, finalization_pr=None, repository_state="UNAVAILABLE", workspace_state="UNAVAILABLE", main_origin_sync="UNAVAILABLE", worktree_state="UNAVAILABLE", active_blocker="UNAVAILABLE", recovery_required="UNAVAILABLE", persist=True)
        self.assertEqual(snapshot["cleanup_outcome"], "FAILED")
        self.assertEqual(snapshot["run_qualification"], "NOT_QUALIFIED")

    def test_validation_only_is_not_promoted_when_required_evidence_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_run_qualification_context(root, run_id="missing-validation-only", submission_id="s", fresh_submission=True, retry_parent_run_id=None, resume_parent_run_id=None, recorded_at="2026-08-29T00:00:00+00:00")
            record_validation_profile(root, run_id="missing-validation-only", selected_validation_tier="DASHBOARD", validation_profile_version="1.0", required_validation_controls=("dashboard_browser",), recorded_at="2026-08-29T00:00:00+00:00")
            snapshot = terminal_snapshot(root, run_id="missing-validation-only", execution_outcome="COMPLETE", implementation_pr=None, finalization_pr=None, repository_state="MERGED_RECONCILED", workspace_state="WORKSPACE_READY", main_origin_sync="YES", worktree_state="CLEAN", active_blocker="NONE", recovery_required="NO", lineage_available=True, action_intent="VALIDATION_ONLY")
        self.assertNotEqual(snapshot["run_qualification"], "QUALIFIED")
        self.assertIn("REQUIRED_VALIDATION_UNRESOLVED", snapshot["qualification_failure_reasons"])

    def test_operator_merge_actions_remain_expected_gates_through_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._qualified(root)
            for action in ("IMPLEMENTATION_MERGE", "FINALIZATION_MERGE"):
                append_action(
                    root,
                    run_id="inbox-managed-proof",
                    action=action,
                    authority="EXPECTED_OPERATOR_GATE",
                    actor="operator",
                    evidence_ref="github_merge",
                )
            snapshot = terminal_snapshot(
                root,
                run_id="inbox-managed-proof",
                execution_outcome="COMPLETE",
                implementation_pr=101,
                finalization_pr=102,
                repository_state="MERGED_RECONCILED",
                workspace_state="WORKSPACE_READY",
                main_origin_sync="YES",
                worktree_state="CLEAN",
                active_blocker="NONE",
                recovery_required="NO",
                lineage_available=True,
            )
        self.assertEqual(
            [action["authority"] for action in snapshot["actions"][-2:]],
            ["EXPECTED_OPERATOR_GATE", "EXPECTED_OPERATOR_GATE"],
        )
        self.assertEqual(snapshot["unplanned_manual_intervention_count"], 0)
        self.assertEqual(snapshot["managed_autonomy_qualification"], "QUALIFIED")

    def test_manual_repair_is_disqualifying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self._qualified(root)
            append_action(
                root,
                run_id="inbox-managed-proof",
                action="MANUAL_CODE_REPAIR",
                authority="UNPLANNED_MANUAL_INTERVENTION",
                actor="operator",
                evidence_ref="commit",
            )
            snapshot = terminal_snapshot(
                root,
                run_id="inbox-managed-proof",
                execution_outcome="COMPLETE",
                implementation_pr=101,
                finalization_pr=102,
                repository_state="MERGED_RECONCILED",
                workspace_state="WORKSPACE_READY",
                main_origin_sync="YES",
                worktree_state="CLEAN",
                active_blocker="NONE",
                recovery_required="NO",
                lineage_available=True,
            )
        self.assertEqual(snapshot["managed_autonomy_qualification"], "NOT_QUALIFIED")
        self.assertIn("UNEXPECTED_MANUAL_INTERVENTION", snapshot["qualification_failure_reasons"])

    def test_failed_or_blocked_terminal_execution_can_never_run_qualify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._qualified(root)
            for terminal, reason in (("FAILED", "TERMINAL_EXECUTION_FAILED"), ("BLOCKED", "TERMINAL_EXECUTION_BLOCKED")):
                snapshot = terminal_snapshot(
                    root, run_id="inbox-managed-proof", execution_outcome=terminal,
                    implementation_pr=101, finalization_pr=102, repository_state="MERGED_RECONCILED",
                    workspace_state="WORKSPACE_READY", main_origin_sync="YES", worktree_state="CLEAN",
                    active_blocker="NONE", recovery_required="NO", lineage_available=True,
                )
                self.assertEqual(snapshot["run_qualification"], "NOT_QUALIFIED")
                self.assertEqual(snapshot["managed_autonomy_qualification"], "NOT_QUALIFIED")
                self.assertEqual(snapshot["qualification_failure_reasons"], [reason])

    def test_unknown_authority_and_legacy_evidence_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_action(
                root,
                run_id="inbox-legacy",
                action="IMPLEMENTATION_MERGE",
                authority="UNKNOWN_AUTHORITY",
            )
            snapshot = terminal_snapshot(
                root,
                run_id="inbox-legacy",
                execution_outcome="COMPLETE",
                implementation_pr=1,
                finalization_pr=2,
                repository_state="MERGED_RECONCILED",
                workspace_state="WORKSPACE_READY",
                main_origin_sync="YES",
                worktree_state="CLEAN",
                active_blocker="NONE",
                recovery_required="NO",
                lineage_available=True,
            )
        self.assertEqual(snapshot["managed_autonomy_qualification"], "EVIDENCE_INSUFFICIENT")
        self.assertEqual(snapshot["terminal_execution_state"], "COMPLETE")

    def test_newer_pass_supersedes_waiting_but_conflicting_current_evidence_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._qualified(root)
            append_validation_observation(
                root,
                run_id="inbox-managed-proof",
                control="github_check",
                state="WAITING",
                required=True,
                currentness=1,
            )
            append_validation_observation(
                root,
                run_id="inbox-managed-proof",
                control="github_check",
                state="PASS",
                required=True,
                currentness=2,
            )
            snapshot = terminal_snapshot(
                root,
                run_id="inbox-managed-proof",
                execution_outcome="COMPLETE",
                implementation_pr=101,
                finalization_pr=102,
                repository_state="MERGED_RECONCILED",
                workspace_state="WORKSPACE_READY",
                main_origin_sync="YES",
                worktree_state="CLEAN",
                active_blocker="NONE",
                recovery_required="NO",
                lineage_available=True,
            )
            self.assertEqual(snapshot["validation_current"]["github_check"], "PASS")
            append_validation_observation(
                root,
                run_id="inbox-managed-proof",
                control="github_check",
                state="FAIL",
                required=True,
                currentness=2,
            )
            conflict = terminal_snapshot(
                root,
                run_id="inbox-managed-proof",
                execution_outcome="COMPLETE",
                implementation_pr=101,
                finalization_pr=102,
                repository_state="MERGED_RECONCILED",
                workspace_state="WORKSPACE_READY",
                main_origin_sync="YES",
                worktree_state="CLEAN",
                active_blocker="NONE",
                recovery_required="NO",
            )
        self.assertEqual(conflict["managed_autonomy_qualification"], "EVIDENCE_INSUFFICIENT")
        self.assertIn("EVIDENCE_CONFLICT", conflict["qualification_failure_reasons"])

    def test_retry_parent_prevents_fresh_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._qualified(root, retry_parent="inbox-parent")
            snapshot = terminal_snapshot(
                root,
                run_id="inbox-managed-proof",
                execution_outcome="COMPLETE",
                implementation_pr=101,
                finalization_pr=102,
                repository_state="MERGED_RECONCILED",
                workspace_state="WORKSPACE_READY",
                main_origin_sync="YES",
                worktree_state="CLEAN",
                active_blocker="NONE",
                recovery_required="NO",
                lineage_available=True,
            )
        self.assertEqual(snapshot["fresh_submission"], "NO")
        self.assertIn("FRESH_SUBMISSION_UNPROVEN", snapshot["qualification_failure_reasons"])

    def test_resume_parent_prevents_fresh_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._qualified(root, resume_parent="inbox-parent")
            snapshot = terminal_snapshot(
                root,
                run_id="inbox-managed-proof",
                execution_outcome="COMPLETE",
                implementation_pr=101,
                finalization_pr=102,
                repository_state="MERGED_RECONCILED",
                workspace_state="WORKSPACE_READY",
                main_origin_sync="YES",
                worktree_state="CLEAN",
                active_blocker="NONE",
                recovery_required="NO",
                lineage_available=True,
            )
        self.assertEqual(snapshot["fresh_submission"], "NO")
        self.assertEqual(snapshot["resume_parent"], "inbox-parent")
        self.assertIn("FRESH_SUBMISSION_UNPROVEN", snapshot["qualification_failure_reasons"])

    def test_legacy_lineage_is_explicitly_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = terminal_snapshot(
                Path(directory), run_id="inbox-legacy-lineage", execution_outcome="COMPLETE",
                implementation_pr=None, finalization_pr=None, repository_state="UNAVAILABLE",
                workspace_state="UNAVAILABLE", main_origin_sync="UNAVAILABLE", worktree_state="UNAVAILABLE",
                active_blocker="UNAVAILABLE", recovery_required="UNAVAILABLE",
            )
        self.assertEqual(snapshot["fresh_submission"], "UNAVAILABLE")
        self.assertEqual(snapshot["retry_parent"], "UNAVAILABLE")
        self.assertEqual(snapshot["resume_parent"], "UNAVAILABLE")

    def test_original_lineage_cannot_be_overwritten_by_later_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_run_qualification_context(root, run_id="lineage-finalization", submission_id="submission-one",
                                             fresh_submission=True, retry_parent_run_id=None, resume_parent_run_id=None,
                                             recorded_at="2026-08-28T00:00:00+00:00")
            record_run_qualification_context(root, run_id="lineage-finalization", submission_id="submission-two",
                                             fresh_submission=False, retry_parent_run_id="old-run", resume_parent_run_id=None,
                                             recorded_at="2026-08-28T00:01:00+00:00")
            snapshot = terminal_snapshot(root, run_id="lineage-finalization", execution_outcome="COMPLETE",
                                         implementation_pr=None, finalization_pr=None, repository_state="UNAVAILABLE",
                                         workspace_state="UNAVAILABLE", main_origin_sync="UNAVAILABLE", worktree_state="UNAVAILABLE",
                                         active_blocker="UNAVAILABLE", recovery_required="UNAVAILABLE")
        self.assertEqual(snapshot["fresh_submission"], "YES")
        self.assertEqual(snapshot["submission_id"], "submission-one")

    def test_required_validation_profile_is_fail_closed_and_optional_not_executed_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_validation_profile(root, run_id="validation-contract", selected_validation_tier="FULL",
                                      validation_profile_version="1.0", required_validation_controls=("suite", "lint"),
                                      recorded_at="2026-08-28T00:00:00+00:00")
            record_validation_control_result(root, run_id="validation-contract", validation_id="suite", category="suite",
                                             control_identity="full suite", required_for_profile=True, execution_status="EXECUTED",
                                             result="PASS", evidence_ref="local", observed_at="2026-08-28T00:00:01+00:00", currentness=1)
            missing = terminal_snapshot(root, run_id="validation-contract", execution_outcome="COMPLETE",
                                       implementation_pr=None, finalization_pr=None, repository_state="UNAVAILABLE",
                                       workspace_state="UNAVAILABLE", main_origin_sync="UNAVAILABLE", worktree_state="UNAVAILABLE",
                                       active_blocker="UNAVAILABLE", recovery_required="UNAVAILABLE")
            self.assertEqual(missing["required_validation_state"], "UNRESOLVED")
            record_validation_control_result(root, run_id="validation-contract", validation_id="lint", category="lint",
                                             control_identity="lint", required_for_profile=True, execution_status="EXECUTED",
                                             result="FAIL", evidence_ref="local", observed_at="2026-08-28T00:00:02+00:00", currentness=1)
            failed = terminal_snapshot(root, run_id="validation-contract", execution_outcome="COMPLETE",
                                       implementation_pr=None, finalization_pr=None, repository_state="UNAVAILABLE",
                                       workspace_state="UNAVAILABLE", main_origin_sync="UNAVAILABLE", worktree_state="UNAVAILABLE",
                                       active_blocker="UNAVAILABLE", recovery_required="UNAVAILABLE")
            self.assertEqual(failed["required_validation_state"], "FAIL")

    def test_newer_required_check_pass_supersedes_historical_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_pr_check_observation(root, run_id="inbox-checks", pr_number=101,
                pr_role="IMPLEMENTATION", pr_state="OPEN", merge_commit=None,
                required_checks_state="WAITING", evidence_ref="github", currentness=0)
            append_pr_check_observation(root, run_id="inbox-checks", pr_number=101,
                pr_role="IMPLEMENTATION", pr_state="MERGED", merge_commit="b" * 40,
                required_checks_state="PASS", evidence_ref="github", currentness=0)
            snapshot = terminal_snapshot(root, run_id="inbox-checks", execution_outcome="COMPLETE",
                implementation_pr=101, finalization_pr=None, repository_state="UNAVAILABLE",
                workspace_state="UNAVAILABLE", main_origin_sync="UNAVAILABLE", worktree_state="UNAVAILABLE",
                active_blocker="UNAVAILABLE", recovery_required="UNAVAILABLE")
        self.assertEqual(snapshot["pr_checks"]["IMPLEMENTATION"]["required_checks_state"], "PASS")
        self.assertEqual(snapshot["pr_checks"]["IMPLEMENTATION"]["historical_observation_count"], 1)

    def test_merged_pr_without_check_evidence_is_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = terminal_snapshot(Path(directory), run_id="inbox-no-checks", execution_outcome="COMPLETE",
                implementation_pr=101, finalization_pr=None, repository_state="UNAVAILABLE",
                workspace_state="UNAVAILABLE", main_origin_sync="UNAVAILABLE", worktree_state="UNAVAILABLE",
                active_blocker="UNAVAILABLE", recovery_required="UNAVAILABLE")
        self.assertNotEqual(snapshot["pr_checks"].get("IMPLEMENTATION", {}).get("required_checks_state"), "PASS")
