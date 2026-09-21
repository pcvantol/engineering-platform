from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import managed_workspace_recovery, platform_admin, server
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

    def test_invalid_scope_and_backup_location_fail_before_mutation(self) -> None:
        cases = (
            ({"operation_id": "short"}, "OPERATION_INVALID"),
            ({"expected_branch": "main"}, "BRANCH_INVALID"),
            ({"expected_head": "not-a-sha"}, "HEAD_INVALID"),
            ({"backup_root": self.base / "data" / "nested"}, "BACKUP_UNSAFE"),
        )
        defaults = {
            "connection": self.connection, "data_root": self.base / "data",
            "project_id": "forge", "repository_id": "forge",
            "operation_id": "recover-mission3-r18-0003",
            "expected_branch": "forge/mission-0014-installed-health",
            "expected_head": self.head, "backup_root": self.base / "backups",
        }
        with patch.object(managed_workspace_recovery, "resolve_execution_repository") as resolver:
            for changes, code in cases:
                with self.subTest(code=code), self.assertRaisesRegex(
                    managed_workspace_recovery.ManagedWorkspaceRecoveryError, code,
                ):
                    managed_workspace_recovery.recover(**(defaults | changes))
        resolver.assert_not_called()

    def test_expected_branch_and_head_must_still_match(self) -> None:
        with patch.object(managed_workspace_recovery, "resolve_execution_repository",
                          return_value=self.binding()):
            with self.assertRaisesRegex(
                managed_workspace_recovery.ManagedWorkspaceRecoveryError,
                "EXPECTATION_MISMATCH",
            ):
                managed_workspace_recovery.recover(
                    connection=self.connection, data_root=self.base / "data",
                    project_id="forge", repository_id="forge",
                    operation_id="recover-mission3-r18-0004",
                    expected_branch="forge/different-transaction",
                    expected_head=self.head, backup_root=self.base / "backups",
                )

    def test_server_cli_dispatches_recovery_and_maps_refusals(self) -> None:
        data_root = self.base / "server-data"
        backup_root = self.base / "backups"
        arguments = [
            "recover-managed-workspace", "--data-root", str(data_root),
            "--project-id", "forge", "--repository-id", "forge",
            "--operation-id", "recover-mission3-r18-0005",
            "--expected-branch", "forge/mission-0014-installed-health",
            "--expected-head", self.head, "--backup-root", str(backup_root),
        ]
        with (
            patch.object(server, "status", return_value={"running": False}),
            patch.object(server, "initialize"),
            patch.object(platform_admin, "require_installation_owner"),
            patch.object(server.storage, "sqlite_connection") as connection_factory,
            patch.object(managed_workspace_recovery, "recover",
                         return_value={"result": "RECOVERED"}) as recover,
        ):
            connection = object()
            connection_factory.return_value.__enter__.return_value = connection
            with redirect_stdout(io.StringIO()):
                self.assertEqual(server.main(arguments), 0)
            recover.assert_called_once_with(
                connection=connection, data_root=data_root, project_id="forge",
                repository_id="forge", operation_id="recover-mission3-r18-0005",
                expected_branch="forge/mission-0014-installed-health",
                expected_head=self.head, backup_root=backup_root,
            )

        with redirect_stdout(io.StringIO()):
            self.assertEqual(server.main([
                "recover-managed-workspace", "--data-root", str(data_root),
            ]), 2)

        with (
            patch.object(server, "status", return_value={"running": True}),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(server.main(arguments), 2)

        with (
            patch.object(server, "status", return_value={"running": False}),
            patch.object(server, "initialize"),
            patch.object(platform_admin, "require_installation_owner"),
            patch.object(server.storage, "sqlite_connection") as connection_factory,
            patch.object(
                managed_workspace_recovery, "recover",
                side_effect=managed_workspace_recovery.ManagedWorkspaceRecoveryError(
                    "MANAGED_WORKSPACE_RECOVERY_EXPECTATION_MISMATCH"
                ),
            ),
            redirect_stdout(io.StringIO()),
        ):
            connection_factory.return_value.__enter__.return_value = object()
            self.assertEqual(server.main(arguments), 2)


if __name__ == "__main__":
    unittest.main()
