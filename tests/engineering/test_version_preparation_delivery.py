from __future__ import annotations

from pathlib import Path
import unittest
import tempfile
import hashlib
import subprocess

from engineering_platform.version_preparation_delivery import ProductHelperDeclaration, VersionPreparationDelivery, VersionPreparationError, VersionPreparationRequest
from engineering_platform.execution_models import PullRequestEvidence


def request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "contract_version": "1", "operation_id": "operation-0001", "product_id": "forge",
        "component_id": "product", "repository_id": "pcvantol/forge", "policy_revision": "v1",
        "policy_digest": "sha256:" + "a" * 64, "source_event_set": ["increment:I-123"], "source_event_policy": "engineering-increment", "release_class": "MINOR", "release_rationale": "capability boundary",
        "expected_source_revision": "a" * 40, "expected_target_branch_revision": None,
        "expected_version": "2.3.0", "requested_change": "minor", "determined_target_version": "2.4.0",
        "allowed_projection_paths": ["product-version.json"], "prepared_operation_digest": "sha256:" + "b" * 64,
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
        with self.assertRaisesRegex(VersionPreparationError, "target branch revision"):
            VersionPreparationRequest.parse(request(expected_target_branch_revision="main"))

    def test_rejects_noncanonical_versions_and_digests(self) -> None:
        with self.assertRaisesRegex(VersionPreparationError, "stable SemVer"):
            VersionPreparationRequest.parse(request(expected_version="02.3.0"))
        with self.assertRaisesRegex(VersionPreparationError, "stable SemVer"):
            VersionPreparationRequest.parse(request(determined_target_version="2.3.00"))
        with self.assertRaisesRegex(VersionPreparationError, "SHA-256"):
            VersionPreparationRequest.parse(request(policy_digest="sha256:policy"))
        with self.assertRaisesRegex(VersionPreparationError, "SHA-256"):
            VersionPreparationRequest.parse(request(prepared_operation_digest="sha256:diff"))

    def test_rejects_a_release_classification_that_disagrees_with_the_operation(self) -> None:
        with self.assertRaisesRegex(VersionPreparationError, "classification"):
            VersionPreparationRequest.parse(request(release_class="PATCH"))

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
        candidate = {"operation_id": "operation-0001", "prepared_operation_digest": "sha256:" + "b" * 64, "candidate_commit_sha": "a" * 40, "candidate_tree_sha": "c" * 40, "branch": "ep/version-preparation/operation-0001", "pull_request_id": 9, "pull_request_head_sha": "a" * 40, "authorization_reference": "grant:bounded"}
        qualification = {"exact_qualified_sha": "a" * 40, "conclusion": "PASS", "checks": []}
        with tempfile.TemporaryDirectory() as directory:
            first = VersionPreparationDelivery.record_delivery_evidence(Path(directory), candidate, qualification)
            self.assertEqual(first, VersionPreparationDelivery.record_delivery_evidence(Path(directory), candidate, qualification))
            recorded = __import__("json").loads(first.read_text(encoding="utf-8"))
            self.assertEqual(recorded["candidate_tree_sha"], "c" * 40)
            self.assertEqual(recorded["delivery"]["state"], "PENDING_PROTECTED_MERGE")
            with self.assertRaisesRegex(VersionPreparationError, "exact successful"):
                VersionPreparationDelivery.record_delivery_evidence(Path(directory), candidate, {**qualification, "exact_qualified_sha": "b" * 40})

    def test_execute_binds_prepare_candidate_qualification_and_evidence(self) -> None:
        class Git:
            def __init__(self) -> None: self.status_calls = 0
            def command(self, _root: Path, *args: str) -> str:
                if args[-2:] == ("-z", "HEAD"):
                    self.status_calls += 1
                    return "" if self.status_calls == 1 else "product-version.json\0"
                if args[-1] == "-z": return "" if self.status_calls == 1 else ".version-operations/operation-0001.json\0"
                if args[-1] == "HEAD^{tree}": return "c" * 40
                if args[-1] == "HEAD": return "a" * 40
                if args[-1] == "--untracked-files=all": return ""
                if args[-2:] == ("branch", "--show-current"): return "ep/version-preparation/operation-0001"
                return ""
        class Helper:
            def apply(self, worktree: Path, operation: VersionPreparationRequest) -> None:
                receipt = worktree / ".version-operations"
                receipt.mkdir(exist_ok=True)
                (receipt / "operation-0001.json").write_text(__import__("json").dumps({"schema_version": 1, "operation_id": operation.operation_id, "product": operation.product_id, "policy_revision": operation.policy_revision, "expected_source_revision": operation.expected_source_revision, "allowed_projection_paths": list(operation.allowed_projection_paths)}), encoding="utf-8")
        class GitHub:
            def version_preparation_writer(self) -> dict[str, object]:
                return {"actor": "ep-writer", "repository_id": "pcvantol/forge", "can_push": True}
            def create_or_recover_pull_request(self, branch: str, base: str, title: str, body: str) -> PullRequestEvidence:
                return PullRequestEvidence(9, "OPEN", True, True, head_branch=branch, base_branch=base, head_sha="a" * 40)
            def qualification_for_exact_head(self, number: int, sha: str) -> dict[str, object]:
                return {"pull_request_id": number, "exact_qualified_sha": sha, "conclusion": "PASS", "checks": []}
        digest = "sha256:" + hashlib.sha256(b".version-operations/operation-0001.json\nproduct-version.json").hexdigest()
        parsed = VersionPreparationRequest.parse(request(prepared_operation_digest=digest))
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".version-preparation.json").write_text(__import__("json").dumps({"contract_version": "1", "product_id": "forge", "repository_id": "pcvantol/forge", "helper_path": "scripts/advance_product_version.py", "receipt_directory": ".version-operations", "allowed_projection_paths": ["product-version.json"], "policy_revision": "v1"}), encoding="utf-8")
            result = VersionPreparationDelivery(Git(), Helper()).execute(Path(directory), Path(directory), parsed, GitHub(), base_branch="main", evidence_root=Path(directory))
            self.assertEqual(result["pull_request_id"], 9)
            self.assertTrue(Path(str(result["delivery_evidence_path"])).is_file())

    def test_publish_rejects_a_pull_request_head_that_raced_the_candidate_push(self) -> None:
        class Git:
            def command(self, _root: Path, *args: str) -> str:
                if args[-2:] == ("branch", "--show-current"): return "ep/version-preparation/operation-0001"
                if args[-1] == "HEAD^{tree}": return "c" * 40
                if args[-1] == "HEAD": return "a" * 40
                return ""
        class GitHub:
            def version_preparation_writer(self) -> dict[str, object]:
                return {"actor": "ep-writer", "repository_id": "pcvantol/forge", "can_push": True}
            def create_or_recover_pull_request(self, branch: str, base: str, title: str, body: str) -> PullRequestEvidence:
                return PullRequestEvidence(9, "OPEN", True, True, head_branch=branch, base_branch=base, head_sha="b" * 40)
        parsed = VersionPreparationRequest.parse(request())
        prepared = {"operation_id": parsed.operation_id, "changed_paths": ("product-version.json",), "prepared_operation_digest": parsed.prepared_operation_digest}
        with self.assertRaisesRegex(VersionPreparationError, "head changed"):
            VersionPreparationDelivery(Git(), object()).publish_candidate(Path("."), parsed, prepared, GitHub(), base_branch="main")

    def test_publish_rejects_a_writer_without_exact_repository_push_scope(self) -> None:
        class Git:
            def command(self, _root: Path, *args: str) -> str:
                if args[-2:] == ("branch", "--show-current"): return "ep/version-preparation/operation-0001"
                raise AssertionError(f"candidate must stop before {args!r}")
        class GitHub:
            def version_preparation_writer(self) -> dict[str, object]:
                return {"actor": "ep-writer", "repository_id": "pcvantol/other", "can_push": True}
        parsed = VersionPreparationRequest.parse(request())
        prepared = {"operation_id": parsed.operation_id, "changed_paths": ("product-version.json",), "prepared_operation_digest": parsed.prepared_operation_digest}
        with self.assertRaisesRegex(VersionPreparationError, "not authorized"):
            VersionPreparationDelivery(Git(), object()).publish_candidate(Path("."), parsed, prepared, GitHub(), base_branch="main")

    def test_publish_rejects_a_target_branch_that_moved_after_admission(self) -> None:
        class Git:
            def command(self, _root: Path, *args: str) -> str:
                if args[-2:] == ("branch", "--show-current"): return "ep/version-preparation/operation-0001"
                if args[-1] == "origin/main": return "b" * 40
                raise AssertionError(f"candidate must stop before {args!r}")
        class GitHub:
            def version_preparation_writer(self) -> dict[str, object]:
                return {"actor": "ep-writer", "repository_id": "pcvantol/forge", "can_push": True}
        parsed = VersionPreparationRequest.parse(request(expected_target_branch_revision="a" * 40))
        prepared = {"operation_id": parsed.operation_id, "changed_paths": ("product-version.json",), "prepared_operation_digest": parsed.prepared_operation_digest}
        with self.assertRaisesRegex(VersionPreparationError, "target branch revision changed"):
            VersionPreparationDelivery(Git(), object()).publish_candidate(Path("."), parsed, prepared, GitHub(), base_branch="main")

    def test_existing_worktree_path_is_rejected_before_git_write(self) -> None:
        class Git:
            def command(self, *_args: str) -> str: raise AssertionError("must not invoke Git")
        with tempfile.TemporaryDirectory() as directory:
            parsed = VersionPreparationRequest.parse(request())
            with self.assertRaisesRegex(VersionPreparationError, "already exists"):
                VersionPreparationDelivery(Git(), object()).create_isolated_worktree(Path(directory), Path(directory), parsed)

    def test_receipt_must_be_in_the_declared_directory(self) -> None:
        class Git:
            def __init__(self) -> None: self.status_calls = 0
            def command(self, _root: Path, *args: str) -> str:
                if args[-2:] == ("-z", "HEAD"):
                    self.status_calls += 1
                    return "" if self.status_calls == 1 else "product-version.json\0"
                if args[-1] == "-z": return "" if self.status_calls == 1 else "other/operation-0001.json\0"
                if args[-1] == "HEAD": return "a" * 40
                if args[-1] == "--untracked-files=all": return ""
                return ""
        class Helper:
            def apply(self, _worktree: Path, _request: object) -> None: pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".version-preparation.json").write_text(__import__("json").dumps({"contract_version": "1", "product_id": "forge", "repository_id": "pcvantol/forge", "helper_path": "scripts/advance_product_version.py", "receipt_directory": ".version-operations", "allowed_projection_paths": ["product-version.json"], "policy_revision": "v1"}), encoding="utf-8")
            digest = "sha256:" + hashlib.sha256(b"other/operation-0001.json\nproduct-version.json").hexdigest()
            with self.assertRaisesRegex(VersionPreparationError, "declared operation"):
                VersionPreparationDelivery(Git(), Helper()).prepare(root, root, VersionPreparationRequest.parse(request(prepared_operation_digest=digest)))

    def test_prepared_receipt_must_bind_the_admitted_product_operation(self) -> None:
        declaration = ProductHelperDeclaration("forge", "pcvantol/forge", "scripts/advance_product_version.py", ".version-operations", ("product-version.json",), "v1")
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory, "operation-0001.json")
            receipt.write_text(__import__("json").dumps({"schema_version": 1, "operation_id": "operation-0001", "product": "workspace", "policy_revision": "v1", "expected_source_revision": "a" * 40, "allowed_projection_paths": ["product-version.json"]}), encoding="utf-8")
            with self.assertRaisesRegex(VersionPreparationError, "does not bind"):
                VersionPreparationDelivery._validate_prepared_receipt(receipt, declaration, VersionPreparationRequest.parse(request()))

    def test_isolated_git_worktree_includes_an_untracked_receipt_in_candidate_scope(self) -> None:
        """A real temporary Git checkout proves receipts cannot be omitted by diff."""
        from engineering_platform.providers import GitProvider

        class Helper:
            def apply(self, worktree: Path, _request: object) -> None:
                (worktree / "product-version.json").write_text('{"version":"2.4.0"}\n', encoding="utf-8")
                receipt = worktree / ".version-operations"
                receipt.mkdir()
                (receipt / "operation-0001.json").write_text(__import__("json").dumps({"schema_version": 1, "operation_id": "operation-0001", "product": "forge", "policy_revision": "v1", "expected_source_revision": sha, "allowed_projection_paths": ["product-version.json"]}), encoding="utf-8")

        def git(root: Path, *args: str) -> None:
            subprocess.run(("git", *args), cwd=root, check=True, text=True, capture_output=True)

        declaration = {"contract_version": "1", "product_id": "forge", "repository_id": "pcvantol/forge", "helper_path": "scripts/advance_product_version.py", "receipt_directory": ".version-operations", "allowed_projection_paths": ["product-version.json"], "policy_revision": "v1"}
        digest = "sha256:" + hashlib.sha256(b".version-operations/operation-0001.json\nproduct-version.json").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root, candidate = Path(directory, "source"), Path(directory, "candidate")
            root.mkdir()
            git(root, "init", "-q")
            git(root, "config", "user.email", "test@example.invalid")
            git(root, "config", "user.name", "Version Preparation Test")
            (root / "product-version.json").write_text('{"version":"2.3.0"}\n', encoding="utf-8")
            (root / ".version-preparation.json").write_text(__import__("json").dumps(declaration), encoding="utf-8")
            git(root, "add", "product-version.json", ".version-preparation.json")
            git(root, "commit", "-qm", "baseline")
            sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=root, check=True, text=True, capture_output=True).stdout.strip()
            parsed = VersionPreparationRequest.parse(request(expected_source_revision=sha, prepared_operation_digest=digest))
            delivery = VersionPreparationDelivery(GitProvider(), Helper())
            prepared = delivery.prepare_in_isolated_worktree(root, candidate, parsed)
            self.assertEqual(prepared["changed_paths"], (".version-operations/operation-0001.json", "product-version.json"))
            self.assertEqual(GitProvider().command(candidate, "git", "rev-parse", "HEAD"), sha)

            class FailingHelper:
                def apply(self, worktree: Path, operation: VersionPreparationRequest) -> None:
                    (worktree / "product-version.json").write_text('{"version":"2.4.0"}\n', encoding="utf-8")
                    (worktree / ".version-operations").mkdir()
                    (worktree / ".version-operations" / f"{operation.operation_id}.json").write_text("not-json", encoding="utf-8")

            failed = Path(directory, "failed-candidate")
            failed_request = VersionPreparationRequest.parse(request(operation_id="operation-0002", expected_source_revision=sha, prepared_operation_digest=digest))
            with self.assertRaisesRegex(VersionPreparationError, "receipt is unreadable"):
                VersionPreparationDelivery(GitProvider(), FailingHelper()).prepare_in_isolated_worktree(root, failed, failed_request)
            self.assertEqual(GitProvider().command(root, "git", "rev-parse", "HEAD"), sha)
            self.assertEqual(GitProvider().command(failed, "git", "rev-parse", "HEAD"), sha)
            self.assertTrue(GitProvider().command(failed, "git", "status", "--porcelain"))


if __name__ == "__main__":
    unittest.main()
