from __future__ import annotations

from pathlib import Path
import unittest


class LocalDataRetirementTest(unittest.TestCase):
    def test_server_console_has_no_checkout_local_snapshot_fallback(self) -> None:
        source = (Path(__file__).parents[2] / "src" / "engineering_platform" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("server_console_services._sse_snapshot", source)
        self.assertIn("_central_console_project_snapshot(self.server.data_root, project_id)", source)
        references = [line.strip() for line in source.splitlines() if "server_console_services." in line]
        self.assertEqual(len(references), 2)
        self.assertTrue(all("_dashboard_html" in line for line in references))

    def test_runtime_resolver_has_no_dashboard_configuration_or_inbox_override(self) -> None:
        source = (Path(__file__).parents[2] / "src" / "engineering_platform" / "platform_api.py").read_text(encoding="utf-8")
        self.assertNotIn("dashboard_configuration", source)
        self.assertNotIn("DJCONNECT_ENGINEERING_INBOX", source)
        self.assertFalse((Path(__file__).parents[2] / "src" / "engineering_platform" / "dashboard_configuration.py").exists())

    def test_retirement_and_configuration_inventories_are_complete(self) -> None:
        docs = Path(__file__).parents[2] / "docs" / "engineering"
        data = (docs / "LOCAL_DATA_RETIREMENT_MATRIX.md").read_text(encoding="utf-8")
        configuration = (docs / "CONFIGURATION_MIGRATION_MATRIX.md").read_text(encoding="utf-8")
        for classification in ("CENTRAL_OPERATIONAL_AUTHORITY", "HOST_DIAGNOSTIC_STATE", "HISTORICAL_RETIREMENT_INPUT", "DELETE"):
            self.assertIn(classification, data)
        for classification in ("SERVER", "PROJECT", "INSTALLATION", "HISTORICAL"):
            self.assertIn(classification, configuration)
        self.assertIn("no Server route or event stream reads them", data)
        self.assertIn("never operational authority", data)
