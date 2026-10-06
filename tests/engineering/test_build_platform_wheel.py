"""The qualification build must consume only the selected Git commit."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "qualification" / "build_platform_wheel.py"


def _module():
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location("build_platform_wheel", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class CommittedSourceBuildTests(unittest.TestCase):
    def test_materialization_rejects_tracked_edits_and_excludes_checkout_residue(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src" / "engineering_platform"
            package.mkdir(parents=True)
            (root / ".gitignore").write_text("src/engineering_platform/ignored.py\n", encoding="utf-8")
            (root / "pyproject.toml").write_text('[project]\nname = "engineering-platform"\nversion = "1.0.0"\n', encoding="utf-8")
            source = package / "__init__.py"
            source.write_text('IDENTITY = "committed"\n', encoding="utf-8")
            for command in (("init", "-q"), ("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline")):
                subprocess.run(("git", *command), cwd=root, check=True, capture_output=True)

            source.write_text('IDENTITY = "dirty"\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "tracked source"):
                with module.committed_source(root):
                    pass

            source.write_text('IDENTITY = "committed"\n', encoding="utf-8")
            (package / "ignored.py").write_text("POISON = True\n", encoding="utf-8")
            (package / "untracked.py").write_text("POISON = True\n", encoding="utf-8")
            with module.committed_source(root) as (materialized, head, tree):
                self.assertEqual((materialized / "src/engineering_platform/__init__.py").read_text(), 'IDENTITY = "committed"\n')
                self.assertFalse((materialized / "src/engineering_platform/ignored.py").exists())
                self.assertFalse((materialized / "src/engineering_platform/untracked.py").exists())
                self.assertEqual(len(head), 40)
                self.assertEqual(len(tree), 40)


if __name__ == "__main__":
    unittest.main()
