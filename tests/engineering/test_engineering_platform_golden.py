from __future__ import annotations
from pathlib import Path
import json
import tempfile
import unittest
from engineering_platform.golden_scenario import run

ROOT = Path(__file__).resolve().parents[2]

class EngineeringPlatformGoldenTest(unittest.TestCase):
    def test_golden_lifecycle_passes(self) -> None:
        self.assertEqual(run(ROOT)["result"], "ENGINEERING_PLATFORM_GOLDEN_PASS")
    def test_golden_failure_is_diagnostic(self) -> None:
        result = run(ROOT, fail_phase="runtime_execution_simulation")
        self.assertEqual(result["result"], "ENGINEERING_PLATFORM_GOLDEN_FAIL")
        self.assertEqual(result["failed_phase"], "runtime_execution_simulation")

    def test_golden_receipt_is_written_only_to_explicit_evidence_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence_directory = Path(temporary) / "receipt"
            result = run(ROOT, evidence_directory=evidence_directory)
            receipt = evidence_directory / "ep-golden-001.json"
            self.assertTrue(receipt.is_file())
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["result"], result["result"])
