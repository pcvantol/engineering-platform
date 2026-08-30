from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.engineering.agent_state import StateStore, TransactionState
from tools.engineering.execution_lease import acquire, history
from tools.engineering.execution_timing import phase_spans, start_phase
from tools.engineering.provider_interruption import terminalize_after_host_exit
from tools.engineering.provider_usage import ProviderInvocation, persist_provider_invocation


class ProviderInterruptionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = StateStore(self.root / ".engineering" / "engineering-runs")
        self.state = TransactionState(
            "provider-interruption-run", "pcvantol/djconnect", "prompt.md",
            "LOCAL_REPOSITORY_VALIDATION", next_action="run_local_repository_validation",
        )
        self.store.save(self.state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _interrupted_invocation(self) -> None:
        persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id=self.state.run_id, ordinal=1, provider="codex_cli", model=None,
                phase="PROVIDER_EXECUTION", role="IMPLEMENTATION",
                started_at="2026-08-30T08:00:00+00:00", completed_at="2026-08-30T08:00:01+00:00",
                duration_ms=None, usage={},
                churn={
                    "interruption_classification": "provider_turn_interrupted",
                    "interruption_reason": "interrupted",
                },
            ),
        )

    def test_provider_proven_interruption_terminalizes_checkpoint_spans_and_lease(self) -> None:
        self._interrupted_invocation()
        start_phase(self.root, self.state.run_id, "PROVIDER_EXECUTION")
        lease = acquire(self.root, self.state.run_id, identity="test", instance_id="host")

        terminal = terminalize_after_host_exit(self.root, self.state.run_id)

        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal.phase, "FAILED")
        self.assertTrue(terminal.terminal)
        self.assertEqual(terminal.next_action, "NONE")
        self.assertEqual(terminal.terminal_condition, "provider_turn_interrupted")
        self.assertIn("Provider invocation:", terminal.diagnostic or "")
        self.assertEqual(history(self.root, lease.run_id)["lease_state"], "RELEASED")
        self.assertEqual(phase_spans(self.root, lease.run_id)[0]["outcome"], "INTERRUPTED")

    def test_unclassified_provider_failure_is_not_reinterpreted_as_interruption(self) -> None:
        persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id=self.state.run_id, ordinal=1, provider="codex_cli", model=None,
                phase="PROVIDER_EXECUTION", role="IMPLEMENTATION",
                started_at="2026-08-30T08:00:00+00:00", completed_at="2026-08-30T08:00:01+00:00",
                duration_ms=None, usage={}, churn={},
            ),
        )
        self.assertIsNone(terminalize_after_host_exit(self.root, self.state.run_id))
        self.assertFalse(self.store.load(self.state.run_id).terminal)

    def test_interrupted_child_span_and_unavailable_usage_terminalize_without_jsonl_abort(self) -> None:
        """A provider crash may lose its final JSONL event after cancelling a child command."""
        provider = start_phase(self.root, self.state.run_id, "PROVIDER_EXECUTION")
        child = start_phase(
            self.root, self.state.run_id, "VALIDATION", parent_phase_id=provider.phase_id,
        )
        from tools.engineering.execution_timing import complete_phase

        complete_phase(self.root, child, outcome="INTERRUPTED")
        complete_phase(self.root, provider, outcome="FAILED")
        persist_provider_invocation(
            self.root,
            ProviderInvocation(
                run_id=self.state.run_id, ordinal=1, provider="codex_cli", model=None,
                phase="QUALITY_CONTROL", role="QUALITY_REVIEW",
                started_at="2026-08-30T08:00:00+00:00", completed_at="2026-08-30T08:00:01+00:00",
                duration_ms=None, usage={}, churn={},
            ),
        )

        terminal = terminalize_after_host_exit(self.root, self.state.run_id)

        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal.terminal_condition, "provider_turn_interrupted")
        self.assertIn("interrupted_child_span_without_provider_result", terminal.diagnostic or "")

    def test_lease_cleanup_failure_does_not_overwrite_terminal_checkpoint(self) -> None:
        self._interrupted_invocation()
        with patch("tools.engineering.provider_interruption.release_terminal_lease", side_effect=Exception("offline")):
            terminal = terminalize_after_host_exit(self.root, self.state.run_id)
        self.assertIsNotNone(terminal)
        saved = self.store.load(self.state.run_id)
        self.assertTrue(saved.terminal)
        self.assertEqual(saved.terminal_condition, "provider_turn_interrupted")
