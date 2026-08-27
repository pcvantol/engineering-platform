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


if __name__ == "__main__":
    unittest.main()
