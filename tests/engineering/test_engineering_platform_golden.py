from __future__ import annotations
from pathlib import Path
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
