"""Regression coverage for the deterministic EP extraction baseline control."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "scripts/engineering/audit_ep_extraction_baseline.py"
AUDIT_DOCUMENT = ROOT / "docs/engineering/extraction/EP_2X_EXTRACTION_AUDIT.md"
SPEC = importlib.util.spec_from_file_location("ep_extraction_audit", AUDIT)
assert SPEC and SPEC.loader
AUDIT_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT_MODULE)


class ExtractionBaselineAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((ROOT / "docs/engineering/extraction/EP_2X_EXTRACTION_MANIFEST.json").read_text())

    def _run(self, argument: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(AUDIT), argument], cwd=ROOT, text=True, capture_output=True, check=False)

    def _fixture_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path, dict]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        # Materialize every classified path, plus one discovered file below each
        # candidate root, without copying production content into the fixture.
        for rule in self.manifest["path_rules"]:
            path = root / rule["path"]
            if path.suffix:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            else:
                path.mkdir(parents=True, exist_ok=True)
        for relative in AUDIT_MODULE.CANDIDATE_ROOTS:
            path = root / relative / "fixture.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")
        for relative in AUDIT_MODULE.CANDIDATE_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")
        product_fixture = root / "tools/engineering/fixture.py"
        product_fixture.write_text("import homeassistant\n", encoding="utf-8")
        manifest = copy.deepcopy(self.manifest)
        manifest["candidate_universe_digest"] = AUDIT_MODULE.universe_digest(AUDIT_MODULE.candidate_universe(root))
        return temporary, root, manifest

    def test_checked_in_manifest_is_valid_and_audit_is_deterministic(self) -> None:
        self.assertEqual(self._run("--check").returncode, 0)
        first, second = self._run("--projection"), self._run("--projection")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        projection = json.loads(first.stdout)
        self.assertEqual(projection["candidate_universe_count"], 283)
        self.assertEqual(projection["manifest_semantic_digest"], self.manifest["manifest_semantic_digest"])
        self.assertEqual(projection["classified_exactly_once"], projection["candidate_universe_count"])
        self.assertEqual(projection["unclassified"], 0)
        self.assertEqual(projection["ambiguous"], 0)

    def test_audit_document_navigates_to_each_canonical_control(self) -> None:
        contents = AUDIT_DOCUMENT.read_text(encoding="utf-8")

        self.assertIn("## Control navigation", contents)
        for relative_path in (
            "EP_2X_EXTRACTION_BASELINE.md",
            "EP_2X_EXTRACTION_MANIFEST.json",
            "../../../scripts/engineering/audit_ep_extraction_baseline.py",
            "../../../tests/engineering/test_ep_extraction_baseline.py",
            "../../development/ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md",
        ):
            self.assertIn(relative_path, contents)

    def test_duplicate_path_invalid_classification_and_unsafe_path_fail(self) -> None:
        duplicate = copy.deepcopy(self.manifest)
        duplicate["path_rules"].append(copy.deepcopy(duplicate["path_rules"][0]))
        self.assertTrue(any("duplicate canonical path" in error for error in AUDIT_MODULE.validate(duplicate, ROOT)))
        invalid = copy.deepcopy(self.manifest)
        invalid["path_rules"][0]["classification"] = "NOT_A_CLASSIFICATION"
        self.assertTrue(any("invalid classification" in error for error in AUDIT_MODULE.validate(invalid, ROOT)))
        for unsafe in ("/private/path", "C:\\Users\\person", "\\\\server\\share", "../outside"):
            unsafe_manifest = copy.deepcopy(self.manifest)
            unsafe_manifest["path_rules"][0]["path"] = unsafe
            self.assertTrue(any("unsafe path" in error for error in AUDIT_MODULE.validate(unsafe_manifest, ROOT)))

    def test_unclassified_and_missing_required_path_fail(self) -> None:
        unclassified = copy.deepcopy(self.manifest)
        unclassified["path_rules"] = [rule for rule in unclassified["path_rules"] if rule["path"] != "tools/engineering"]
        self.assertTrue(any("unclassified candidates" in error for error in AUDIT_MODULE.validate(unclassified, ROOT)))
        missing = copy.deepcopy(self.manifest)
        missing["path_rules"][0]["path"] = "tools/engineering/does-not-exist"
        self.assertTrue(any("missing required classified path" in error for error in AUDIT_MODULE.validate(missing, ROOT)))

    def test_valid_classification_change_requires_manifest_reconciliation(self) -> None:
        changed = copy.deepcopy(self.manifest)
        changed["path_rules"][0]["classification"] = "DJCONNECT_RETAINED"
        self.assertTrue(any("manifest semantic drift" in error for error in AUDIT_MODULE.validate(changed, ROOT)))

    def test_deleted_required_classified_path_and_blocking_import_are_detected(self) -> None:
        temporary, root, manifest = self._fixture_root()
        with temporary:
            projection = AUDIT_MODULE.projection(manifest, root)
            self.assertEqual(projection["import_audit"]["home_assistant_runtime_imports"], 1)
            (root / "scripts/engineering/audit_ep_extraction_baseline.py").unlink()
            self.assertTrue(any("missing required classified path" in error for error in AUDIT_MODULE.validate(manifest, root)))

    def test_equal_specificity_overlap_fails_and_file_override_is_deterministic(self) -> None:
        overlap = copy.deepcopy(self.manifest)
        overlap["path_rules"].append(copy.deepcopy(overlap["path_rules"][1]))
        self.assertTrue(any("ambiguous candidates" in error for error in AUDIT_MODULE.validate(overlap, ROOT)))
        winner, candidates = AUDIT_MODULE.effective_rule("tools/engineering/ENGINEERING_PLATFORM_VERSION.json", self.manifest["path_rules"])
        self.assertEqual(len(candidates), 1)
        self.assertIsNotNone(winner)
        self.assertEqual(winner["classification"], "EP_RELEASE_ASSET")

    def test_new_ep_candidate_file_cannot_escape_and_fixture_drift_is_detected(self) -> None:
        temporary, root, manifest = self._fixture_root()
        with temporary:
            self.assertEqual(AUDIT_MODULE.validate(manifest, root), [])
            new_file = root / "tools/engineering/new_ep_module.py"
            new_file.write_text("fixture", encoding="utf-8")
            errors = AUDIT_MODULE.validate(manifest, root)
            self.assertTrue(any("candidate universe drift" in error for error in errors))

    def test_generated_run_evidence_does_not_change_ep_candidate_universe(self) -> None:
        temporary, root, _ = self._fixture_root()
        with temporary:
            before = AUDIT_MODULE.candidate_universe(root)
            generated = root / "docs/engineering/runs/2026/finalization.md"
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text("generated evidence", encoding="utf-8")
            self.assertEqual(AUDIT_MODULE.candidate_universe(root), before)


if __name__ == "__main__":
    unittest.main()
