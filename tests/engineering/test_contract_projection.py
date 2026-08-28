"""Regression coverage for the public, read-only EP contract boundary."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from tools.engineering.agent_state import StateStore, TransactionState
from tools.engineering.contracts import AllowedAction, evaluate_action, get_allowed_actions, get_run_context
from tools.engineering.contracts.models import ContractVersionError, require_compatible_version
from tools.engineering.managed_autonomy import append_pr_check_observation
from tools.engineering.storage import (
    open_storage,
    record_run_qualification_context,
    record_submission,
    record_validation_control_result,
    record_validation_profile,
)


class ContractProjectionTests(unittest.TestCase):
    def _state(self, root: Path, run_id: str = "inbox-contract") -> TransactionState:
        state = TransactionState(run_id=run_id, repository="pcvantol/djconnect", prompt_path="private", phase="EXECUTE_AGENT", branch="contract", last_verified_sha="a" * 40)
        StateStore(root / ".engineering" / "engineering-runs").save(state)
        return state

    def test_projection_is_serializable_read_only_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self._state(root)
            before = (root / ".engineering" / "engineering.db").read_bytes()
            payload = get_run_context(root, state.run_id)
            after = (root / ".engineering" / "engineering.db").read_bytes()
            self.assertEqual(before, after)
            self.assertEqual(payload["contract_name"], "run_context")
            self.assertEqual(payload["objective"]["objective_summary"], "UNAVAILABLE")
            rendered = json.dumps(payload)
            self.assertNotIn("private", rendered)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("command", rendered.casefold())
            self.assertTrue(all(item["classification"] == "READ_ONLY" for item in payload["allowed_actions"]))
            self.assertNotIn("merge", {item["action_id"] for item in payload["allowed_actions"]})

    def test_raw_submission_content_and_other_run_evidence_never_cross_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self._state(root)
            self._state(root, "inbox-unrelated")
            record_submission(root, submission_id="submission-contract", producer_id="producer", producer_type="HUMAN",
                              prompt_content="authorization: Bearer secret-value /absolute/private/path", prompt_metadata={},
                              target_identity={}, original_envelope={}, received_at="2026-08-24T00:00:00+00:00", link_run_id=state.run_id)
            append_pr_check_observation(root, run_id="inbox-unrelated", pr_number=99, pr_role="IMPLEMENTATION", pr_state="OPEN", merge_commit=None, required_checks_state="PASS", evidence_ref="unrelated")
            payload = json.dumps(get_run_context(root, state.run_id))
            self.assertNotIn("secret-value", payload)
            self.assertNotIn("absolute/private", payload)
            self.assertNotIn('"implementation_pr": 99', payload)

    def test_unsafe_objective_metadata_is_omitted_instead_of_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self._state(root)
            record_submission(root, submission_id="submission-objective", producer_id="producer", producer_type="HUMAN",
                              prompt_content="bounded", prompt_metadata={
                                  "objective_summary": "Inspect current evidence",
                                  "scope_summary": "Review /private/workspace only",
                                  "constraints": ["Never expose authorization: Bearer secret-value"],
                              }, target_identity={}, original_envelope={}, received_at="2026-08-24T00:00:00+00:00", link_run_id=state.run_id)
            objective = get_run_context(root, state.run_id)["objective"]
            self.assertEqual(objective["objective_summary"], "Inspect current evidence")
            self.assertEqual(objective["scope_summary"], "UNAVAILABLE")
            self.assertEqual(objective["constraints"], "UNAVAILABLE")

    def test_operator_gate_and_failed_required_check_are_projected_without_auto_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self._state(root)
            StateStore(root / ".engineering" / "engineering-runs").save(replace(state, phase="WAIT_FOR_OPERATOR_MERGE", implementation_pull_request=42))
            append_pr_check_observation(root, run_id=state.run_id, pr_number=42, pr_role="IMPLEMENTATION", pr_state="OPEN", merge_commit=None, required_checks_state="FAIL", evidence_ref="check-42")
            payload = get_run_context(root, state.run_id)
            self.assertEqual(payload["delivery"]["implementation_required_checks_state"], "FAIL")
            self.assertEqual(payload["delivery"]["implementation_merge_gate"], "EXPECTED_OPERATOR_GATE")
            self.assertFalse(any("merge" in item["action_id"] for item in payload["allowed_actions"]))

    def test_projection_uses_explicit_lineage_and_required_validation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self._state(root)
            record_run_qualification_context(
                root, run_id=state.run_id, submission_id="submission-contract", fresh_submission=True,
                retry_parent_run_id=None, resume_parent_run_id=None, recorded_at="2026-08-28T00:00:00+00:00",
            )
            record_validation_profile(
                root, run_id=state.run_id, selected_validation_tier="DOCUMENTATION", validation_profile_version="1.0",
                required_validation_controls=("git_diff_check",), recorded_at="2026-08-28T00:00:00+00:00",
            )
            record_validation_control_result(
                root, run_id=state.run_id, validation_id="git_diff_check", category="repository",
                control_identity="git diff --check", required_for_profile=True, execution_status="EXECUTED",
                result="PASS", evidence_ref="local", observed_at="2026-08-28T00:00:01+00:00", currentness=1,
            )
            payload = get_run_context(root, state.run_id)
        self.assertTrue(payload["run"]["fresh_submission"])
        self.assertIsNone(payload["run"]["retry_parent"])
        self.assertIsNone(payload["run"]["resume_parent"])
        required = payload["validation"]["required_validation"]
        self.assertEqual(required["required_validation_state"], "PASS")
        self.assertEqual(required["required_validation_controls"], ["git_diff_check"])

    def test_unknown_and_stale_actions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self._state(root)
            current = get_allowed_actions(root, state.run_id)[0]
            stale = {**current, "evidence_version": "snapshot:old"}
            self.assertEqual(evaluate_action(root, state.run_id, stale)["decision"], "STALE_REVALIDATION_REQUIRED")
            unknown = AllowedAction(action_id="delivery.recovery.auto_merge", action_namespace="delivery.recovery.*", run_id=state.run_id, evidence_version=get_run_context(root, state.run_id)["evidence_version"], classification="MUTATING_RECOVERY")
            self.assertEqual(evaluate_action(root, state.run_id, unknown)["decision"], "DENIED")
            incompatible = {**current, "contract_version": "2.0"}
            decision = evaluate_action(root, state.run_id, incompatible)
            self.assertEqual(decision["decision"], "UNAVAILABLE")
            self.assertEqual(decision["reason_code"], "INCOMPATIBLE_CONTRACT_VERSION")

    def test_named_lifecycle_fixtures_stay_read_only(self) -> None:
        fixtures = {
            "RUNNING": "EXECUTE_AGENT", "WAITING_IMPLEMENTATION": "WAIT_FOR_OPERATOR_MERGE",
            "CHECK_FAILED": "REPAIR_AGENT", "WAITING_FINALIZATION": "WAIT_FOR_FINALIZATION_MERGE",
            "RECONCILIATION": "RECONCILE_AGENT", "COMPLETE": "COMPLETE", "DIRTY_REPOSITORY": "BLOCKED",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, phase in fixtures.items():
                run_id = f"inbox-{name.lower().replace('_', '-')}"
                state = self._state(root, run_id)
                StateStore(root / ".engineering" / "engineering-runs").save(replace(state, phase=phase, terminal=phase in {"COMPLETE", "BLOCKED"}))
                payload = get_run_context(root, run_id)
                self.assertEqual(payload["run"]["current_execution_state"], phase)
                self.assertTrue(all(item["classification"] == "READ_ONLY" for item in payload["allowed_actions"]))

    def test_workspace_owner_is_bounded_and_never_enables_recovery_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self._state(root)
            self._state(root, "inbox-other-owner")
            connection = open_storage(root)
            try:
                connection.execute("INSERT INTO execution_run_leases(lease_id,run_id,host_identity,host_instance_id,process_id,acquired_at,last_heartbeat_at,expires_at,lease_state,lease_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("lease-other", "inbox-other-owner", "host-a", "instance", 1, "t", "t", "t", "ACTIVE", 1, "t", "t"))
            finally:
                connection.close()
            payload = get_run_context(root, state.run_id)
            self.assertTrue(payload["workspace"]["workspace_occupied"])
            self.assertEqual(payload["workspace"]["active_owner_run_id"], "inbox-other-owner")
            self.assertFalse(any(item["classification"] != "READ_ONLY" for item in payload["allowed_actions"]))

    def test_missing_run_and_version_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = get_run_context(Path(temporary), "legacy-missing")
            self.assertEqual(payload["run"]["current_execution_state"], "UNAVAILABLE")
            self.assertTrue(all(item["allowed"] is False for item in payload["allowed_actions"]))
            with self.assertRaises(ContractVersionError):
                require_compatible_version("2.0")
