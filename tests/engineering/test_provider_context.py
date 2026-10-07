from __future__ import annotations

import unittest
import json

from engineering_platform.provider_context import ProviderRole, project_context, provider_need_for_phase
from engineering_platform.provider_context_benchmark import benchmark_shape
from engineering_platform.capability_review import ReviewerSelection, reviewer_prompt, mandatory_coverage_surfaces


OBJECTIVE = """# Objective
Implement the bounded change.

# Non-negotiable constraints
Do not weaken validation or merge authority.

# Acceptance criteria
Add focused tests.

# Historical transcript
This lower-priority historical context is deliberately long and is not needed by a repair.\n""" + ("old detail\n" * 4000)


class ProviderContextTest(unittest.TestCase):
    def test_raw_markup_is_unclassified_and_retained_in_full(self) -> None:
        objective = "# Safety\n<pre>\n# Historical transcript\n</pre>\nNever omit this required rule.\n"
        projection = project_context(ProviderRole.SECURITY_REVIEW, objective)
        self.assertEqual(projection.text, objective)
        self.assertEqual(projection.omitted_low_priority_count, 0)

    def test_unknown_headings_are_required_even_when_nested_in_optional_history(self) -> None:
        for heading in ("Security requirements", "Delivery conditions", "Constraints", "Unrecognised obligations"):
            for level in ("# ", "## "):
                objective = ("# Objective\nAssess the candidate.\n# Optional history\nold notes.\n"
                             + level + heading + "\nDO_NOT_PUBLISH_WITHOUT_OWNER_AUTHORIZATION\n")
                for role in ("quality", "security"):
                    with self.subTest(heading=heading, level=level, role=role):
                        prompt = json.loads(reviewer_prompt(ReviewerSelection(role, "mandatory", 1.0), objective))
                        self.assertIn("DO_NOT_PUBLISH_WITHOUT_OWNER_AUTHORIZATION", prompt["objective"])

    def test_unknown_parent_keeps_complete_contract_and_unclassified_history(self) -> None:
        objective = ("# Delivery conditions\n## Optional history\nStill part of this unknown contract.\n"
                     "# History\nUnclassified required lineage.\n")
        projection = project_context(ProviderRole.QUALITY_REVIEW, objective)
        self.assertIn("Still part of this unknown contract.", projection.text)
        self.assertIn("Unclassified required lineage.", projection.text)
        self.assertEqual(projection.omitted_low_priority_count, 0)

    def test_actual_mandatory_reviewer_prompts_keep_fenced_safety_and_role_rubrics(self) -> None:
        objective = "#\tSafety constraints\n## Details\n```sh\n# inert sample comment\n```\nNever publish without owner authorization.\n"
        for role in ("quality", "security"):
            surfaces = mandatory_coverage_surfaces(role, "IMPLEMENTATION")
            selection = ReviewerSelection(role, "mandatory assurance", 1.0,
                                           required_coverage_surfaces=surfaces)
            prompt = json.loads(reviewer_prompt(selection, objective))
            self.assertIn("Never publish without owner authorization.", prompt["objective"])
            self.assertEqual(prompt["context_projection"]["role"], role.upper() + "_REVIEW")
            for surface in surfaces:
                self.assertIn(surface, json.dumps(prompt))

    def test_tabs_and_fenced_samples_cannot_end_the_mandatory_section(self) -> None:
        for fence in ("```sh", "~~~~", "````python"):
            closing = fence.split("sh")[0].split("python")[0]
            objective = ("  #\tSafety constraints\n## Details\n" + fence
                         + "\n# inert sample comment\n## sample\n"
                         + closing + "\nNever publish without owner authorization.\n"
                         + "# Historical transcript\noptional past notes.\n")
            with self.subTest(fence=fence):
                projection = project_context(ProviderRole.SECURITY_REVIEW, objective)
                self.assertIn("Never publish without owner authorization.", projection.text)
                self.assertIn("# inert sample comment", projection.text)
                self.assertNotIn("optional past notes.", projection.text)

    def test_unclosed_and_mixed_fences_preserve_required_remaining_material(self) -> None:
        objective = "# Safety constraints\n```example\n~~~\n# inert\nNever drop this required rule.\n"
        self.assertIn("Never drop this required rule.", project_context(ProviderRole.QUALITY_REVIEW, objective).text)

    def test_mandatory_contract_includes_nested_sections_with_neutral_titles(self) -> None:
        objective = "# Acceptance criteria\n## Scenarios\nRequired case A.\n### Details\nRequired case B.\n# Optional history\n## Optional transcript\nOptional old notes.\n"
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
