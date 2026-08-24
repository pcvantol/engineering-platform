from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.engineering.managed_autonomy import (
    append_action,
    append_validation_observation,
    record_gate,
    terminal_snapshot,
)


class ManagedAutonomyEvidenceTest(unittest.TestCase):
    def _qualified(self, root: Path) -> dict[str, object]:
        run = "inbox-managed-proof"
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
        )

    def test_expected_operator_gates_are_not_manual_interventions_and_qualify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._qualified(Path(directory))
        self.assertEqual(snapshot["managed_autonomy_qualification"], "QUALIFIED")
        self.assertEqual(snapshot["unplanned_manual_intervention_count"], 0)
        self.assertEqual(snapshot["expected_operator_gate_count"], 2)

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
            )
        self.assertEqual(snapshot["managed_autonomy_qualification"], "NOT_QUALIFIED")
        self.assertIn("UNEXPECTED_MANUAL_INTERVENTION", snapshot["qualification_failure_reasons"])

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
            self._qualified(root)
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
                retry_parent="inbox-parent",
            )
        self.assertEqual(snapshot["fresh_submission"], "NO")
        self.assertIn("FRESH_SUBMISSION_UNPROVEN", snapshot["qualification_failure_reasons"])
