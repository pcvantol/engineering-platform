"""Unit coverage for the explicit, local-only qualification provider seam."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from engineering_platform.qualification_runtime import (
    DeterministicQualificationAgent,
    LocalQualificationGitHub,
)


class DeterministicQualificationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(("git", "init", "-b", "main", str(self.root)), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(self.root), "config", "user.email", "qualification@example.invalid"), check=True)
        subprocess.run(("git", "-C", str(self.root), "config", "user.name", "Qualification"), check=True)
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.root), "add", "README.md"), check=True)
        subprocess.run(("git", "-C", str(self.root), "commit", "-m", "fixture"), check=True, capture_output=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_local_genesis_managed_and_reconciliation_results_are_deterministic(self) -> None:
        agent = DeterministicQualificationAgent()
        process = []
        agent.set_process_callback(process.append)

        managed = agent.invoke(self.root, "implement the approved work")
        reconciled = agent.invoke(self.root, "the sole automatic post-finalization reconciliation")
        genesis = agent.invoke(self.root, f"Execution mode: Genesis\nTarget repository: {self.root}")

        self.assertEqual(process[0]["pid"], os.getpid())
        self.assertEqual(managed.branch, "qualification-managed")
        self.assertEqual(managed.pull_request, 1)
        self.assertEqual(reconciled.terminal_condition, "repository_reconciled")
        self.assertEqual(genesis.terminal_condition, "local_commit_reconciled")
        self.assertEqual(genesis.repository_path, str(self.root.resolve()))
        self.assertTrue(agent.available())
        self.assertEqual(agent.version(), "0.153.4")

    def test_finalization_branch_contract_and_external_target_gate(self) -> None:
        agent = DeterministicQualificationAgent()
        prompt = "Create Finalization PR on exactly `qualification-finalize`."
        self.assertEqual(agent._finalization_branch(prompt), "qualification-finalize")
        with self.assertRaisesRegex(RuntimeError, "QUALIFICATION_FINALIZATION_BRANCH_UNAVAILABLE"):
            agent._finalization_branch("no branch")
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(agent._github_write_target(self.root))
        with patch.dict(os.environ, {
            "EP_QUALIFICATION_GITHUB_WRITE_FLOW": "1",
            "EP_QUALIFICATION_GITHUB_REPOSITORY": "owner/repository",
        }, clear=True), patch("engineering_platform.qualification_runtime.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "https://github.com/owner/repository.git\n"
            self.assertTrue(agent._github_write_target(self.root))
            run.return_value.stdout = "https://github.com/other/repository.git\n"
            self.assertFalse(agent._github_write_target(self.root))
            run.return_value.returncode = 1
            self.assertFalse(agent._github_write_target(self.root))

    def test_controlled_interruption_window_is_explicit_and_bounded(self) -> None:
        agent = DeterministicQualificationAgent()
        ready = self.root / "arm-ready.json"
        enable = ready.with_suffix(".json.enable")
        enable.write_text("enabled\n", encoding="utf-8")
        ready.with_suffix(".json.continue").write_text("continue\n", encoding="utf-8")
        with patch.dict(os.environ, {"EP_QUALIFICATION_CONTROL_ARM_READY_FILE": str(ready)}, clear=True):
            self.assertIsNone(agent.wait_for_controlled_interruption_arm(self.root, SimpleNamespace(run_id="run-a")))
        self.assertEqual(ready.read_text(encoding="utf-8"), '{"run_id": "run-a", "phase": "EXECUTE_AGENT"}')
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(agent.wait_for_controlled_interruption_arm(self.root, SimpleNamespace(run_id="run-a")))

    def test_external_handoff_writes_only_the_explicit_fixture_contract(self) -> None:
        agent = DeterministicQualificationAgent()
        (self.root / ".engineering-platform").mkdir()
        completed = SimpleNamespace(stdout="17\n", returncode=0)
        environment = {
            "EP_QUALIFICATION_GITHUB_REPOSITORY": "owner/fixture",
            "EP_QUALIFICATION_GITHUB_BRANCH": "qualification-managed",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "engineering_platform.qualification_runtime.subprocess.run", return_value=completed
        ) as run:
            handoff = agent._create_github_managed_handoff(self.root)
            recovery = agent._create_github_managed_handoff(self.root, prompt="controlled recovery qualification")
            finalization = agent._create_github_managed_handoff(
                self.root, finalization=True, prompt="Finalization PR on exactly `qualification-finalize`."
            )
        self.assertEqual((handoff.branch, handoff.pull_request), ("qualification-managed", 17))
        self.assertEqual(recovery.branch, "qualification-managed-recovery")
        self.assertEqual(finalization.branch, "qualification-finalize")
        self.assertTrue((self.root / ".engineering-platform" / "managed-github-e2e-proof.json").is_file())
        self.assertTrue((self.root / ".engineering-platform" / "managed-github-e2e-finalization-proof.json").is_file())
        self.assertGreaterEqual(run.call_count, 21)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "QUALIFICATION_GITHUB_WRITE_CONFIGURATION_INVALID"):
                agent._create_github_managed_handoff(self.root)
        self.assertEqual(agent.review(self.root, SimpleNamespace(reviewer="quality"), "objective").reviewer, "quality")

    def test_local_github_adapter_models_open_then_merged_evidence(self) -> None:
        adapter = LocalQualificationGitHub(self.root)
        opened = adapter.pull_request(7)
        merged = adapter.pull_request(7)

        self.assertEqual((opened.state, opened.head_branch), ("OPEN", "qualification-managed"))
        self.assertEqual((merged.state, merged.head_branch), ("MERGED", "qualification-managed"))
        self.assertTrue(merged.merge_commit)
        self.assertIsNone(adapter.pull_request_for_head_branch("ignored"))
        self.assertFalse(adapter.normalize_markdown_body(7))
        self.assertIsNone(adapter.ready(7))
        self.assertIsNone(adapter.merge(7))


if __name__ == "__main__":
    unittest.main()
