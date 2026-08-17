"""Regression coverage for the Engineering Platform coverage quality gate."""

from __future__ import annotations

from pathlib import Path
import unittest


class EngineeringPlatformCoverageContractTests(unittest.TestCase):
    """Keep the CI contract aligned with the explicit quality threshold."""

    def test_coverage_gate_requires_strictly_more_than_eighty_percent(self) -> None:
        workflow = Path(".github/workflows/engineering-platform-validation.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("minimum = 80.20", workflow)
        self.assertIn('minimum 80.20%', workflow)
        self.assertIn("covered is None or covered < minimum", workflow)
