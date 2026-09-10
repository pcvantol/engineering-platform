"""Regression tests for the committed-candidate wheel source boundary."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def _builder_module() -> object:
    qualification = Path(__file__).parents[2] / "tools" / "qualification"
    path = qualification / "build_platform_wheel.py"
    specification = importlib.util.spec_from_file_location("build_platform_wheel", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("wheel builder module is unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.path.insert(0, str(qualification))
    try:
        specification.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class BuildPlatformWheelTest(unittest.TestCase):
    def test_committed_snapshot_excludes_ignored_build_residue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            destination = Path(temporary) / "snapshot"
            root.mkdir()
            subprocess.run(("git", "init", "-q", "-b", "main", str(root)), check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.email", "qualification@example.invalid"), check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.name", "Qualification"), check=True)
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            (root / "tracked.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(root), "add", ".gitignore", "tracked.txt"), check=True)
            subprocess.run(("git", "-C", str(root), "commit", "-qm", "candidate"), check=True)
            stale = root / "build" / "lib" / "retired.py"
            stale.parent.mkdir(parents=True)
            stale.write_text("retired = True\n", encoding="utf-8")

            _builder_module()._extract_committed_source(root, destination)

            self.assertEqual((destination / "tracked.txt").read_text(encoding="utf-8"), "candidate\n")
            self.assertFalse((destination / "build").exists())

    def test_committed_snapshot_rejects_tracked_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            subprocess.run(("git", "init", "-q", "-b", "main", str(root)), check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.email", "qualification@example.invalid"), check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.name", "Qualification"), check=True)
            candidate = root / "candidate.txt"
            candidate.write_text("committed\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(root), "add", "candidate.txt"), check=True)
            subprocess.run(("git", "-C", str(root), "commit", "-qm", "candidate"), check=True)
            candidate.write_text("changed\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "uncommitted tracked changes"):
                _builder_module()._extract_committed_source(root, Path(temporary) / "snapshot")


if __name__ == "__main__":
    unittest.main()
