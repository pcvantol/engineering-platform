from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
NFR_DOCUMENT = ROOT / "docs" / "engineering" / "ENGINEERING_PLATFORM_NON_FUNCTIONAL_REQUIREMENTS.md"
MIGRATION_PLAN = ROOT / "docs" / "development" / "ENGINEERING_PLATFORM_EXTRACTION_MIGRATION_PLAN.md"


class EngineeringPlatformNonFunctionalRequirementsTest(unittest.TestCase):
    def test_canonical_matrix_covers_current_release_gates(self) -> None:
        document = NFR_DOCUMENT.read_text(encoding="utf-8")
        for requirement in (
            "NFR-UX-001",
            "NFR-LOC-001",
            "NFR-A11Y-001",
            "NFR-SEC-001",
            "NFR-REL-001",
            "NFR-OPS-001",
            "NFR-PERF-001",
            "NFR-QUAL-001",
            "NFR-PKG-001",
            "NFR-TDE-001",
            "NFR-INSTALL-001",
        ):
            self.assertIn(requirement, document)

    def test_current_coverage_and_tde_status_are_unambiguous(self) -> None:
        document = NFR_DOCUMENT.read_text(encoding="utf-8")
        self.assertIn("at least **80.20% branch coverage**", document)
        self.assertIn("TDE is currently observation evidence, not a release blocker.", document)
        self.assertIn("`en`, `nl`, `de`, `fr` and `es`", document)

    def test_production_wheel_gate_is_explicit_and_release_blocking(self) -> None:
        nfr = NFR_DOCUMENT.read_text(encoding="utf-8")
        plan = MIGRATION_PLAN.read_text(encoding="utf-8")
        self.assertIn("NFR-PKG-001", nfr)
        self.assertIn("clean tagged checkout in explicit production release mode", nfr)
        self.assertIn("allowlisted", nfr)
        self.assertIn("blocking standalone-wheel release gate", nfr)
        self.assertIn("clean, tagged checkout in explicit production\n   release mode", plan)
        self.assertIn("tests, debug/development assets and development-only dependencies", plan)
        self.assertIn("manifest/SBOM/checksum", plan)
