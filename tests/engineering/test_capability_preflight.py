from __future__ import annotations
from pathlib import Path
import tempfile
import unittest
from tools.engineering import capability_preflight


class CapabilityPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        target = self.root / "tools/engineering"
        target.mkdir(parents=True)
        source = (
            Path(__file__).resolve().parents[2]
            / "tools/engineering/ENGINEERING_PLATFORM_VERSION.json"
        )
        (target / "ENGINEERING_PLATFORM_VERSION.json").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (self.root / ".engineering/status").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_compatible_declaration_is_deterministic(self) -> None:
        prompt = "Execution Host Version: 2.0.0\nRunner Version: 2.0.0\nEngineering Database Schema: 6\nRequired Capabilities: capability_preflight\n"
        first = capability_preflight.execute(self.root, prompt, run_id="inbox-one")
        second = capability_preflight.execute(self.root, prompt, run_id="inbox-two")
        self.assertEqual(first.outcome, "PASS")
        self.assertEqual(first.recoverability, "RETRYABLE")
        self.assertEqual(
            [c.identifier for c in first.checks], [c.identifier for c in second.checks]
        )
        self.assertEqual(capability_preflight.latest(self.root)["run_id"], "inbox-two")

    def test_unsupported_requirement_fails_closed_with_evidence(self) -> None:
        result = capability_preflight.execute(
            self.root, "Report Format: 99\nRequired Provider Support: sqlite\n"
        )
        self.assertEqual(result.outcome, "FAIL")
        self.assertEqual(result.failure_origin, "CAPABILITY")
        self.assertEqual(result.recoverability, "RETRYABLE_AFTER_HOST_REPAIR")
        self.assertIn(
            "report_format",
            {check.identifier for check in result.checks if check.outcome == "FAIL"},
        )
        self.assertIn(
            "provider_support",
            {check.identifier for check in result.checks if check.outcome == "FAIL"},
        )
