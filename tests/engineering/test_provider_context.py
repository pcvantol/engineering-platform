from __future__ import annotations

import unittest

from engineering_platform.provider_context import ProviderRole, project_context, provider_need_for_phase
from engineering_platform.provider_context_benchmark import benchmark_shape


OBJECTIVE = """# Objective
Implement the bounded change.

# Non-negotiable constraints
Do not weaken validation or merge authority.

# Acceptance criteria
Add focused tests.

# Historical transcript
This lower-priority historical context is deliberately long and is not needed by a repair.\n""" + ("old detail\n" * 4000)


class ProviderContextTest(unittest.TestCase):
    def test_mandatory_contract_includes_nested_sections_with_neutral_titles(self) -> None:
        objective = "# Acceptance criteria\n## Scenarios\nRequired case A.\n### Details\nRequired case B.\n# History\n## Transcript\nOptional old notes.\n"
        projection = project_context(ProviderRole.SECURITY_REVIEW, objective)
        self.assertIn("Required case A.", projection.text)
        self.assertIn("Required case B.", projection.text)
        self.assertNotIn("Optional old notes.", projection.text)

    def test_overflow_retains_every_mandatory_section(self) -> None:
        objective = "# Objective\n" + ("bounded work\n" * 2000) + "\n# Safety constraints\nNever change authority.\n# Acceptance criteria\nRequire independent security review.\n"
        for role in (ProviderRole.SPECIALIST_REVIEW, ProviderRole.QUALITY_REVIEW,
                     ProviderRole.REPAIR, ProviderRole.FINALIZATION, ProviderRole.SECURITY_REVIEW):
            with self.subTest(role=role):
                projection = project_context(role, objective)
                self.assertIn("Never change authority.", projection.text)
                self.assertIn("Require independent security review.", projection.text)
                self.assertEqual(projection.omitted_low_priority_count, 0)
                self.assertGreater(projection.telemetry["context_budget_overflow_bytes"], 0)

    def test_deterministic_and_passive_phases_require_no_provider(self) -> None:
        for phase in ("INITIALIZE", "RECONCILIATION", "WAIT_FOR_OPERATOR_MERGE", "LOCAL_REPOSITORY_VALIDATION"):
            self.assertFalse(provider_need_for_phase(phase).required)
        self.assertFalse(provider_need_for_phase("EXECUTE_AGENT", passive_observation=True).required)
        self.assertTrue(provider_need_for_phase("EXECUTE_AGENT").required)

    def test_downstream_roles_keep_mandatory_contract_without_full_replay(self) -> None:
        implementation = project_context(ProviderRole.IMPLEMENTATION, OBJECTIVE)
        for role in (ProviderRole.SPECIALIST_REVIEW, ProviderRole.QUALITY_REVIEW, ProviderRole.REPAIR, ProviderRole.FINALIZATION, ProviderRole.SECURITY_REVIEW):
            projection = project_context(role, OBJECTIVE)
            self.assertIn("Do not weaken validation or merge authority.", projection.text)
            self.assertIn("Add focused tests.", projection.text)
            self.assertNotIn("old detail\nold detail", projection.text)
            self.assertLess(len(projection.text), len(implementation.text))
            self.assertGreater(projection.telemetry["context_omitted_low_priority_count"], 0)

    def test_structural_benchmark_has_zero_provider_passive_paths_and_smaller_repair(self) -> None:
        result = benchmark_shape(OBJECTIVE)
        self.assertEqual(result["deterministic_preflight_blocker"]["provider_calls"], 0)
        self.assertEqual(result["passive_merge_wait"]["provider_calls"], 0)
        self.assertLess(result["repair"]["context_bytes"], result["baseline_full_replay_bytes"]["context_bytes"])
