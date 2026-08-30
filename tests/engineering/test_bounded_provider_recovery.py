from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest

from tools.engineering.agent_state import StateStore, TransactionState
from tools.engineering.execution_errors import CodexInvocationError
from tools.engineering.execution_host import EngineeringRunner
from tools.engineering.execution_lease import acquire
from tools.engineering.execution_models import AgentResult, RepositoryEvidence
from tools.engineering.provider_recovery import (
    claim_replacement_launch, create_recovery_available, persist_recovery_agent_result,
    record_provider_started, record_replacement_terminal, transition_recovery_state,
)
from tools.engineering.storage import load_submission_for_run, open_storage, record_submission


class _Repository:
    def inspect(self, _: Path) -> RepositoryEvidence:
        return RepositoryEvidence("pcvantol/djconnect", "topic", "a" * 40, True)


class _Agent:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.last_usage: dict[str, object] = {}
        self.last_usage_snapshots: tuple[dict[str, int], ...] = ()
        self.last_churn: dict[str, object] = {}
        self.last_runtime_metadata: dict[str, object] = {}
        self.last_context_escalations: tuple[dict[str, object], ...] = ()
        self.last_execution_seconds = 0.01
        self._process_callback = None

    def set_process_callback(self, callback) -> None:
        self._process_callback = callback

    def invoke(self, _: Path, __: str) -> AgentResult:
        if self._process_callback is not None:
            self._process_callback({"pid": os.getpid(), "process_group": os.getpgrp()})
        outcome = self.outcomes.pop(0)
        if self._process_callback is not None:
            self._process_callback(None)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, AgentResult)
        return outcome


class _Runner(EngineeringRunner):
    def _require_provider_dispatch_admission(self, _: TransactionState) -> None:
        return None

    def _require_agent_readiness(self, _: TransactionState) -> None:
        return None


def _interrupted() -> CodexInvocationError:
    return CodexInvocationError(
        "Provider turn interrupted before returning the required structured AgentResult.",
        "safe", next_action="NONE", terminal_condition="provider_turn_interrupted",
        interruption_reason="interrupted",
    )


class BoundedProviderRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("objective", encoding="utf-8")
        self.store = StateStore(self.root / ".engineering" / "engineering-runs")
        self.state = TransactionState("bounded-recovery", "pcvantol/djconnect", str(self.prompt), "EXECUTE_AGENT", branch="topic")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _runner(self, outcomes: list[object]) -> _Runner:
        runner = _Runner(self.root, self.store, _Repository(), object(), _Agent(outcomes), lambda _: None)
        self.store.save(self.state)
        runner.active_lease = acquire(self.root, self.state.run_id, identity="test", instance_id="host")
        return runner

    def test_one_interruption_reuses_the_same_run_and_records_two_invocations(self) -> None:
        runner = self._runner([_interrupted(), AgentResult("COMPLETE", branch="topic")])
        result = runner._invoke_agent_with_timing(self.state, "objective")
        self.assertEqual(result.terminal_state, "COMPLETE")
        saved = self.store.load(self.state.run_id)
        self.assertEqual(len(saved.provider_recovery_attempts), 1)
        record = saved.provider_recovery_attempts[0]
        self.assertEqual(record["result"], "RECOVERED")
        self.assertNotEqual(record["original_invocation_id"], record["replacement_invocation_id"])
        with open_storage(self.root) as connection:
            rows = connection.execute(
                "SELECT run_id,ordinal,usage_authority,input_tokens,output_tokens FROM provider_invocations ORDER BY ordinal"
            ).fetchall()
        self.assertEqual(
            rows,
            [(self.state.run_id, 1, "UNAVAILABLE", None, None), (self.state.run_id, 2, "UNAVAILABLE", None, None)],
        )

    def test_provider_backed_lifecycle_phases_keep_one_same_run_recovery_lineage(self) -> None:
        for phase in ("QUALITY_CONTROL_AGENT", "REPAIR_AGENT", "FINALIZE_AGENT"):
            with self.subTest(phase=phase), TemporaryDirectory() as temporary:
                root = Path(temporary)
                prompt = root / "prompt.md"
                prompt.write_text("objective", encoding="utf-8")
                state = TransactionState(
                    f"bounded-{phase.casefold().replace('_', '-')}", "pcvantol/djconnect", str(prompt), phase,
                    branch="topic",
                )
                store = StateStore(root / ".engineering" / "engineering-runs")
                runner = _Runner(root, store, _Repository(), object(), _Agent([_interrupted(), AgentResult("COMPLETE", branch="topic")]), lambda _: None)
                store.save(state)
                runner.active_lease = acquire(root, state.run_id, identity="test", instance_id="host")
                result = runner._invoke_agent_with_timing(
                    state, "objective", repair=phase == "REPAIR_AGENT", quality=phase == "QUALITY_CONTROL_AGENT",
                )
                self.assertEqual(result.terminal_state, "COMPLETE")
                recovery = store.load(state.run_id).provider_recovery_attempts[0]
                self.assertEqual(recovery["phase"], phase)
                self.assertEqual(recovery["result"], "RECOVERED")
                with open_storage(root) as connection:
                    invocations = connection.execute(
                        "SELECT run_id,phase FROM provider_invocations WHERE run_id=? ORDER BY ordinal", (state.run_id,)
                    ).fetchall()
                self.assertEqual(len(invocations), 2)
                self.assertEqual({row[0] for row in invocations}, {state.run_id})

    def test_recovery_preserves_structured_producer_provenance_without_prompt_parsing(self) -> None:
        runner = self._runner([_interrupted(), AgentResult("COMPLETE", branch="topic")])
        context = {
            "context_version": "1.0", "action_intent": "MUTATING_DELIVERY",
            "execution_context": "managed", "ingress": "icloud_text",
        }
        record_submission(
            self.root, submission_id="submission-human-recovery", producer_id="human:icloud-operator",
            producer_type="HUMAN", producer_version="1.0", contract_version="1.0",
            prompt_content="structured prompt", prompt_metadata={}, target_identity={}, original_envelope={},
            received_at="2026-08-30T00:00:00+00:00", link_run_id=self.state.run_id,
            execution_context=context,
        )
        before = load_submission_for_run(self.root, self.state.run_id)
        self.assertIsNotNone(before)
        result = runner._invoke_agent_with_timing(self.state, "objective")
        self.assertEqual(result.terminal_state, "COMPLETE")
        after = load_submission_for_run(self.root, self.state.run_id)
        self.assertEqual(after, before)
        assert after is not None
        self.assertEqual(after["producer_type"], "HUMAN")
        self.assertEqual(after["producer_id"], "human:icloud-operator")
        self.assertEqual(after["submission_id"], "submission-human-recovery")
        self.assertEqual(after["execution_context"], context)

    def test_recovered_router_rejects_a_phase_that_is_not_recovery_bound(self) -> None:
        runner = self._runner([])
        result = AgentResult("COMPLETE", branch="topic")
        routed = runner._advance_after_recovered_provider_result(
            self.state, result, _Repository().inspect(self.root), "NOT_A_PROVIDER_PHASE",
        )
        self.assertTrue(routed.terminal)
        self.assertEqual(routed.next_action, "recovered_provider_phase_invalid")

    def test_second_interruption_consumes_budget_and_never_starts_a_third_attempt(self) -> None:
        agent = [_interrupted(), _interrupted(), AgentResult("COMPLETE", branch="topic")]
        runner = self._runner(agent)
        with self.assertRaises(CodexInvocationError) as raised:
            runner._invoke_agent_with_timing(self.state, "objective")
        self.assertTrue(raised.exception.provider_turn_interrupted)
        saved = self.store.load(self.state.run_id)
        self.assertEqual(saved.provider_recovery_attempts[0]["result"], "INTERRUPTED_AGAIN")
        self.assertEqual(len(runner.agent.outcomes), 1)
        with open_storage(self.root) as connection:
            count = connection.execute("SELECT COUNT(*) FROM provider_invocations").fetchone()[0]
        self.assertEqual(count, 2)

    def test_non_interruption_never_consumes_automatic_restart_budget(self) -> None:
        error = CodexInvocationError("CLI failed", "safe", next_action="inspect_codex_cli", terminal_condition="codex_invocation_failed")
        runner = self._runner([error])
        with self.assertRaises(CodexInvocationError):
            runner._invoke_agent_with_timing(self.state, "objective")
        self.assertEqual(self.store.load(self.state.run_id).provider_recovery_attempts, ())

    def test_controlled_hook_is_run_bound_one_shot_and_not_prompt_driven(self) -> None:
        runner = self._runner([AgentResult("COMPLETE", branch="topic")])
        original = os.environ.get("DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE")
        os.environ["DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE"] = f"{self.state.run_id}:{self.state.phase}"
        try:
            result = runner._invoke_agent_with_timing(self.state, "prompt prose cannot enable this hook")
        finally:
            if original is None:
                os.environ.pop("DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE", None)
            else:
                os.environ["DJCONNECT_ENGINEERING_TEST_INTERRUPT_PROVIDER_ONCE"] = original
        self.assertEqual(result.terminal_state, "COMPLETE")
        self.assertEqual(self.store.load(self.state.run_id).provider_recovery_attempts[0]["result"], "RECOVERED")
        self.assertEqual(len(runner.agent.outcomes), 0)

    def test_recovered_result_is_consumed_without_another_provider_call(self) -> None:
        runner = self._runner([])
        recovery = create_recovery_available(
            self.root, run_id=self.state.run_id, triggering_invocation_id="attempt-one",
            lifecycle_phase=self.state.phase, branch="topic", worktree_identity=str(self.root),
            lease_id=runner.active_lease.lease_id if runner.active_lease else None,
        )
        self.assertTrue(transition_recovery_state(
            self.root, run_id=self.state.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
        ))
        claim = claim_replacement_launch(self.root, run_id=self.state.run_id)
        assert claim is not None
        self.assertTrue(record_provider_started(
            self.root, run_id=self.state.run_id, receipt_id=str(claim["receipt_id"]), pid=os.getpid(), process_group=os.getpgrp(),
        ))
        reference = persist_recovery_agent_result(
            self.root, run_id=self.state.run_id,
            invocation_id=str(recovery["replacement_invocation_id"]), result=AgentResult("COMPLETE", branch="topic"),
        )
        self.assertTrue(record_replacement_terminal(
            self.root, run_id=self.state.run_id, outcome="SUCCESS", result_evidence_ref=reference,
        ))
        result = runner._invoke_agent_with_timing(self.state, "objective")
        self.assertEqual(result.terminal_state, "COMPLETE")
        self.assertEqual(runner.agent.outcomes, [])

    def test_same_phase_invalid_recovered_result_remains_fail_closed(self) -> None:
        runner = self._runner([])
        create_recovery_available(
            self.root, run_id=self.state.run_id, triggering_invocation_id="attempt-one",
            lifecycle_phase=self.state.phase, branch="topic", worktree_identity=str(self.root),
            lease_id=runner.active_lease.lease_id if runner.active_lease else None,
        )
        self.assertTrue(transition_recovery_state(
            self.root, run_id=self.state.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERED",
            result="SUCCESS", result_evidence_ref="artifact:missing-result",
        ))
        with self.assertRaisesRegex(CodexInvocationError, "Recovered provider result is unavailable"):
            runner._invoke_agent_with_timing(self.state, "objective")
        self.assertEqual(runner.agent.outcomes, [])

    def test_historical_quality_recovery_does_not_block_finalization_provider(self) -> None:
        quality = TransactionState(
            self.state.run_id, "pcvantol/djconnect", str(self.prompt), "QUALITY_CONTROL_AGENT", branch="topic",
        )
        finalization = TransactionState(
            self.state.run_id, "pcvantol/djconnect", str(self.prompt), "FINALIZE_AGENT", branch="topic",
            transaction_kind="FINALIZATION",
        )
        runner = _Runner(
            self.root, self.store, _Repository(), object(), _Agent([AgentResult("COMPLETE", branch="topic")]), lambda _: None,
        )
        self.store.save(quality)
        runner.active_lease = acquire(self.root, self.state.run_id, identity="test", instance_id="host")
        recovery = create_recovery_available(
            self.root, run_id=self.state.run_id, triggering_invocation_id="quality-attempt-one",
            lifecycle_phase="QUALITY_CONTROL_AGENT", branch="topic", worktree_identity=str(self.root),
            lease_id=runner.active_lease.lease_id,
        )
        self.assertTrue(transition_recovery_state(
            self.root, run_id=self.state.run_id, expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
        ))
        claim = claim_replacement_launch(self.root, run_id=self.state.run_id)
        assert claim is not None
        self.assertTrue(record_provider_started(
            self.root, run_id=self.state.run_id, receipt_id=str(claim["receipt_id"]), pid=os.getpid(), process_group=os.getpgrp(),
        ))
        reference = persist_recovery_agent_result(
            self.root, run_id=self.state.run_id, invocation_id=str(recovery["replacement_invocation_id"]),
            result=AgentResult("COMPLETE", branch="topic"),
        )
        self.assertTrue(record_replacement_terminal(
            self.root, run_id=self.state.run_id, outcome="SUCCESS", result_evidence_ref=reference,
        ))

        result = runner._invoke_agent_with_timing(finalization, "finalize")

        self.assertEqual(result.terminal_state, "COMPLETE")
        self.assertEqual(runner.agent.outcomes, [])
        with open_storage(self.root) as connection:
            recovery_row = connection.execute(
                "SELECT lifecycle_phase,state FROM provider_recovery_attempts WHERE run_id=?", (self.state.run_id,),
            ).fetchone()
            finalization_invocations = connection.execute(
                "SELECT COUNT(*) FROM provider_invocations WHERE run_id=? AND phase='PROVIDER_EXECUTION'", (self.state.run_id,),
            ).fetchone()[0]
        self.assertEqual(recovery_row, ("QUALITY_CONTROL_AGENT", "RECOVERED"))
        self.assertEqual(finalization_invocations, 1)
