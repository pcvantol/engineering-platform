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

    def test_browser_dashboard_validation_uses_four_parallel_shards(self) -> None:
        workflow = Path(".github/workflows/engineering-platform-validation.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('shard: "1/4"', workflow)
        self.assertIn('shard: "4/4"', workflow)
        self.assertIn("max-parallel: 4", workflow)
        self.assertIn("--shard=${{ matrix.shard }}", workflow)
        self.assertIn("engineering-status-browser-screenshots-${{ matrix.artifact_suffix }}", workflow)

    def test_browser_ci_parity_contract_is_documented(self) -> None:
        supervisor = Path(
            "docs/engineering/LOCAL_DASHBOARD_SUPERVISOR.md"
        ).read_text(encoding="utf-8")
        design_system = Path(
            "src/engineering_platform/OPERATIONS_CONSOLE_DESIGN_SYSTEM.md"
        ).read_text(encoding="utf-8")

        self.assertIn("CI-pariteit en reproduceerbare browserfouten", supervisor)
        self.assertIn('CI=1 npm run test:engineering-dashboard -- --reporter=dot --shard="$shard"', supervisor)
        self.assertIn("Linux Chromium", supervisor)
        self.assertIn("CI-parity contract", design_system)
