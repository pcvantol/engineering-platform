"""Offline documentation guards only: no runtime/model-policy qualification."""
from graphlib import TopologicalSorter
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = ROOT / "docs/development/ROLE_TASK_MODEL_POLICY_V1_DAG.json"


class RoleTaskModelPolicyDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.parent = json.loads((ROOT / cls.graph["parent_graph"]).read_text(encoding="utf-8"))
        cls.design = (ROOT / cls.graph["architecture"]).read_text(encoding="utf-8")
        cls.roadmap = (ROOT / cls.graph["roadmap"]).read_text(encoding="utf-8")

    def test_documentary_only_without_activation_or_canary_dependency(self):
        g = self.graph
        self.assertEqual(g["increment"], "EP_ROLE_TASK_MODEL_POLICY_V1")
        self.assertEqual(g["parent_node"], "SA-ROLE")
        self.assertEqual(g["finding"], "SA-F09")
        self.assertEqual(g["status"], "PLANNED")
        self.assertEqual(g["version_change"], "NO_BUMP")
        self.assertTrue(g["documentary_only"])
        for flag in ("execution_authority", "automatic_dispatch", "runtime_activation", "first_canary_prerequisite"):
            self.assertIs(g[flag], False)
        self.assertEqual(g["actual_model_ids_selected"], [])

    def test_parent_decomposition_preserves_original_dependencies(self):
        ext = self.parent["role_task_model_policy"]
        self.assertEqual(ext["graph"], GRAPH_PATH.relative_to(ROOT).as_posix())
        self.assertEqual(ext["completion_requires"], ["RMP-Q"])
        parent_nodes = {n["id"]: n for n in self.parent["nodes"]}
        self.assertEqual(len(parent_nodes), 11)
        self.assertEqual(parent_nodes["SA-ROLE"]["depends_on"], ["SA-CTX", "SA-OBS"])
        self.assertEqual(parent_nodes["SA-ROLE"]["status"], "PLANNED")
        self.assertEqual(parent_nodes["SA-ROLE"]["qualification_evidence"], [])
        self.assertEqual(parent_nodes["SA-OBS"]["depends_on"], ["SA-ISO"])
        self.assertIn("SA-ROLE", parent_nodes["SA-Q"]["depends_on"])

    def test_seven_nodes_and_full_nested_graph_remain_acyclic(self):
        nodes = self.graph["nodes"]
        ids = {n["id"] for n in nodes}
        self.assertEqual(len(nodes), 7)
        self.assertEqual(len(ids), 7)
        deps = {n["id"]: list(n["depends_on"]) for n in nodes}
        for n in nodes:
            self.assertEqual(n["owner"], "engineering-platform")
            self.assertEqual(n["status"], "PLANNED")
            self.assertTrue(n["completion_evidence"])
            self.assertTrue(set(n["depends_on"]) <= ids)
            self.assertNotIn(n["id"], n["depends_on"])
            self.assertEqual(len(n["depends_on"]), len(set(n["depends_on"])))
        self.assertEqual(set(TopologicalSorter(deps).static_order()), ids)
        combined = {n["id"]: list(n["depends_on"]) for n in self.parent["nodes"]}
        combined.update(deps)
        for n, external in self.graph["external_dependencies"].items():
            combined[n].extend(external)
        for e in self.graph["external_requirements"]:
            self.assertEqual(e["owning_lane"], "POL-E")
            self.assertEqual(e["evidence"], [])
            combined[e["id"]] = []
        for e in self.parent["external_requirements"]:
            combined[e["id"]] = e["depends_on"]
        combined["SA-ROLE"].extend(self.parent["role_task_model_policy"]["completion_requires"])
        self.assertTrue(all(set(v) <= combined.keys() for v in combined.values()))
        self.assertEqual(set(TopologicalSorter(combined).static_order()), set(combined))

    def test_markdown_and_json_delivery_edges_match(self):
        rows = {}
        for line in self.roadmap.splitlines():
            if line.startswith("| RMP-"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                rows[cells[0]] = set(re.findall(r"RMP-[A-Z]+", cells[2]))
        self.assertEqual(rows, {n["id"]: set(n["depends_on"]) for n in self.graph["nodes"]})

    def test_task_role_matrix_keeps_controls_and_reviews_distinct(self):
        tasks = {n["task"]: n for n in self.graph["task_matrix"]}
        self.assertEqual(len(tasks), 11)
        for key, row in tasks.items():
            self.assertIn(f"| {key} |", self.design)
        for task in ("QUALITY_REVIEW", "SECURITY_REVIEW", "SPECIALIST_REVIEW", "FAILURE_DIAGNOSIS"):
            self.assertEqual(tasks[task]["mode"], "READ_ONLY")
        for task in ("VALIDATION_CONTROL", "PUBLICATION_CONTROL", "RECONCILIATION_CONTROL"):
            self.assertEqual(tasks[task]["mode"], "NO_LLM_TARGET")
            self.assertIsNone(tasks[task]["profile_intent"])
        self.assertNotEqual(tasks["QUALITY_REVIEW"]["profile_intent"], tasks["SECURITY_REVIEW"]["profile_intent"])

    def test_identity_default_and_subscription_safety(self):
        inv = self.graph["invariants"]
        for key in ("provider_default_requires_explicit_policy", "per_invocation_settings", "qualified_binding_required_before_activation"):
            self.assertIs(inv[key], True)
        for key in ("requested_is_observed", "missing_usage_is_zero", "automatic_api_fallback",
                    "unknown_observed_model_can_satisfy_exact_requirement", "all_optional_providers_required_for_v1"):
            self.assertIs(inv[key], False)
        for text in ("PROVIDER_DEFAULT", "NOT_REPORTED", "No authfile scraping", "existing EP-managed Codex"):
            self.assertIn(text, self.design)

    def test_no_new_retry_authority_or_budget_reset(self):
        inv = self.graph["invariants"]
        for key in ("allow_ambiguous_fallback", "allow_review_shopping", "allow_repair_budget_reset",
                    "model_switch_grants_authority", "current_timeout_constants_editable",
                    "live_profile_edits_change_active_runs"):
            self.assertIs(inv[key], False)
        self.assertEqual(inv["current_runwide_repair_limit"], 3)
        for text in ("MAY_HAVE_HAPPENED", "finite acyclic", "run/continuation lineage", "same EP provider boundary"):
            self.assertIn(text, self.design)

    def test_sixteen_required_scenario_families_are_visible(self):
        scenarios = self.graph["scenarios"]
        self.assertEqual([s["id"] for s in scenarios], [f"RMT-{i:02}" for i in range(1, 17)])
        for s in scenarios:
            self.assertIs(s["required"], True)
            self.assertEqual(s["status"], "PLANNED")
            self.assertIn(f'| {s["id"]} |', self.roadmap)
        self.assertIn("Documentary tests alone are not execution qualification", self.design)

    def test_console_and_source_provenance_are_explicit(self):
        self.assertRegex(self.graph["source_pin"], r"^[0-9a-f]{40}$")
        self.assertIn(self.graph["source_pin"], self.design)
        for phrase in ("AI task/role policies", "en/nl/de/fr/es", "expected revision/digest",
                       "predeclared floors", "No separate API subscription", "direct peer SQL"):
            self.assertIn(phrase, self.design)
        self.assertIs(self.graph["invariants"]["page_refresh_generates"], False)
        self.assertIs(self.graph["invariants"]["forge_planning_policy_unchanged"], True)
        self.assertIs(self.graph["invariants"]["metadata_redefines_terminal_v12_without_versioning"], False)

    def test_navigation_reaches_one_design_from_both_owning_lanes(self):
        for key in ("architecture", "roadmap", "parent_graph", "policy_roadmap"):
            self.assertTrue((ROOT / self.graph[key]).is_file())
        for path in ("docs/development/SUBAGENT_ORCHESTRATION_V1_ROADMAP.md", self.graph["policy_roadmap"]):
            text = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn("ROLE_TASK_MODEL_POLICY_V1.md", text)
            self.assertIn("ROLE_TASK_MODEL_POLICY_V1_ROADMAP.md", text)


if __name__ == "__main__":
    unittest.main()
