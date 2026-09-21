from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import managed_workspace_recovery
from engineering_platform.local_repository_binding import LocalRepositoryBinding


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(("git", *arguments), cwd=root, text=True,
                            capture_output=True, check=True)
    return result.stdout.strip()


class ManagedWorkspaceRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repository"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "remote", "add", "origin", "https://github.com/pcvantol/forge.git")
        (self.root / "BOOTSTRAP.md").write_text("bootstrap\n", encoding="utf-8")
        (self.root / "tracked.txt").write_text("before\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "baseline")
        self.head = git(self.root, "rev-parse", "HEAD")
        git(self.root, "switch", "-c", "forge/mission-0014-installed-health")
        (self.root / "tracked.txt").write_text("after\n", encoding="utf-8")
        (self.root / "new.txt").write_text("new\n", encoding="utf-8")
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE execution_run_leases(lease_state TEXT NOT NULL)")

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def binding(self) -> LocalRepositoryBinding:
        return LocalRepositoryBinding("forge", "forge", self.root, "BOUND", "before", "now")

    def test_preserves_wip_then_restores_main_and_removes_transaction_branch(self) -> None:
        def synchronize(client, root: Path) -> None:
            git(root, "switch", "main")

        with (
            patch.object(managed_workspace_recovery, "resolve_execution_repository",
                         return_value=self.binding()),
            patch.object(managed_workspace_recovery.SubprocessRepositoryClient,
                         "synchronize_main", autospec=True, side_effect=synchronize),
        ):
            result = managed_workspace_recovery.recover(
                connection=self.connection, data_root=self.base / "data",
                project_id="forge", repository_id="forge",
                operation_id="recover-mission3-r18-0001",
                expected_branch="forge/mission-0014-installed-health",
                expected_head=self.head, backup_root=self.base / "backups",
            )
        self.assertEqual(result["result"], "RECOVERED")
        self.assertEqual(git(self.root, "branch", "--show-current"), "main")
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertNotIn("forge/mission-0014-installed-health", git(self.root, "branch", "--list"))
        backup = self.base / "backups" / "recover-mission3-r18-0001"
        self.assertIn("after", (backup / "tracked.patch").read_text(encoding="utf-8"))
        self.assertEqual((backup / "untracked" / "new.txt").read_text(encoding="utf-8"), "new\n")
        receipt = json.loads((backup / "receipt.json").read_text(encoding="utf-8"))
        self.assertTrue(receipt["final_clean"])

    def test_active_lease_blocks_before_repository_mutation(self) -> None:
        self.connection.execute("INSERT INTO execution_run_leases VALUES('ACTIVE')")
        with patch.object(managed_workspace_recovery, "resolve_execution_repository") as resolver:
            with self.assertRaisesRegex(
                managed_workspace_recovery.ManagedWorkspaceRecoveryError,
                "ACTIVE_LEASE",
            ):
                managed_workspace_recovery.recover(
                    connection=self.connection, data_root=self.base / "data",
                    project_id="forge", repository_id="forge",
                    operation_id="recover-mission3-r18-0002",
                    expected_branch="forge/mission-0014-installed-health",
                    expected_head=self.head, backup_root=self.base / "backups",
                )
        resolver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
