from __future__ import annotations

from pathlib import Path
import unittest
from types import SimpleNamespace

from engineering_platform.execution_readiness import ReadinessFacts, Requirement, decide, evaluate, selected_profile


class ExecutionReadinessTest(unittest.TestCase):
    def test_genesis_selects_only_target_profile(self) -> None:
        result = evaluate(
            selected_profile("GENESIS"), host_root=Path("/host"), target_root=Path("/target"),
            managed_clean=lambda _: False, genesis_preflight=lambda _: None,
        )
        self.assertEqual(result.profile.profile_id, "genesis_target")
        self.assertEqual(result.profile.remote, Requirement.NOT_APPLICABLE)
        self.assertTrue(result.ready)

    def test_managed_failure_does_not_call_genesis_preflight(self) -> None:
        result = evaluate(
            selected_profile("MANAGED"), host_root=Path("/host"), target_root=None,
            managed_clean=lambda _: False,
            genesis_preflight=lambda _: self.fail("Genesis readiness must not run for Managed work"),
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.profile.profile_id, "managed_repository")
        self.assertEqual(result.profile.remote, Requirement.REQUIRED)

    def test_decision_lists_failed_typed_requirements(self) -> None:
        decision = decide(
            selected_profile("MANAGED"),
            ReadinessFacts(
                True, True, False, True,
                remote_present=True, upstream_present=True, branch_present=True,
                workspace_authorized=True, capabilities_available=True,
                providers_available=True, datastore_healthy=True, producer_contract_valid=True,
            ),
        )
        self.assertFalse(decision.passed)
        self.assertEqual(decision.failed_requirements, ("clean_worktree",))

    def test_missing_required_fact_fails_closed(self) -> None:
        decision = decide(selected_profile("MANAGED"), ReadinessFacts(True, True, True, True))
        self.assertFalse(decision.passed)
        self.assertIn("remote", decision.failed_requirements)
        self.assertIn("datastore", decision.failed_requirements)

    def test_provider_requirement_is_explicit_and_fail_closed(self) -> None:
        facts = ReadinessFacts(
            True, True, True, True, remote_present=True, upstream_present=True,
            branch_present=True, workspace_authorized=True, capabilities_available=True,
            providers_available=False, datastore_healthy=True, producer_contract_valid=True,
        )
        decision = decide(selected_profile("MANAGED"), facts)
        self.assertFalse(decision.passed)
        self.assertEqual(decision.failed_requirements, ("providers",))

    def test_preflight_adapter_uses_existing_observed_outcomes(self) -> None:
        workspace = SimpleNamespace(checks=(SimpleNamespace(identifier="target_repository", outcome="PASS"), SimpleNamespace(identifier="clean_worktree", outcome="PASS")))
        facts = ReadinessFacts.from_preflight(host=SimpleNamespace(outcome="PASS"), workspace=workspace, capability=SimpleNamespace(outcome="PASS"), lease_available=True)
        self.assertTrue(facts.host_ready)
        self.assertTrue(facts.repository_present)
        self.assertTrue(facts.repository_clean)
        self.assertTrue(facts.capabilities_available)
