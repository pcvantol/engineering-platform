"""Offline documentary guards; these do not qualify runtime Action sizing."""
from graphlib import TopologicalSorter
from hashlib import sha256
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / 'docs/development/action-execution-envelope-v1.json'
OWNER = 'engineering-platform'
NODE_COUNT = 3


class AdaptiveActionSizingDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = json.loads(GRAPH.read_text(encoding="utf-8"))
        cls.design = (ROOT / cls.g["architecture"]).read_text(encoding="utf-8")
        cls.parent = (ROOT / cls.g["parent_roadmap"]).read_text(encoding="utf-8")

    def test_documentation_is_not_active_runtime(self):
        self.assertEqual(self.g["increment"], "ADAPTIVE_ACTION_SIZING_V1")
        self.assertEqual(self.g["owner"], OWNER)
        self.assertEqual(self.g["authority"], "DOCUMENTARY")
        self.assertEqual(self.g["status"], "PLANNED")
        self.assertEqual(self.g["version_change"], "NO_BUMP")
        for key in ("executable", "authorizes_execution", "first_canary_prerequisite"):
            self.assertIs(self.g[key], False)

    def test_presets_are_independent_adaptive_preferences(self):
        self.assertEqual(self.g["presets"], ["SMALLER", "BALANCED_ADAPTIVE", "LARGER_COHERENT"])
        self.assertEqual(self.g["proposed_new_policy_default"], "BALANCED_ADAPTIVE")
        self.assertIs(self.g["invariants"]["all_presets_adaptive"], True)
        self.assertIs(self.g["invariants"]["live_defaults_changed"], False)

    def test_local_dag_and_markdown_edges_agree(self):
        nodes = self.g["nodes"]
        mapping = {n["id"]: n["depends_on"] for n in nodes}
        self.assertEqual(len(nodes), NODE_COUNT)
        self.assertEqual(len(mapping), NODE_COUNT)
        for n in nodes:
            self.assertEqual(n["owner"], OWNER)
            self.assertEqual(n["status"], "PLANNED")
            self.assertEqual(n["qualification_evidence"], [])
            self.assertTrue(set(n["depends_on"]) <= mapping.keys())
            self.assertNotIn(n["id"], n["depends_on"])
        self.assertEqual(set(TopologicalSorter(mapping).static_order()), set(mapping))
        rows = {}
        for line in self.design.splitlines():
            if line.startswith("| AS-"):
                cells = [v.strip() for v in line.strip("|").split("|")]
                rows[cells[0]] = set(re.findall(r"AS-[FEW]-[A-Z]+", cells[1]))
        self.assertEqual(rows, {k: set(v) for k, v in mapping.items()})

    def test_owner_limits_remain_separate(self):
        inv = self.g["invariants"]
        for k in ("forge_owns_decomposition", "ep_owns_execution_fit", "first_action_requires_sizing"):
            self.assertIs(inv[k], True)
        for k in ("workspace_owns_scheduler", "action_equals_one_llm_call", "sizing_weakens_assurance",
                  "planning_tokens_are_execution_tokens", "fit_is_authority_or_reservation"):
            self.assertIs(inv[k], False)
        self.assertEqual(inv["peer_transport"], "HTTP_ONLY")

    def test_evidence_and_recovery_are_not_fabricated(self):
        for k in ("requested_is_observed", "missing_usage_is_zero", "mandatory_context_truncation_allowed",
                  "repair_budget_reset_allowed", "ambiguous_repartition_retry_allowed", "automatic_paid_api_fallback",
                  "materialized_action_mutable", "refresh_generates", "mock_ci_is_live_proof"):
            self.assertIs(self.g["invariants"][k], False)

    def test_shared_scenario_ids_and_registry_digest(self):
        registry = self.g["scenario_registry"]
        expected = [f"AS-T{i:02}" for i in range(1, 21)]
        self.assertEqual(registry["ids"], expected)
        self.assertTrue(set(self.g["applicable_scenarios"]) <= set(expected))
        self.assertRegex(registry["sha256"], r"^[0-9a-f]{64}$")
        if OWNER == "forge":
            cases = self.g["scenarios"]
            self.assertEqual([s["id"] for s in cases], expected)
            for s in cases:
                self.assertTrue(s["required"])
                self.assertEqual(s["status"], "PLANNED")
                self.assertTrue(s["requirement"])
            payload = json.dumps(cases, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(sha256(payload).hexdigest(), registry["sha256"])

    def test_navigation_and_source_pins(self):
        for k in ("architecture", "parent_roadmap"):
            self.assertTrue((ROOT / self.g[k]).is_file())
        self.assertIn(Path(self.g["architecture"]).name, self.parent)
        self.assertIn(GRAPH.name, self.parent)
        self.assertIn(self.g["source_pins"][OWNER], self.design)
        for pin in self.g["source_pins"].values():
            self.assertRegex(pin, r"^[0-9a-f]{40}$")
        self.assertTrue(self.g["required_subset_evidence"])

    def test_no_dependency_on_consumer_ui_for_forge_runtime(self):
        node_ids = {n["id"] for n in self.g["nodes"]}
        for own, dependencies in self.g["external_dependencies"].items():
            self.assertIn(own, node_ids)
            self.assertTrue(dependencies)
            for dep in dependencies:
                self.assertNotIn(dep, node_ids)
                self.assertRegex(dep, r"^AS-[FEW]-[A-Z]+$")
                if OWNER != "workspace":
                    self.assertFalse(dep.startswith("AS-W-"))
        if OWNER == "forge":
            join = self.g["ci_join"]
            self.assertEqual(join["retained_scenarios"], "FIE-01..28")
            self.assertEqual(len(join["nodes"]), 7)
            self.assertTrue(join["real_forge_logic_required"])
            self.assertIs(join["skipped_required_is_pass"], False)
            text = (ROOT / "docs/roadmap/FORGE_INNER_LOOP_CI_V1.md").read_text(encoding="utf-8")
            self.assertIn("AS-T01..20", text)
            self.assertIn(GRAPH.name, text)


if __name__ == "__main__":
    unittest.main()
