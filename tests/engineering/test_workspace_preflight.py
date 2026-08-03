from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.engineering import workspace_preflight


ROOT = Path(__file__).resolve().parents[2]


class WorkspacePreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "host"
        engineering = self.root / "tools" / "engineering"
        engineering.mkdir(parents=True)
        (engineering / "ENGINEERING_PLATFORM_CONFIG.json").write_text((ROOT / "tools" / "engineering" / "ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"), encoding="utf-8")
        (self.root / ".engineering" / "status").mkdir(parents=True)
        self._initialize(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("git", "-C", str(path), *arguments), text=True, capture_output=True, check=True)

    def _initialize(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._run(path, "init", "--initial-branch=main")
        self._run(path, "config", "user.email", "preflight@example.invalid")
        self._run(path, "config", "user.name", "Workspace Preflight")
        (path / "README.md").write_text("workspace\n", encoding="utf-8")
        (path / ".gitignore").write_text(".engineering/\n", encoding="utf-8")
        self._run(path, "add", "-A")
        self._run(path, "commit", "-m", "Initial workspace")

    def _managed(self) -> workspace_preflight.WorkspacePreflightResult:
        remote = Path(self.temporary.name) / "origin.git"
        subprocess.run(("git", "init", "--bare", str(remote)), text=True, capture_output=True, check=True)
        self._run(self.root, "remote", "add", "origin", str(remote))
        self._run(self.root, "push", "-u", "origin", "main")
        return workspace_preflight.execute(self.root, "Execution Mode: Managed\n", run_id="inbox-workspace")

    def _failed(self, result: workspace_preflight.WorkspacePreflightResult) -> set[str]:
        return {check.identifier for check in result.checks if check.outcome == "FAIL"}

    def test_managed_valid_repository_persists_evidence(self) -> None:
        result = self._managed()
        self.assertEqual(result.outcome, "PASS")
        self.assertEqual(workspace_preflight.latest(self.root)["run_id"], "inbox-workspace")

    def test_missing_repository_fails(self) -> None:
        result = workspace_preflight.execute(self.root, "Execution Mode: Genesis\nTarget repository: /missing/workspace\n")
        self.assertIn("target_repository", self._failed(result))

    def test_non_git_directory_fails(self) -> None:
        target = Path(self.temporary.name) / "workspace" / "plain"
        target.mkdir(parents=True)
        self._set_workspace_root(target.parent)
        self.assertIn("git_repository", self._failed(workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {target}\n")))

    def test_staged_unstaged_and_untracked_changes_fail_individually(self) -> None:
        self._managed()
        (self.root / "README.md").write_text("staged\n", encoding="utf-8")
        self._run(self.root, "add", "README.md")
        self.assertIn("worktree_staged", self._failed(workspace_preflight.execute(self.root, "Execution Mode: Managed\n")))
        self._run(self.root, "reset", "HEAD", "README.md")
        self.assertIn("worktree_unstaged", self._failed(workspace_preflight.execute(self.root, "Execution Mode: Managed\n")))
        self._run(self.root, "checkout", "--", "README.md")
        (self.root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        self.assertIn("worktree_untracked", self._failed(workspace_preflight.execute(self.root, "Execution Mode: Managed\n")))

    def test_index_lock_and_unfinished_operations_fail(self) -> None:
        self._managed()
        git = self.root / ".git"
        (git / "index.lock").write_text("", encoding="utf-8")
        self.assertIn("git_index_lock", self._failed(workspace_preflight.execute(self.root, "Execution Mode: Managed\n")))
        (git / "index.lock").unlink()
        (git / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        (git / "rebase-merge").mkdir()
        self.assertTrue({"git_merge", "git_rebase"}.issubset(self._failed(workspace_preflight.execute(self.root, "Execution Mode: Managed\n"))))

    def test_invalid_workspace_root_fails(self) -> None:
        target = Path(self.temporary.name) / "other" / "target"
        self._initialize(target)
        self._set_workspace_root(Path(self.temporary.name) / "approved")
        self.assertIn("WORKSPACE_TARGET_AUTHORIZED", self._failed(workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {target}\n")))

    def test_genesis_success_needs_no_remote(self) -> None:
        target = Path(self.temporary.name) / "workspace" / "target"
        self._initialize(target)
        self._set_workspace_root(target.parent)
        result = workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {target}\n")
        self.assertEqual(result.outcome, "PASS")
        self.assertEqual(result.execution_mode, "GENESIS")

    def test_explicit_workspace_root_authorizes_forge_direct_child(self) -> None:
        workspace = Path(self.temporary.name) / "GitHub"
        target = workspace / "forge"
        self._initialize(target)
        self._set_workspace_authorization([{"path": str(workspace), "repository_scope": "direct_children"}])
        self.assertEqual(workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {target}\n").outcome, "PASS")

    def test_direct_children_reject_nested_repository_but_descendants_accept_it(self) -> None:
        workspace = Path(self.temporary.name) / "GitHub"
        target = workspace / "group" / "forge"
        self._initialize(target)
        self._set_workspace_authorization([{"path": str(workspace), "repository_scope": "direct_children"}])
        self.assertIn("WORKSPACE_TARGET_AUTHORIZED", self._failed(workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {target}\n")))
        self._set_workspace_authorization([{"path": str(workspace), "repository_scope": "descendants"}])
        self.assertEqual(workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {target}\n").outcome, "PASS")

    def test_allow_list_authorizes_and_deny_list_wins(self) -> None:
        target = Path(self.temporary.name) / "outside" / "forge"
        self._initialize(target)
        self._set_workspace_authorization([], allowed=[str(target)])
        self.assertEqual(workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {target}\n").outcome, "PASS")
        self._set_workspace_authorization([], allowed=[str(target)], denied=[str(target)])
        self.assertIn("WORKSPACE_TARGET_AUTHORIZED", self._failed(workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {target}\n")))

    def test_path_prefix_traversal_and_symlink_escape_are_rejected(self) -> None:
        workspace = Path(self.temporary.name) / "GitHub"
        target = Path(self.temporary.name) / "GitHub-lookalike" / "forge"
        self._initialize(target)
        self._set_workspace_authorization([{"path": str(workspace), "repository_scope": "descendants"}])
        result = workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {target}\n")
        self.assertIn("WORKSPACE_TARGET_AUTHORIZED", self._failed(result))
        nested = workspace / "nested" / "forge"
        self._initialize(nested)
        traversal = workspace / "nested" / ".." / "nested" / "forge"
        self.assertIn("WORKSPACE_TARGET_AUTHORIZED", self._failed(workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {traversal}\n")))
        link = workspace / "link"
        workspace.mkdir(exist_ok=True)
        link.symlink_to(target.parent, target_is_directory=True)
        self.assertIn("WORKSPACE_TARGET_AUTHORIZED", self._failed(workspace_preflight.execute(self.root, f"Execution Mode: Genesis\nTarget repository: {link / target.name}\n")))

    def _set_workspace_root(self, root: Path) -> None:
        configuration = json.loads((self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"))
        configuration["schema_version"] = 1
        configuration["workspace"].pop("workspace_authorization", None)
        (self.root / "tools" / "engineering" / "ENGINEERING_PLATFORM_CONFIG.json").write_text(json.dumps(configuration), encoding="utf-8")
        (self.root / ".engineering" / "engineering-platform.local.json").write_text(json.dumps({"workspace": {"provisioning_root": str(root)}}), encoding="utf-8")

    def _set_workspace_authorization(self, roots: list[dict[str, str]], *, allowed: list[str] | None = None, denied: list[str] | None = None) -> None:
        authorization = {
            "allowed_roots": roots,
            "allowed_repositories": allowed or [],
            "denied_repositories": denied or [],
            "symlink_policy": "reject",
            "case_sensitivity": "host",
        }
        (self.root / ".engineering" / "engineering-platform.local.json").write_text(json.dumps({"workspace": {"workspace_authorization": authorization}}), encoding="utf-8")
