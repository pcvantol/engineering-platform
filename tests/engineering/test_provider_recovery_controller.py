from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest

from tools.engineering.agent_state import StateStore, TransactionState
from tools.engineering.provider_recovery import (
    claim_replacement_launch,
    create_recovery_available,
    load_recovery_state,
    record_provider_started,
    record_pre_execution_launch_failure,
    record_replacement_terminal,
    reconcile_recovery,
    transition_recovery_state,
    consume_controlled_interruption_hook,
    watcher_resume_action,
)


class ProviderRecoveryControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_id = "provider-recovery-controller"
        StateStore(self.root / ".engineering" / "engineering-runs").save(
            TransactionState(self.run_id, "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT", branch="topic")
        )
        self.recovery = create_recovery_available(
            self.root, run_id=self.run_id, triggering_invocation_id="attempt-one",
            lifecycle_phase="EXECUTE_AGENT", branch="topic", worktree_identity="worktree:test", lease_id="lease-test",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_claim_start_terminal_lineage_is_exactly_once(self) -> None:
        self.assertTrue(transition_recovery_state(
            self.root, run_id=self.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
        ))
        claim = claim_replacement_launch(self.root, run_id=self.run_id)
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertIsNone(claim_replacement_launch(self.root, run_id=self.run_id))
        self.assertTrue(record_provider_started(
            self.root, run_id=self.run_id, receipt_id=str(claim["receipt_id"]), pid=os.getpid(), process_group=os.getpgrp(),
        ))
        self.assertTrue(record_replacement_terminal(
            self.root, run_id=self.run_id, outcome="SUCCESS", result_evidence_ref="artifact:agent-result",
        ))
        state = load_recovery_state(self.root, self.run_id)
        assert state is not None
        self.assertEqual(state["state"], "RECOVERED")
        self.assertEqual(state["replacement_invocation_id"], self.recovery["replacement_invocation_id"])
        self.assertFalse(record_replacement_terminal(self.root, run_id=self.run_id, outcome="SUCCESS"))

    def test_starting_without_a_claim_allows_only_the_persisted_intent(self) -> None:
        self.assertTrue(transition_recovery_state(
            self.root, run_id=self.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
        ))
        self.assertEqual(reconcile_recovery(self.root, run_id=self.run_id), "LAUNCH_UNCLAIMED")
        claim = claim_replacement_launch(self.root, run_id=self.run_id)
        assert claim is not None
        self.assertEqual(claim["invocation_id"], self.recovery["replacement_invocation_id"])
        self.assertEqual(reconcile_recovery(self.root, run_id=self.run_id, verifier=lambda _: "NOT_ACTIVE"), "AMBIGUOUS")
        state = load_recovery_state(self.root, self.run_id)
        assert state is not None
        self.assertEqual(state["state"], "AMBIGUOUS")

    def test_process_started_without_a_terminal_receipt_fails_closed(self) -> None:
        self.assertTrue(transition_recovery_state(
            self.root, run_id=self.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
        ))
        claim = claim_replacement_launch(self.root, run_id=self.run_id)
        assert claim is not None
        self.assertTrue(record_provider_started(
            self.root, run_id=self.run_id, receipt_id=str(claim["receipt_id"]), pid=os.getpid(), process_group=os.getpgrp(),
        ))
        self.assertEqual(reconcile_recovery(self.root, run_id=self.run_id, verifier=lambda _: "NOT_ACTIVE"), "AMBIGUOUS")
        state = load_recovery_state(self.root, self.run_id)
        assert state is not None
        self.assertEqual(state["state"], "AMBIGUOUS")

    def test_matching_session_bound_process_is_observed_without_relaunch(self) -> None:
        self.assertTrue(transition_recovery_state(
            self.root, run_id=self.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
        ))
        claim = claim_replacement_launch(self.root, run_id=self.run_id)
        assert claim is not None
        self.assertTrue(record_provider_started(
            self.root, run_id=self.run_id, receipt_id=str(claim["receipt_id"]), pid=os.getpid(), process_group=os.getpgrp(),
        ))
        self.assertEqual(reconcile_recovery(self.root, run_id=self.run_id, verifier=lambda _: "MATCH"), "SAME_PROVIDER_STILL_ACTIVE")
        state = load_recovery_state(self.root, self.run_id)
        assert state is not None
        self.assertEqual(state["state"], "RECOVERY_IN_PROGRESS")
        self.assertEqual(state["provider_session_id"], claim["provider_session_id"])
        self.assertIsNone(watcher_resume_action(self.root, self.run_id))

    def test_session_or_process_identity_mismatch_fails_closed(self) -> None:
        self.assertTrue(transition_recovery_state(
            self.root, run_id=self.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
        ))
        claim = claim_replacement_launch(self.root, run_id=self.run_id)
        assert claim is not None
        self.assertTrue(record_provider_started(
            self.root, run_id=self.run_id, receipt_id=str(claim["receipt_id"]), pid=os.getpid(), process_group=os.getpgrp(),
        ))
        self.assertEqual(reconcile_recovery(self.root, run_id=self.run_id, verifier=lambda _: "MISMATCH"), "AMBIGUOUS")

    def test_claimed_pre_execution_failure_reuses_only_the_same_intent(self) -> None:
        self.assertTrue(transition_recovery_state(
            self.root, run_id=self.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
        ))
        claim = claim_replacement_launch(self.root, run_id=self.run_id)
        assert claim is not None
        self.assertTrue(record_pre_execution_launch_failure(
            self.root, run_id=self.run_id, diagnostic_code="spawn_refused",
        ))
        self.assertEqual(reconcile_recovery(self.root, run_id=self.run_id), "LAUNCH_CLAIMED_PREEXEC_FAILURE")
        state = load_recovery_state(self.root, self.run_id)
        assert state is not None
        self.assertEqual(state["replacement_invocation_id"], claim["invocation_id"])

    def test_watcher_does_not_resume_when_a_live_run_lease_exists(self) -> None:
        from tools.engineering.execution_lease import acquire

        lease = acquire(self.root, self.run_id, identity="test", instance_id="live-host")
        try:
            self.assertIsNone(watcher_resume_action(self.root, self.run_id))
        finally:
            from tools.engineering.execution_lease import release
            release(self.root, lease)

    def test_watcher_does_not_consume_recovered_result_after_phase_advanced(self) -> None:
        self.assertTrue(transition_recovery_state(
            self.root, run_id=self.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERED",
            result="SUCCESS", result_evidence_ref="artifact:already-consumed",
        ))
        StateStore(self.root / ".engineering" / "engineering-runs").save(
            TransactionState(self.run_id, "pcvantol/djconnect", "prompt.md", "WAIT_FOR_TERMINAL_EVIDENCE", branch="topic")
        )
        self.assertIsNone(watcher_resume_action(self.root, self.run_id))

    def test_controlled_hook_is_consumed_before_the_synthetic_interruption(self) -> None:
        hook_run = "provider-recovery-hook"
        StateStore(self.root / ".engineering" / "engineering-runs").save(
            TransactionState(hook_run, "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT", branch="topic")
        )
        prior = os.environ.get("DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE")
        os.environ["DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE"] = f"{hook_run}:EXECUTE_AGENT"
        try:
            self.assertTrue(consume_controlled_interruption_hook(
                self.root, run_id=hook_run, phase="EXECUTE_AGENT",
            ))
            # A simulated host restart retains only the durable marker, not
            # in-memory controller state. It still cannot fire a second time.
            self.assertFalse(consume_controlled_interruption_hook(
                self.root, run_id=hook_run, phase="EXECUTE_AGENT",
            ))
            self.assertFalse(consume_controlled_interruption_hook(
                self.root, run_id="unrelated-run", phase="EXECUTE_AGENT",
            ))
        finally:
            if prior is None:
                os.environ.pop("DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE", None)
            else:
                os.environ["DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE"] = prior
