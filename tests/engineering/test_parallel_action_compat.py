"""EP PA-E0 consumer contract against the protected Forge PA-F0 peer bytes."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from engineering_platform.parallel_action_compat import (
    CompatibilityError,
    CompatibilityScope,
    MAX_DOCUMENT_BYTES,
    READBACK_VERSION,
    assess_parallel_action_graph,
)


_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/forge-parallel-action-peer-graph-v1.json"
_FIXTURE_SHA256 = "b938388fb7a031c574407b62f07cb3ed7b12d692170dd4acf81cba5a14a3ab9c"
_SCOPE = CompatibilityScope("ep-fixture-1", "project-fixture-1", ("repository-a", "repository-b"))


def _source() -> dict:
    return json.loads(_FIXTURE.read_bytes())


def _assess(graph: dict) -> dict:
    return assess_parallel_action_graph(json.dumps(graph).encode(), scope=_SCOPE)


class ParallelActionCompatibilityTests(unittest.TestCase):
    def test_protected_forge_fixture_has_stable_compatible_non_authorizing_readback(self) -> None:
        fixture = _FIXTURE.read_bytes()
        self.assertEqual(sha256(fixture).hexdigest(), _FIXTURE_SHA256)
        source = _source()
        result = assess_parallel_action_graph(fixture, scope=_SCOPE)
        self.assertEqual(result["contract_version"], READBACK_VERSION)
        self.assertEqual(result["producer_contract_version"], "parallel-action-graph/v1")
        self.assertEqual(result["status"], "COMPATIBLE")
        self.assertTrue(result["compatible"])
        self.assertFalse(result["dispatch_authorized"])
        self.assertEqual(result["execution_authority"], "NOT_EVALUATED")
        self.assertEqual(result["admission"], "COMPATIBILITY_ONLY")
        self.assertEqual(result["mission_id"], "MISSION-FIXTURE-1")
        self.assertEqual(result["mission_revision"], 1)
        self.assertEqual(result["producer_snapshot_digest"],
                         "sha256:91058b863010772c3f1db9b5d9b95181aced6c2cd89d061c112e4a7a923faeb9")
        self.assertEqual([action["action_id"] for action in result["actions"]],
                         ["ACTION-A", "ACTION-B", "ACTION-Q"])
        for original, readback in zip(source["actions"], result["actions"], strict=True):
            self.assertEqual(readback["target"], original["target"])
            self.assertEqual(readback["dependencies"], original["dependencies"])
        self.assertEqual([item["predecessor_evidence_state"] for item in result["actions"]],
                         ["NOT_REQUIRED", "NOT_REQUIRED", "UNVERIFIED"])
        self.assertEqual([item["target_baseline_state"] for item in result["actions"]],
                         ["UNVERIFIED", "UNVERIFIED", "UNVERIFIED"])
        self.assertEqual(result["concurrency"], {
            "classification": "TOPOLOGY_ONLY_UNQUALIFIED",
            "independent_repository_action_pairs": [["ACTION-A", "ACTION-B"]],
            "resource_and_capacity_verified": False,
        })
        self.assertEqual(result, assess_parallel_action_graph(fixture, scope=_SCOPE))
        reordered = deepcopy(source)
        reordered["actions"].reverse()
        self.assertEqual(result["producer_snapshot_digest"], _assess(reordered)["producer_snapshot_digest"])

    def test_readback_is_snapshot_copied_and_does_not_invent_edges(self) -> None:
        source = _source()
        original = deepcopy(source)
        first = _assess(source)
        self.assertEqual(source, original)
        first["actions"][2]["dependencies"][0]["predecessor_action_id"] = "tampered"
        second = _assess(source)
        self.assertEqual(second["actions"][2]["dependencies"], original["actions"][2]["dependencies"])
        self.assertEqual(second["producer_snapshot_digest"], _assess(original)["producer_snapshot_digest"])
        self.assertEqual(second["actions"][0]["dependencies"], [])
        self.assertEqual(second["actions"][1]["dependencies"], [])
        changed = deepcopy(source)
        changed["actions"][2]["dependencies"][0]["required_evidence"]["content_digest"] = (
            "sha256:" + "c" * 64
        )
        self.assertNotEqual(second["producer_snapshot_digest"], _assess(changed)["producer_snapshot_digest"])

    def test_independent_same_repository_has_no_concurrency_candidate(self) -> None:
        graph = _source()
        graph["actions"][1]["target"]["repository_id"] = "repository-a"
        graph["actions"][2]["dependencies"][1]["required_evidence"]["repository_id"] = "repository-a"
        result = _assess(graph)
        self.assertTrue(result["compatible"])
        self.assertEqual(result["concurrency"]["independent_repository_action_pairs"], [])
        self.assertEqual(graph["actions"][1]["dependencies"], [])

    def test_transitive_predecessors_are_never_concurrency_candidates(self) -> None:
        graph = _source()
        graph["actions"][1]["dependencies"] = [deepcopy(graph["actions"][2]["dependencies"][0])]
        result = _assess(graph)
        self.assertTrue(result["compatible"])
        self.assertEqual(result["concurrency"]["independent_repository_action_pairs"], [])

    def test_rejected_envelope_and_json_are_typed_and_non_authorizing(self) -> None:
        cases = [
            (b"", "MALFORMED_INPUT"),
            (b"\xff", "MALFORMED_INPUT"),
            (b"{", "MALFORMED_INPUT"),
            (b'{"x":NaN}', "MALFORMED_INPUT"),
            (b'{"contract_version":"parallel-action-graph/v1","contract_version":"parallel-action-graph/v1"}', "MALFORMED_INPUT"),
            (b"[]", "INVALID_ENVELOPE"),
            (b" " * (MAX_DOCUMENT_BYTES + 1), "INPUT_TOO_LARGE"),
        ]
        for payload, code in cases:
            with self.subTest(code=code, payload=payload[:32]):
                self.assert_rejected(assess_parallel_action_graph(payload, scope=_SCOPE), code)
        self.assert_rejected(assess_parallel_action_graph("not bytes", scope=_SCOPE), "MALFORMED_INPUT")
        self.assert_rejected(assess_parallel_action_graph(_FIXTURE.read_bytes(), scope=object()), "INVALID_SCOPE")

    def test_large_valid_forge_dag_fits_and_action_flood_never_reaches_json_decoder(self) -> None:
        action_ids = [f"A{index:03d}" + "x" * 124 for index in range(256)]
        repository = "R" * 128
        actions = [
            {
                "action_id": action_id,
                "target": {
                    "ep_instance_id": "E" * 128,
                    "project_id": "P" * 128,
                    "repository_id": repository,
                    "baseline_revision": "b" * 128,
                },
                "dependencies": [
                    {
                        "predecessor_action_id": action_ids[predecessor],
                        "required_evidence": {
                            "kind": "QUALIFIED_ARTIFACT",
                            "repository_id": repository,
                            "content_digest": "sha256:" + "a" * 64,
                        },
                    }
                    for predecessor in range(index)
                ],
            }
            for index, action_id in enumerate(action_ids)
        ]
        graph = {
            "contract_version": "parallel-action-graph/v1",
            "mission_id": "MISSION-LARGE",
            "mission_revision": 1,
            "actions": actions,
        }
        encoded = json.dumps(graph, separators=(",", ":")).encode()
        self.assertGreater(len(encoded), 8 * 1024 * 1024)
        self.assertLess(len(encoded), MAX_DOCUMENT_BYTES)
        scope = CompatibilityScope("E" * 128, "P" * 128, (repository,))
        result = assess_parallel_action_graph(encoded, scope=scope)
        self.assertEqual(result["status"], "COMPATIBLE")
        self.assertEqual(len(result["actions"]), 256)
        self.assertFalse(result["dispatch_authorized"])

        flood = (b'{"contract_version":"parallel-action-graph/v1","mission_id":"M",'
                 b'"mission_revision":1,"actions":[' + b'[],' * 1_999_999 + b'[]]}')
        with patch("engineering_platform.parallel_action_compat.json.loads",
                   side_effect=AssertionError("invalid flood was decoded")):
            self.assert_rejected(
                assess_parallel_action_graph(flood, scope=_SCOPE), "STRUCTURE_LIMIT_EXCEEDED"
            )

    def test_unsupported_version_and_unknown_authority_fields_fail_closed(self) -> None:
        for version in ["parallel-action-graph/v2", None, 1]:
            with self.subTest(version=version):
                graph = _source()
                graph["contract_version"] = version
                self.assert_rejected(_assess(graph), "UNSUPPORTED_VERSION")
        graph = _source()
        graph["dispatch_authorized"] = True
        self.assert_rejected(_assess(graph), "INVALID_ENVELOPE")
        graph = _source()
        graph["concurrency"] = {"max_workers": 2}
        self.assert_rejected(_assess(graph), "INVALID_ENVELOPE")

    def test_invalid_envelope_and_actions_fail_closed(self) -> None:
        cases = []
        graph = _source(); graph["mission_id"] = "../foreign"; cases.append((graph, "INVALID_ENVELOPE"))
        graph = _source(); graph["mission_revision"] = True; cases.append((graph, "INVALID_ENVELOPE"))
        graph = _source(); graph["mission_revision"] = 0; cases.append((graph, "INVALID_ENVELOPE"))
        graph = _source(); graph["actions"] = []; cases.append((graph, "INVALID_ENVELOPE"))
        graph = _source(); graph["actions"] = [graph["actions"][0]] * 257; cases.append((graph, "INVALID_ENVELOPE"))
        graph = _source(); graph["actions"][0]["unknown"] = 1; cases.append((graph, "INVALID_ACTION"))
        graph = _source(); graph["actions"][0]["action_id"] = "bad/id"; cases.append((graph, "INVALID_ACTION"))
        graph = _source(); graph["actions"][1]["action_id"] = "ACTION-A"; cases.append((graph, "INVALID_ACTION"))
        for value, code in cases:
            with self.subTest(code=code):
                self.assert_rejected(_assess(value), code)

    def test_target_scope_and_foreign_repository_fail_closed(self) -> None:
        for field, replacement, code in [
            ("ep_instance_id", "ep-foreign", "TARGET_SCOPE_MISMATCH"),
            ("project_id", "project-foreign", "TARGET_SCOPE_MISMATCH"),
            ("repository_id", "repository-foreign", "FOREIGN_REPOSITORY"),
            ("repository_id", "../foreign", "INVALID_TARGET"),
            ("baseline_revision", "../foreign", "INVALID_TARGET"),
        ]:
            with self.subTest(field=field, replacement=replacement):
                graph = _source()
                graph["actions"][0]["target"][field] = replacement
                self.assert_rejected(_assess(graph), code)
        graph = _source()
        graph["actions"][0]["target"]["unknown"] = "x"
        self.assert_rejected(_assess(graph), "INVALID_TARGET")

    def test_dependency_evidence_and_cycle_fail_closed(self) -> None:
        cases = []
        graph = _source(); graph["actions"][2]["dependencies"] = "ACTION-A"; cases.append((graph, "INVALID_DEPENDENCY"))
        graph = _source(); graph["actions"][2]["dependencies"][0]["unknown"] = 1; cases.append((graph, "INVALID_DEPENDENCY"))
        graph = _source(); graph["actions"][2]["dependencies"].append(deepcopy(graph["actions"][2]["dependencies"][0])); cases.append((graph, "INVALID_DEPENDENCY"))
        graph = _source(); graph["actions"][2]["dependencies"][0]["predecessor_action_id"] = "ACTION-MISSING"; cases.append((graph, "MISSING_PREDECESSOR"))
        graph = _source(); graph["actions"][2]["dependencies"][0]["predecessor_action_id"] = "ACTION-Q"; cases.append((graph, "CYCLE"))
        graph = _source(); graph["actions"][0]["dependencies"] = [deepcopy(graph["actions"][2]["dependencies"][0])]; graph["actions"][0]["dependencies"][0]["predecessor_action_id"] = "ACTION-Q"; cases.append((graph, "CYCLE"))
        graph = _source(); graph["actions"][2]["dependencies"][0]["required_evidence"]["repository_id"] = "repository-b"; cases.append((graph, "EVIDENCE_TARGET_MISMATCH"))
        graph = _source(); graph["actions"][2]["dependencies"][0]["required_evidence"]["kind"] = "UNQUALIFIED"; cases.append((graph, "INVALID_EVIDENCE"))
        graph = _source(); graph["actions"][2]["dependencies"][0]["required_evidence"]["content_digest"] = "sha256:bad"; cases.append((graph, "INVALID_EVIDENCE"))
        graph = _source(); graph["actions"][2]["dependencies"][0]["required_evidence"]["repository_id"] = "../foreign"; cases.append((graph, "INVALID_EVIDENCE"))
        graph = _source(); graph["actions"][2]["dependencies"][0]["required_evidence"]["unknown"] = 1; cases.append((graph, "INVALID_EVIDENCE"))
        for value, code in cases:
            with self.subTest(code=code, value=value):
                self.assert_rejected(_assess(value), code)

    def test_compatibility_is_pure_even_with_execution_apis_disabled(self) -> None:
        payload = _FIXTURE.read_bytes()
        with (patch("builtins.open", side_effect=AssertionError("filesystem")),
              patch("sqlite3.connect", side_effect=AssertionError("database")),
              patch("subprocess.Popen", side_effect=AssertionError("provider")),
              patch("urllib.request.urlopen", side_effect=AssertionError("network")),
              patch("os.chdir", side_effect=AssertionError("execution"))):
            result = assess_parallel_action_graph(payload, scope=_SCOPE)
        self.assertTrue(result["compatible"])
        self.assertFalse(result["dispatch_authorized"])

    def test_scope_validation_and_second_supported_evidence_kind(self) -> None:
        for repositories in [(), ("repository-a", "repository-a"), ("../bad",)]:
            with self.subTest(repositories=repositories), self.assertRaises(CompatibilityError):
                CompatibilityScope("ep-fixture-1", "project-fixture-1", repositories)
        with self.assertRaises(CompatibilityError):
            CompatibilityScope("../bad", "project-fixture-1", ("repository-a",))
        graph = _source()
        graph["actions"][2]["dependencies"][0]["required_evidence"]["kind"] = "REPOSITORY_REVISION"
        self.assertTrue(_assess(graph)["compatible"])

    def assert_rejected(self, result: dict, code: str) -> None:
        self.assertEqual(result["contract_version"], READBACK_VERSION)
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse(result["compatible"])
        self.assertFalse(result["dispatch_authorized"])
        self.assertEqual(result["execution_authority"], "NOT_EVALUATED")
        self.assertEqual(result["admission"], "DENIED")
        self.assertEqual(result["error"]["code"], code)
        self.assertNotIn("actions", result)
        self.assertNotIn("producer_snapshot_digest", result)


if __name__ == "__main__":
    unittest.main()
