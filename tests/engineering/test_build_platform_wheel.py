"""The qualification build must consume only the selected Git commit."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


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
    def test_installed_qualification_rejects_extra_missing_and_changed_wheel_bytes(self) -> None:
        specification = importlib.util.spec_from_file_location("mpr_installed_matrix", SCRIPT.with_name("mpr_installed_matrix.py"))
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            built, supplied = root / "built.whl", root / "supplied.whl"
            expected = {"engineering_platform/__init__.py": "source", "engineering_platform-1.0.dist-info/METADATA": "metadata"}
            with zipfile.ZipFile(built, "w") as archive:
                for name, value in expected.items(): archive.writestr(name, value)
            supplied.write_bytes(built.read_bytes())
            module.verify_wheel_identity(supplied, built)
            for contents in ({**expected, "uncommitted.pth": "import uncommitted"},
                             {"engineering_platform/__init__.py": "source"},
                             {**expected, "engineering_platform-1.0.dist-info/METADATA": "changed"}):
                with self.subTest(contents=tuple(contents)):
                    with zipfile.ZipFile(supplied, "w") as archive:
                        for name, value in contents.items(): archive.writestr(name, value)
                    with self.assertRaisesRegex(RuntimeError, "MPR_WHEEL_SOURCE_IDENTITY_MISMATCH"):
                        module.verify_wheel_identity(supplied, built)

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
