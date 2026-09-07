from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]


def _module():
    script = ROOT / "tools" / "qualification" / "production_wheel_qualification.py"
    spec = importlib.util.spec_from_file_location("production_wheel_qualification", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _release_fixture(directory: str) -> tuple[Path, Path]:
    root = Path(directory)
    (root / "pyproject.toml").write_text('[project]\nversion = "2.2.0"\ndependencies = []\n', encoding="utf-8")
    return root, root / "engineering_platform-2.2.0-py3-none-any.whl"


class ProductionWheelQualificationTest(unittest.TestCase):
    def test_accepts_only_allowlisted_runtime_members(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root, wheel = _release_fixture(directory)
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("engineering_platform/server.py", "")
                archive.writestr("engineering_platform/assets/dashboard.css", "")
                archive.writestr("engineering_platform-2.2.0.dist-info/METADATA", "")
                archive.writestr("engineering_platform-2.2.0.dist-info/WHEEL", "")
                archive.writestr("engineering_platform-2.2.0.dist-info/RECORD", "")
            evidence = module.qualify(root, wheel, "2.2.0")
            self.assertEqual(evidence["result"], "PASS")
            self.assertIn("engineering_platform/assets/dashboard.css", evidence["members"])

    def test_rejects_test_or_debug_material(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root, wheel = _release_fixture(directory)
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("engineering_platform/tests/debug.py", "")
            with self.assertRaisesRegex(RuntimeError, "unexpected production wheel members"):
                module.qualify(root, wheel, "2.2.0")

    def test_rejects_packaged_documentation(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root, wheel = _release_fixture(directory)
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("engineering_platform/assets/operations-console/ASSET_CATALOG.md", "")
            with self.assertRaisesRegex(RuntimeError, "unexpected production wheel members"):
                module.qualify(root, wheel, "2.2.0")


if __name__ == "__main__":
    unittest.main()
