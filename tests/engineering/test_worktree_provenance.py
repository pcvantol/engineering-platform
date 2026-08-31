from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.worktree_provenance import capture, verify_recovery


class WorktreeProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        for command in (("git", "init"), ("git", "config", "user.email", "test@example.invalid"), ("git", "config", "user.name", "Test")):
            subprocess.run(command, cwd=self.root, check=True, capture_output=True)
        (self.root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(("git", "add", "tracked.txt"), cwd=self.root, check=True, capture_output=True)
        subprocess.run(("git", "commit", "-m", "baseline"), cwd=self.root, check=True, capture_output=True)
        self.branch = subprocess.run(("git", "branch", "--show-current"), cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()
        self.run_id = "worktree-provenance"
        StateStore(self.root / ".engineering" / "engineering-runs").save(
            TransactionState(self.run_id, "pcvantol/djconnect", "prompt.md", "EXECUTE_AGENT", branch=self.branch)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_same_run_dirty_progress_is_preserved_only_when_digest_matches(self) -> None:
        self.assertTrue(capture(self.root, run_id=self.run_id, phase="EXECUTE_AGENT", stage="baseline"))
        (self.root / "tracked.txt").write_text("run-owned progress\n", encoding="utf-8")
        self.assertTrue(capture(self.root, run_id=self.run_id, phase="EXECUTE_AGENT", stage="interrupted"))
        self.assertTrue(verify_recovery(self.root, run_id=self.run_id, branch=self.branch))
        (self.root / "external.txt").write_text("unattributed\n", encoding="utf-8")
        self.assertFalse(verify_recovery(self.root, run_id=self.run_id, branch=self.branch))

    def test_transaction_baseline_mismatch_rejects_recovery(self) -> None:
        baseline = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=self.root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertTrue(capture(
            self.root, run_id=self.run_id, phase="EXECUTE_AGENT", stage="baseline",
            transaction_baseline_sha=baseline,
        ))
        self.assertTrue(capture(
            self.root, run_id=self.run_id, phase="EXECUTE_AGENT", stage="interrupted",
            transaction_baseline_sha=baseline,
        ))
        self.assertTrue(verify_recovery(
            self.root, run_id=self.run_id, branch=self.branch,
            transaction_baseline_sha=baseline,
        ))
        self.assertFalse(verify_recovery(
            self.root, run_id=self.run_id, branch=self.branch,
            transaction_baseline_sha="f" * 40,
        ))
