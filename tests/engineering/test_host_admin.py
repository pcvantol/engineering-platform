from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest
from unittest.mock import patch

from engineering_platform import host_admin


def completed(args: tuple[str, ...], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, "")


class HostAdminTargetRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / "approved"
        self.primary = self.root / "primary"
        self.child = self.root / "worktrees" / "candidate"
        self.primary.mkdir(parents=True); self.child.mkdir(parents=True)
        (self.primary / ".git").mkdir()
        self.targets = host_admin.HostAdminTargetRegistry((
            host_admin.HostAdminTarget("approved", self.root, self.primary, (("candidate", self.child),)),
        ))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _inventory(self, *extra: subprocess.CompletedProcess[str]) -> list[subprocess.CompletedProcess[str]]:
        return [completed(("git",), f"worktree {self.primary.resolve()}\n\nworktree {self.child.resolve()}\n"), *extra]

    def test_registry_accepts_only_opaque_registered_identifiers(self) -> None:
        self.assertEqual(self.targets.worktree("approved", "candidate"), self.child.resolve())
        for value in ("../approved", str(self.child), None):
            with self.assertRaises(host_admin.HostAdminTargetError):
                self.targets.target(value)
        with self.assertRaises(host_admin.HostAdminTargetError):
            self.targets.worktree("approved", "../candidate")

    def test_registry_rejects_wrong_root_and_symlink_escape(self) -> None:
        outside = Path(self.temporary.name) / "outside"; outside.mkdir()
        with self.assertRaises(host_admin.HostAdminTargetError):
            host_admin.HostAdminTargetRegistry((host_admin.HostAdminTarget("wrong", self.root, outside),))
        escape = self.root / "escape"; escape.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(host_admin.HostAdminTargetError):
            host_admin.HostAdminTargetRegistry((host_admin.HostAdminTarget("escape", self.root, self.primary, (("escape", escape),)),))

    def test_source_has_no_legacy_path_inference_or_deletion_primitive(self) -> None:
        source = (Path(__file__).parents[2] / "src" / "engineering_platform" / "host_admin.py").read_text(encoding="utf-8")
        for forbidden in ("Path.cwd(", "headers.get(", "git remote", ".unlink(", "rmtree(",
                          '"worktree", "remove"', "def _remove_safe_worktree", "def _recover_stale_workspace_git_lock"):
            self.assertNotIn(forbidden, source)

    @patch("engineering_platform.host_admin.log_event")
    @patch("engineering_platform.host_admin.component_logger")
    @patch("engineering_platform.host_admin.subprocess.run")
    def test_inventory_and_removed_mutation_gate_are_audited(self, run: object, logger: object, audit: object) -> None:
        run.side_effect = self._inventory(completed(("git",)))
        inventory = host_admin.worktree_inventory(self.targets, "approved")
        self.assertTrue(inventory["worktrees"][0]["present"])
        run.side_effect = self._inventory(completed(("git",)))
        result = host_admin.remove_worktree(self.root, self.targets, "approved", "candidate")
        self.assertEqual(result["outcome"], "UNSUPPORTED_REMOVED")
        audit.assert_called()

    @patch("engineering_platform.host_admin.log_event")
    @patch("engineering_platform.host_admin.component_logger")
    @patch("engineering_platform.host_admin.subprocess.run")
    def test_mutation_gate_rejects_stale_dirty_active_and_unknown_targets(self, run: object, _logger: object, audit: object) -> None:
        # Git no longer reporting the explicitly registered target is stale.
        run.return_value = completed(("git",), f"worktree {self.primary.resolve()}\n")
        with self.assertRaisesRegex(host_admin.HostAdminTargetError, "STALE"):
            host_admin.remove_worktree(self.root, self.targets, "approved", "candidate")
        with self.assertRaises(host_admin.HostAdminTargetError):
            host_admin.remove_worktree(self.root, self.targets, "unknown", "candidate")
        run.side_effect = self._inventory(completed(("git",), " M file\n"))
        with self.assertRaisesRegex(host_admin.HostAdminTargetError, "DIRTY"):
            host_admin.remove_worktree(self.root, self.targets, "approved", "candidate")
        run.side_effect = self._inventory(completed(("git",)))
        (self.child / ".git").mkdir()
        (self.child / ".git" / "index.lock").touch()
        with self.assertRaisesRegex(host_admin.HostAdminTargetError, "ACTIVE"):
            host_admin.remove_worktree(self.root, self.targets, "approved", "candidate")
        self.assertTrue(audit.called)

    @patch("engineering_platform.host_admin.log_event")
    @patch("engineering_platform.host_admin.component_logger")
    def test_git_lock_repair_is_exact_contained_and_removed(self, _logger: object, audit: object) -> None:
        lock = self.primary / ".git" / "index.lock"; lock.touch()
        diagnosis = host_admin.diagnose_git_lock(self.targets, "approved")
        self.assertEqual(diagnosis["lock"], str(lock.resolve()))
        result = host_admin.repair_git_lock(self.root, self.targets, "approved")
        self.assertEqual(result["outcome"], "UNSUPPORTED_REMOVED")
        lock.unlink(); lock.symlink_to(Path(self.temporary.name) / "outside-lock")
        with self.assertRaisesRegex(host_admin.HostAdminTargetError, "ESCAPE"):
            host_admin.repair_git_lock(self.root, self.targets, "approved")
        self.assertTrue(audit.called)

    @patch("engineering_platform.host_admin.log_event")
    @patch("engineering_platform.host_admin.component_logger")
    @patch("engineering_platform.host_admin.subprocess.run")
    def test_git_lock_repair_rejects_a_detectable_active_owner(self, run: object, _logger: object, audit: object) -> None:
        (self.primary / ".git" / "index.lock").touch()
        run.return_value = completed(("lsof",), "git 123 operator\n")
        with self.assertRaisesRegex(host_admin.HostAdminTargetError, "ACTIVE"):
            host_admin.repair_git_lock(self.root, self.targets, "approved")
        self.assertTrue(audit.called)
