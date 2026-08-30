from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

from tools.engineering.evidence_projection import ToolProxyEnvironment
from tools.engineering.provider_context_scope import (
    ContextEscalationReason,
    ContextEscalationRequest,
    ContextScope,
    HistoryBoundaryKind,
    MAX_HISTORICAL_COMMITS,
    initial_context_scope,
    provider_instruction,
)
from tools.engineering.provider_usage import churn_from_jsonl


class ProviderContextScopeTests(unittest.TestCase):
    def test_fresh_run_defaults_to_normal_and_current_delta_first(self) -> None:
        self.assertEqual(initial_context_scope(phase="EXECUTE_AGENT"), ContextScope.NORMAL)
        instruction = provider_instruction(ContextScope.NORMAL)
        self.assertIn("current branch/worktree/status", instruction)
        self.assertIn("merge-base delta", instruction)
        self.assertIn("Do not enumerate historical pull requests", instruction)
        self.assertIn("broad git history", instruction)
        self.assertLess(len(instruction.encode("utf-8")), 1_400)

    def test_retry_repair_admits_only_direct_predecessor_evidence(self) -> None:
        self.assertEqual(initial_context_scope(phase="REPAIR_AGENT"), ContextScope.RETRY_REPAIR)
        instruction = provider_instruction(ContextScope.RETRY_REPAIR)
        self.assertIn("Direct predecessor", instruction)
        self.assertIn("do not load older ancestors", instruction)

    def test_explicit_historical_audit_can_start_in_investigation(self) -> None:
        self.assertEqual(
            initial_context_scope(phase="EXECUTE_AGENT", objective="Perform a historical audit of this contract."),
            ContextScope.INVESTIGATION,
        )

    def test_escalation_requires_a_valid_reason_and_bounded_boundary(self) -> None:
        request = ContextEscalationRequest(
            ContextEscalationReason.REGRESSION_ORIGIN_UNKNOWN,
            HistoryBoundaryKind.COMMITS_TOUCHING_PATH, "tools/engineering/provider_context.py", 10,
            "Current delta does not explain the regression.",
        ).validate()
        self.assertEqual(request.limit, MAX_HISTORICAL_COMMITS)
        with self.assertRaises(ValueError):
            ContextEscalationRequest(
                ContextEscalationReason.OTHER_BOUNDED_INVESTIGATION,
                HistoryBoundaryKind.COMMITS_TOUCHING_PATH, "file.py", 11, "Need more context."
            ).validate()

    def test_helper_persists_a_bounded_escalation_and_rejects_unbounded(self) -> None:
        proxy = ToolProxyEnvironment()
        with proxy as environment:
            ok = subprocess.run(
                ("djconnect-context-escalate", "BLAME_REQUIRED", "COMMITS_TOUCHING_PATH", "file.py", "2",
                 "--diagnostic", "Current source does not identify the authoring change."),
                text=True, capture_output=True, env=environment, check=False,
            )
            rejected = subprocess.run(
                ("djconnect-context-escalate", "BLAME_REQUIRED", "COMMITS_TOUCHING_PATH", "file.py", "11",
                 "--diagnostic", "Current source does not identify the authoring change."),
                text=True, capture_output=True, env=environment, check=False,
            )
            self.assertEqual(ok.returncode, 0)
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("CONTEXT_ESCALATION_ADMITTED", ok.stdout)
            records = proxy.context_escalations()
            self.assertEqual(records[0]["reason"], "BLAME_REQUIRED")
        # The temporary invocation ledger is deliberately destroyed on exit.
        self.assertFalse(Path(environment["DJCONNECT_CONTEXT_ESCALATION_FILE"]).exists())

    def test_history_telemetry_is_observed_only_when_a_history_query_runs(self) -> None:
        no_history = churn_from_jsonl(
            '{"type":"item.completed","item":{"type":"command_execution","command":"git status","aggregated_output":"## main"}}'
        )
        self.assertNotIn("historical_pr_queries", no_history)
        history = churn_from_jsonl(
            '{"type":"item.completed","item":{"type":"command_execution","command":"git log --max-count=2","aggregated_output":"a\\nb"}}\n'
            '{"type":"item.completed","item":{"type":"command_execution","command":"gh pr list --limit 2","aggregated_output":"1\\n2"}}'
        )
        self.assertEqual(history["historical_commit_queries"], 1)
        self.assertEqual(history["historical_pr_queries"], 1)
        self.assertEqual(history["historical_context_bytes"], 6)
