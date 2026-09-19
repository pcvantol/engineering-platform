"""Exact PR-diff authority for independent delivery assurance."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from engineering_platform.assurance_scope import observed_delivery_scope


class AssuranceScopeTest(unittest.TestCase):
    def git(self, root: Path, *args: str) -> str:
        return subprocess.run(("git", "-C", str(root), *args), check=True,
                              capture_output=True, text=True).stdout.strip()

    def fixture(self, temporary: str) -> Path:
        root = Path(temporary)
        self.git(root, "init", "-q", "--initial-branch=main")
        self.git(root, "config", "user.name", "Assurance Fixture")
        self.git(root, "config", "user.email", "assurance@example.invalid")
        (root / "README.md").write_text("# Main\n", encoding="utf-8")
        self.git(root, "add", "README.md")
        self.git(root, "commit", "-qm", "Main")
        self.git(root, "checkout", "-qb", "codex/finalize")
        return root

    def commit(self, root: Path, path: str, content: str) -> str:
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        self.git(root, "add", path)
        self.git(root, "commit", "-qm", "Candidate")
        return self.git(root, "rev-parse", "HEAD")

    def test_finalization_diff_allows_owned_records_without_implementation_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            self.commit(root, "HANDOFF.md", "# Pending handoff\n")
            head = self.commit(root, "docs/engineering/runs/2026/run.md",
                               "# Handoff\n- Prepared: `2026-09-19`\n- Repository State: `FINALIZATION_PR_OPEN`\n")
            scope = observed_delivery_scope(root, role="FINALIZATION", candidate_sha=head)
            self.assertEqual(scope["candidate_sha"], head)
            self.assertEqual(scope["changed_paths"], ("HANDOFF.md", "docs/engineering/runs/2026/run.md"))
            self.assertEqual(scope["out_of_scope_paths"], ())
            self.assertEqual(scope["premature_completion_paths"], ())
            self.assertNotIn("mission_parser/cli.py", scope["changed_paths"])

    def test_finalization_qualification_proof_and_three_run_records_are_exactly_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            self.commit(root, ".engineering-platform/managed-github-e2e-finalization-proof.json",
                        '{"kind":"EP_MANAGED_GITHUB_E2E","stage":"FINALIZATION","version":1}\n')
            self.commit(root, "docs/engineering/runs/2026/run.md", "# Prepared handoff\n")
            self.commit(root, "docs/engineering/runs/index.json", '{"runs":[]}\n')
            head = self.commit(root, "docs/engineering/runs/latest.md", "# Pending finalization\n")
            scope = observed_delivery_scope(root, role="FINALIZATION", candidate_sha=head)
            self.assertEqual(scope["changed_paths"], (
                ".engineering-platform/managed-github-e2e-finalization-proof.json",
                "docs/engineering/runs/2026/run.md",
                "docs/engineering/runs/index.json",
                "docs/engineering/runs/latest.md",
            ))
            self.assertEqual(scope["out_of_scope_paths"], ())
            self.assertEqual(scope["premature_completion_paths"], ())

    def test_finalization_rejects_unrelated_or_disguised_qualification_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            self.commit(root, ".engineering-platform/managed-github-e2e-proof.json", "{}\n")
            head = self.commit(root, ".engineering-platform/other-finalization-proof.json", "{}\n")
            scope = observed_delivery_scope(root, role="FINALIZATION", candidate_sha=head)
            self.assertEqual(scope["out_of_scope_paths"], (
                ".engineering-platform/managed-github-e2e-proof.json",
                ".engineering-platform/other-finalization-proof.json",
            ))

    def test_finalization_rejects_code_path_and_anticipatory_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            self.commit(root, "mission_parser/cli.py", "def parse(): pass\n")
            head = self.commit(root, "docs/engineering/runs/latest.md",
                               "# Latest\n- Completed: `2026-09-19`\n- Repository State: `MERGED_RECONCILED`\n")
            scope = observed_delivery_scope(root, role="FINALIZATION", candidate_sha=head)
            self.assertEqual(scope["out_of_scope_paths"], ("mission_parser/cli.py",))
            self.assertEqual(scope["premature_completion_paths"], ("docs/engineering/runs/latest.md",))

    def test_rolling_record_cannot_claim_completion_before_finalization_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            head = self.commit(root, "HANDOFF.md", "# Proposed handoff\n- Workspace State: `WORKSPACE_READY`\n")
            scope = observed_delivery_scope(root, role="FINALIZATION", candidate_sha=head)
            self.assertEqual(scope["out_of_scope_paths"], ())
            self.assertEqual(scope["premature_completion_paths"], ("HANDOFF.md",))

    def test_reconciliation_only_accepts_four_rolling_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            head = self.commit(root, "docs/engineering/runs/latest.md", "# Too broad\n")
            scope = observed_delivery_scope(root, role="RECONCILIATION", candidate_sha=head)
            self.assertEqual(scope["out_of_scope_paths"], ("docs/engineering/runs/latest.md",))

    def test_finalization_cannot_rename_implementation_code_into_allowed_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            self.git(root, "checkout", "main")
            original = root / "mission_parser/cli.py"
            original.parent.mkdir()
            original.write_text("def parse(): return 1\n", encoding="utf-8")
            self.git(root, "add", "mission_parser/cli.py")
            self.git(root, "commit", "-qm", "Implementation")
            self.git(root, "checkout", "codex/finalize")
            self.git(root, "rebase", "main")
            self.git(root, "mv", "mission_parser/cli.py", "HANDOFF.md")
            self.git(root, "commit", "-qm", "Disguised rename")
            scope = observed_delivery_scope(root, role="FINALIZATION", candidate_sha=self.git(root, "rev-parse", "HEAD"))
            self.assertIn("mission_parser/cli.py", scope["out_of_scope_paths"])
            self.assertIn("HANDOFF.md", scope["changed_paths"])

    def test_candidate_identity_must_match_observed_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            self.commit(root, "HANDOFF.md", "# Pending\n")
            with self.assertRaisesRegex(ValueError, "identity changed"):
                observed_delivery_scope(root, role="FINALIZATION", candidate_sha="f" * 40)
            with self.assertRaisesRegex(ValueError, "unknown assurance delivery role"):
                observed_delivery_scope(root, role="UNKNOWN", candidate_sha=self.git(root, "rev-parse", "HEAD"))
