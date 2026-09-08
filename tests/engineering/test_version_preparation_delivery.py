from __future__ import annotations

from pathlib import Path
import unittest
import tempfile
import hashlib

from engineering_platform.version_preparation_delivery import VersionPreparationDelivery, VersionPreparationError, VersionPreparationRequest
from engineering_platform.execution_models import PullRequestEvidence


def request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "contract_version": "1", "operation_id": "operation-0001", "product_id": "forge",
        "component_id": "product", "repository_id": "pcvantol/forge", "policy_revision": "v1",
        "policy_digest": "sha256:policy", "source_event_set": ["merge:1"], "source_event_policy": "main",
        "expected_source_revision": "a" * 40, "expected_target_branch_revision": None,
        "expected_version": "2.3.0", "requested_change": "minor", "determined_target_version": "2.4.0",
        "allowed_projection_paths": ["product-version.json"], "prepared_operation_digest": "sha256:diff",
        "authorization_reference": "grant:bounded", "delivery_mode": "PROTECTED_VERSION_PREPARATION_CANDIDATE",
    }
    value.update(overrides)
    return value


class VersionPreparationRequestTest(unittest.TestCase):
    def test_accepts_exact_bounded_contract(self) -> None:
        parsed = VersionPreparationRequest.parse(request())
        self.assertEqual(parsed.operation_id, "operation-0001")
        self.assertEqual(parsed.allowed_projection_paths, ("product-version.json",))

    def test_rejects_unknown_and_untrusted_paths(self) -> None:
        with self.assertRaisesRegex(VersionPreparationError, "unknown"):
            VersionPreparationRequest.parse({**request(), "shell": "rm"})
        with self.assertRaisesRegex(VersionPreparationError, "paths"):
            VersionPreparationRequest.parse(request(allowed_projection_paths=["../outside"]))

    def test_rejects_duplicate_events_and_wrong_source_sha(self) -> None:
        with self.assertRaisesRegex(VersionPreparationError, "event"):
            VersionPreparationRequest.parse(request(source_event_set=["merge:1", "merge:1"]))
        with self.assertRaisesRegex(VersionPreparationError, "exact SHA"):
            VersionPreparationRequest.parse(request(expected_source_revision="main"))

    def test_candidate_branch_is_deterministically_bound_to_operation(self) -> None:
        parsed = VersionPreparationRequest.parse(request())
        self.assertEqual(VersionPreparationDelivery.branch_name(parsed), "ep/version-preparation/operation-0001")

    def test_qualification_cannot_substitute_an_old_head(self) -> None:
        class GitHub:
            def qualification_for_exact_head(self, number: int, sha: str) -> dict[str, object]:
                return {"pull_request_id": number, "exact_qualified_sha": "b" * 40, "conclusion": "PASS"}
        with self.assertRaisesRegex(VersionPreparationError, "exact candidate SHA"):
            VersionPreparationDelivery.qualify_candidate({"candidate_commit_sha": "a" * 40, "pull_request_id": 9}, GitHub())

    def test_delivery_evidence_is_idempotent_and_exact_head_bound(self) -> None:
        candidate = {"operation_id": "operation-0001", "candidate_commit_sha": "a" * 40, "branch": "ep/version-preparation/operation-0001", "pull_request_id": 9}
        qualification = {"exact_qualified_sha": "a" * 40, "conclusion": "PASS", "checks": []}
        with tempfile.TemporaryDirectory() as directory:
            first = VersionPreparationDelivery.record_delivery_evidence(Path(directory), candidate, qualification)
            self.assertEqual(first, VersionPreparationDelivery.record_delivery_evidence(Path(directory), candidate, qualification))
            with self.assertRaisesRegex(VersionPreparationError, "exact successful"):
                VersionPreparationDelivery.record_delivery_evidence(Path(directory), candidate, {**qualification, "exact_qualified_sha": "b" * 40})

    def test_execute_binds_prepare_candidate_qualification_and_evidence(self) -> None:
        class Git:
            def command(self, _root: Path, *args: str) -> str:
                if args[-1] == "HEAD": return "a" * 40
                if args[-1] == "--untracked-files=all": return ""
                if args[-2:] == ("diff", "--name-only"): return "product-version.json\n.version-operations/operation-0001.json"
                if args[-2:] == ("branch", "--show-current"): return "ep/version-preparation/operation-0001"
                return ""
        class Helper:
            def apply(self, _worktree: Path, _request: object) -> None: pass
        class GitHub:
            def create_or_recover_pull_request(self, branch: str, base: str, title: str, body: str) -> PullRequestEvidence:
                return PullRequestEvidence(9, "OPEN", True, True, head_branch=branch, base_branch=base)
            def qualification_for_exact_head(self, number: int, sha: str) -> dict[str, object]:
                return {"pull_request_id": number, "exact_qualified_sha": sha, "conclusion": "PASS", "checks": []}
        digest = hashlib.sha256(b"product-version.json\n.version-operations/operation-0001.json").hexdigest()
        parsed = VersionPreparationRequest.parse(request(prepared_operation_digest=digest))
        with tempfile.TemporaryDirectory() as directory:
            result = VersionPreparationDelivery(Git(), Helper()).execute(Path(directory), Path(directory), parsed, GitHub(), base_branch="main", evidence_root=Path(directory))
            self.assertEqual(result["pull_request_id"], 9)
            self.assertTrue(Path(str(result["delivery_evidence_path"])).is_file())


if __name__ == "__main__":
    unittest.main()
