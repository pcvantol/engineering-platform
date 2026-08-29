"""Safety contract tests for one-shot human pull-request repairs."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering import pr_check_repair


class PullRequestCheckRepairTest(unittest.TestCase):
    @patch("tools.engineering.pr_check_repair.GitHubProvider")
    @patch("tools.engineering.pr_check_repair.GitProvider")
    def test_current_evidence_accepts_only_terminal_same_repository_failures(
        self, git_provider: object, github_provider: object
    ) -> None:
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.return_value = completed(
            ("git",), 0, "git@github.com:pcvantol/djconnect.git\n", ""
        )
        github_provider.return_value.github.return_value = json.dumps({
            "number": 971, "state": "OPEN", "isDraft": False,
            "headRefOid": "a" * 40, "headRefName": "feature/human-pr",
            "headRepository": {"nameWithOwner": "pcvantol/djconnect"},
            "statusCheckRollup": [
                {"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
        })
        with tempfile.TemporaryDirectory() as directory:
            evidence = pr_check_repair.current_evidence(Path(directory), 971)
        self.assertTrue(evidence["eligible"])
        self.assertEqual(evidence["failed_checks"], ["tests"])

    @patch("tools.engineering.pr_check_repair.GitHubProvider")
    @patch("tools.engineering.pr_check_repair.GitProvider")
    def test_current_evidence_rejects_a_fork_or_an_already_attempted_sha(
        self, git_provider: object, github_provider: object
    ) -> None:
        completed = __import__("subprocess").CompletedProcess
        git_provider.return_value.execute.return_value = completed(
            ("git",), 0, "git@github.com:pcvantol/djconnect.git\n", ""
        )
        payload = {
            "number": 972, "state": "OPEN", "isDraft": False,
            "headRefOid": "b" * 40, "headRefName": "feature/human-pr",
            "headRepository": {"nameWithOwner": "other/djconnect"},
            "statusCheckRollup": [{"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}],
        }
        github_provider.return_value.github.return_value = json.dumps(payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(pr_check_repair.current_evidence(root, 972)["eligible"])
            payload["headRepository"] = {"nameWithOwner": "pcvantol/djconnect"}
            github_provider.return_value.github.return_value = json.dumps(payload)
            pr_check_repair._write_state(root, 972, "b" * 40, {"status": "SUBMITTED"})
            self.assertFalse(pr_check_repair.current_evidence(root, 972)["eligible"])

    def test_repair_state_follows_the_commit_created_by_the_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pr_check_repair._write_state(root, 973, "a" * 40, {
                "status": "SUBMITTED", "commit_sha": "b" * 40,
            })
            self.assertEqual(pr_check_repair.repair_state(root, 973, "a" * 40), "SUBMITTED")
            self.assertEqual(pr_check_repair.repair_state(root, 973, "b" * 40), "SUBMITTED")

    def test_new_playwright_worktree_installs_locked_node_tooling(self) -> None:
        completed = __import__("subprocess").CompletedProcess
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / "package-lock.json").write_text("{}", encoding="utf-8")
            (worktree / "playwright.config.mjs").write_text("export default {};", encoding="utf-8")
            (worktree / "node_modules" / "@playwright" / "test").mkdir(parents=True)
            with patch("tools.engineering.pr_check_repair.subprocess.run", return_value=completed(("npm", "ci"), 0, "", "")) as run:
                pr_check_repair._prepare_worktree_tooling(worktree)
            run.assert_called_once_with(("npm", "ci"), cwd=worktree, check=False, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
