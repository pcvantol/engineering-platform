"""Focused checks for the read-only execution lifecycle projection."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tools.engineering.agent_state import StateStore, TransactionState
from tools.engineering.execution_timing import complete_phase, start_phase
from tools.engineering.execution_lifecycle import intended_path, projection


class ExecutionLifecycleProjectionTests(unittest.TestCase):
    def _state(self, root: Path, phase: str, **values: object) -> None:
        StateStore(root / ".engineering" / "engineering-runs").save(TransactionState(
            run_id="inbox-flow", repository="pcvantol/djconnect", prompt_path="prompt.md",
            phase=phase, terminal=phase in {"COMPLETE", "BLOCKED", "FAILED"}, **values,
        ))

    def test_managed_projects_start_completed_active_and_pending_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "INITIALIZE")
            self._state(root, "EXECUTE_AGENT")
            value = projection(root, "inbox-flow")
        self.assertEqual(value["run_id"], "inbox-flow")
        self.assertTrue(value["available"])
        self.assertEqual(value["steps"][0]["state"], "START")
        self.assertEqual(value["steps"][1]["state"], "COMPLETED")
        self.assertEqual(value["steps"][2]["state"], "ACTIVE")
        self.assertEqual(value["steps"][-1]["state"], "PENDING")

    def test_terminal_outcome_keeps_later_steps_pending_and_repairs_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "INITIALIZE")
            self._state(root, "REPAIR_AGENT", repair_iterations=2)
            self._state(root, "BLOCKED", repair_iterations=2)
            value = projection(root, "inbox-flow")
        by_id = {step["id"]: step for step in value["steps"]}
        self.assertEqual(by_id["REPAIR_AGENT"]["iteration_count"], 2)
        self.assertEqual(by_id["WAIT_FOR_OPERATOR_MERGE"]["state"], "PENDING")
        self.assertEqual(by_id["TERMINAL"]["state"], "BLOCKED")

    def test_genesis_has_its_own_canonical_path(self) -> None:
        self.assertNotIn("WAIT_FOR_OPERATOR_MERGE", intended_path("GENESIS"))
        self.assertIn("WAIT_FOR_OPERATOR_MERGE", intended_path("MANAGED"))

    def test_status_reconciliation_uses_its_own_merge_path(self) -> None:
        path = intended_path("MANAGED", "RECONCILIATION", None)
        self.assertEqual(
            path,
            ("START", "INITIALIZE", "RECONCILE_AGENT", "REPOSITORY_CLEANUP", "TERMINAL"),
        )
        self.assertNotIn("EXECUTE_AGENT", path)
        self.assertNotIn("WAIT_FOR_OPERATOR_MERGE", path)

    def test_reconciliation_projects_its_automatic_agent_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "RECONCILE_AGENT", transaction_kind="RECONCILIATION")
            value = projection(root, "inbox-flow")
        by_id = {step["id"]: step for step in value["steps"]}
        self.assertEqual(value["current_step"], "RECONCILE_AGENT")
        self.assertEqual(by_id["RECONCILE_AGENT"]["state"], "ACTIVE")
        self.assertNotIn("WAIT_FOR_RECONCILIATION_MERGE", by_id)

    def test_required_check_polling_stays_on_the_visible_merge_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "EXECUTE_AGENT")
            self._state(root, "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=840)
            value = projection(root, "inbox-flow")
        by_id = {step["id"]: step for step in value["steps"]}
        self.assertEqual(value["current_step"], "WAIT_FOR_OPERATOR_MERGE")
        self.assertEqual(by_id["WAIT_FOR_OPERATOR_MERGE"]["state"], "ACTIVE")

    def test_finalization_check_polling_stays_on_the_visible_finalization_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "WAIT_FOR_TERMINAL_EVIDENCE", transaction_kind="FINALIZATION", implementation_pull_request=840, pull_request=841)
            value = projection(root, "inbox-flow")
        by_id = {step["id"]: step for step in value["steps"]}
        self.assertEqual(value["current_step"], "FINALIZE_AGENT")
        self.assertEqual(by_id["FINALIZE_AGENT"]["state"], "ACTIVE")

    def test_failed_pr_checks_block_merge_and_identify_the_required_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=840)
            self._state(root, "REPAIR_AGENT", pull_request=840,
                        next_action="repair_bounded_validation_failure")
            value = projection(root, "inbox-flow")
        by_id = {step["id"]: step for step in value["steps"]}
        self.assertEqual(value["current_step"], "REPAIR_AGENT")
        self.assertEqual(by_id["WAIT_FOR_OPERATOR_MERGE"]["state"], "BLOCKED")
        self.assertEqual(
            by_id["WAIT_FOR_OPERATOR_MERGE"]["action_key"],
            "state.repair_bounded_validation_failure",
        )

    def test_merge_is_completed_only_after_finalization_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=840)
            self._state(root, "FINALIZE_AGENT", transaction_kind="FINALIZATION", implementation_pull_request=840, pull_request=840)
            value = projection(root, "inbox-flow")
        by_id = {step["id"]: step for step in value["steps"]}
        self.assertEqual(by_id["WAIT_FOR_OPERATOR_MERGE"]["state"], "COMPLETED")

    def test_finalization_pull_request_has_its_own_visible_merge_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=840)
            self._state(root, "FINALIZE_AGENT", transaction_kind="FINALIZATION", implementation_pull_request=840)
            self._state(
                root, "WAIT_FOR_OPERATOR_MERGE", transaction_kind="FINALIZATION", implementation_pull_request=840,
                pull_request=841, finalization_pull_request=841,
            )
            value = projection(root, "inbox-flow")
        by_id = {step["id"]: step for step in value["steps"]}
        self.assertEqual(value["current_step"], "WAIT_FOR_FINALIZATION_MERGE")
        self.assertEqual(by_id["WAIT_FOR_OPERATOR_MERGE"]["state"], "COMPLETED")
        self.assertEqual(by_id["FINALIZE_AGENT"]["state"], "COMPLETED")
        self.assertEqual(by_id["WAIT_FOR_FINALIZATION_MERGE"]["state"], "ACTIVE")

    def test_completed_managed_run_omits_merge_steps_without_pull_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "EXECUTE_AGENT")
            self._state(root, "COMPLETE")
            value = projection(root, "inbox-flow")
        step_ids = {step["id"] for step in value["steps"]}
        self.assertNotIn("WAIT_FOR_OPERATOR_MERGE", step_ids)
        self.assertNotIn("WAIT_FOR_FINALIZATION_MERGE", step_ids)

    def test_preflight_status_drift_block_never_invents_a_merge_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "INITIALIZE")
            # The inconsistent rolling state itself can contain an old
            # merge-wait event without any persisted PR evidence.
            self._state(root, "WAIT_FOR_OPERATOR_MERGE")
            self._state(
                root, "BLOCKED", terminal_condition="external_blocked",
                diagnostic="Pre-flight is NO-GO: rolling status records still describe Finalization as in progress.",
            )
            value = projection(root, "inbox-flow")
        step_ids = {step["id"] for step in value["steps"]}
        self.assertNotIn("WAIT_FOR_OPERATOR_MERGE", step_ids)
        self.assertNotIn("WAIT_FOR_FINALIZATION_MERGE", step_ids)
        self.assertEqual(value["recovery"], {"kind": "status_reconciliation", "run_id": "inbox-flow"})
        self.assertEqual(value["recovery"], {"kind": "status_reconciliation", "run_id": "inbox-flow"})

    def test_triggering_stale_rolling_record_shape_projects_only_finalization_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "INITIALIZE")
            self._state(
                root, "BLOCKED", terminal_condition="external_blocked",
                diagnostic=(
                    "Current main is clean, but the rolling records still state that "
                    "Finalization is pending despite its merged finalization PR."
                ),
            )
            value = projection(root, "inbox-flow")
        step_ids = {step["id"] for step in value["steps"]}
        self.assertNotIn("WAIT_FOR_OPERATOR_MERGE", step_ids)
        self.assertNotIn("WAIT_FOR_FINALIZATION_MERGE", step_ids)
        self.assertEqual(value["recovery"], {"kind": "status_reconciliation", "run_id": "inbox-flow"})

    def test_completed_managed_run_omits_only_unused_finalization_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "WAIT_FOR_TERMINAL_EVIDENCE", pull_request=840)
            self._state(root, "FINALIZE_AGENT", implementation_pull_request=840)
            self._state(root, "REPOSITORY_CLEANUP", implementation_pull_request=840)
            self._state(root, "COMPLETE", implementation_pull_request=840)
            value = projection(root, "inbox-flow")
        step_ids = {step["id"] for step in value["steps"]}
        self.assertIn("WAIT_FOR_OPERATOR_MERGE", step_ids)
        self.assertNotIn("WAIT_FOR_FINALIZATION_MERGE", step_ids)

    def test_projection_exposes_only_persisted_step_phase_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._state(root, "EXECUTE_AGENT")
            started = datetime(2026, 8, 16, 14, 0, tzinfo=timezone.utc)
            phase = start_phase(
                root, "inbox-flow", "PROVIDER_EXECUTION", started_at=started, monotonic_clock=10.0,
            )
            complete_phase(
                root, phase, completed_at=started + timedelta(seconds=12), monotonic_clock=22.0,
            )
            value = projection(root, "inbox-flow")
        by_id = {step["id"]: step for step in value["steps"]}
        timing = by_id["EXECUTE_AGENT"]["timing"]
        self.assertEqual(timing["started_at"], "2026-08-16T14:00:00+00:00")
        self.assertEqual(timing["finished_at"], "2026-08-16T14:00:12+00:00")
        self.assertEqual(timing["spans"], [{
            "phase": "PROVIDER_EXECUTION", "attempt": 1,
            "started_at": "2026-08-16T14:00:00+00:00",
            "finished_at": "2026-08-16T14:00:12+00:00",
            "duration_ms": 12000, "outcome": "COMPLETE",
        }])

    def test_missing_run_never_infers_historical_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(projection(Path(temporary), "inbox-missing"), {
                "run_id": "inbox-missing", "available": False, "steps": []
            })
