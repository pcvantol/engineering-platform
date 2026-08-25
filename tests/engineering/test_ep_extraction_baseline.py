"""Regression coverage for the deterministic EP extraction baseline audit."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "scripts/engineering/audit_ep_extraction_baseline.py"
SPEC = importlib.util.spec_from_file_location("ep_extraction_audit", AUDIT)
assert SPEC and SPEC.loader
AUDIT_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT_MODULE)


class ExtractionBaselineAuditTests(unittest.TestCase):
    def _run(self, argument: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(AUDIT), argument], cwd=ROOT, text=True, capture_output=True, check=False
        )

    def test_manifest_is_valid_and_projection_is_deterministic(self) -> None:
        self.assertEqual(self._run("--check").returncode, 0)
        first = self._run("--projection")
        second = self._run("--projection")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        projection = json.loads(first.stdout)
        self.assertGreater(projection["entry_count"], 0)
        self.assertEqual(projection["baseline"]["source_commit"], "05583f229ad878c5c06f264a661b4d92eb33b128")
        self.assertIn("EP_PRODUCT_SOURCE", projection["classifications"])

    def test_manifest_paths_are_safe_and_classifications_are_complete(self) -> None:
        manifest = json.loads((ROOT / "docs/engineering/extraction/EP_2X_EXTRACTION_MANIFEST.json").read_text())
        allowed = set(manifest["classifications"])
        for entry in manifest["paths"]:
            self.assertFalse(Path(entry["path"]).is_absolute())
            self.assertNotIn("..", Path(entry["path"]).parts)
            self.assertIn(entry["classification"], allowed)

    def test_duplicate_and_unknown_classifications_are_rejected(self) -> None:
        manifest = json.loads((ROOT / "docs/engineering/extraction/EP_2X_EXTRACTION_MANIFEST.json").read_text())
        duplicate = {**manifest, "paths": manifest["paths"] + [dict(manifest["paths"][0])]}
        self.assertTrue(any("duplicate path" in error for error in AUDIT_MODULE.validate(duplicate, ROOT)))
        invalid = json.loads(json.dumps(manifest))
        invalid["paths"][0]["classification"] = "NOT_A_CLASSIFICATION"
        self.assertTrue(any("unknown classification" in error for error in AUDIT_MODULE.validate(invalid, ROOT)))

    def test_portable_absolute_and_parent_paths_are_rejected(self) -> None:
        manifest = json.loads((ROOT / "docs/engineering/extraction/EP_2X_EXTRACTION_MANIFEST.json").read_text())
        for unsafe_path in ("/private/path", "C:\\Users\\person", "\\\\server\\share", "../outside"):
            invalid = json.loads(json.dumps(manifest))
            invalid["paths"][0]["path"] = unsafe_path
            self.assertTrue(any("unsafe path" in error for error in AUDIT_MODULE.validate(invalid, ROOT)))
