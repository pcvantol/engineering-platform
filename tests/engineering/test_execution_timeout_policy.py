from __future__ import annotations

import unittest

from engineering_platform.execution_timeout_policy import (
    AUTONOMOUS_QUALITY_CONTROL,
    END_RECONCILIATION,
    FINALIZATION,
    IMPLEMENTATION,
    LOCAL_REPOSITORY_VALIDATION,
    REPAIR,
    SPECIALIST_REVIEW,
    WORKFLOW_TIMEOUTS,
    agent_timeout,
)


class ExecutionTimeoutPolicyTests(unittest.TestCase):
    def test_policy_has_a_bounded_timeout_for_every_provider_workflow_stage(self) -> None:
        self.assertEqual(
            tuple(item.key for item in WORKFLOW_TIMEOUTS),
            (
                "specialist_review", "implementation", "local_repository_validation",
                "autonomous_quality_control", "repair", "finalization", "end_reconciliation",
            ),
        )
        self.assertEqual(SPECIALIST_REVIEW.seconds, 5 * 60)
        self.assertEqual(IMPLEMENTATION.seconds, 15 * 60)
        self.assertEqual(LOCAL_REPOSITORY_VALIDATION.seconds, 15 * 60)
        self.assertEqual(AUTONOMOUS_QUALITY_CONTROL.seconds, 10 * 60)
        self.assertEqual(REPAIR.seconds, 15 * 60)
        self.assertEqual(FINALIZATION.seconds, 15 * 60)
        self.assertEqual(END_RECONCILIATION.seconds, 10 * 60)

    def test_primary_timeout_selection_is_phase_and_action_specific(self) -> None:
        self.assertIs(agent_timeout(phase="EXECUTE_AGENT"), IMPLEMENTATION)
        self.assertIs(agent_timeout(phase="LOCAL_REPOSITORY_VALIDATION", local_validation=True), LOCAL_REPOSITORY_VALIDATION)
        self.assertIs(agent_timeout(phase="QUALITY_CONTROL", quality=True), AUTONOMOUS_QUALITY_CONTROL)
        self.assertIs(agent_timeout(phase="REPAIR_AGENT", repair=True), REPAIR)
        self.assertIs(agent_timeout(phase="FINALIZATION"), FINALIZATION)
        self.assertIs(agent_timeout(phase="RECONCILE_AGENT"), END_RECONCILIATION)
