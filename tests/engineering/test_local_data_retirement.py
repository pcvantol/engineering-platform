from __future__ import annotations

from pathlib import Path
import ast
import unittest


class LocalDataRetirementTest(unittest.TestCase):
    def test_server_console_has_no_checkout_local_snapshot_fallback(self) -> None:
        source = (Path(__file__).parents[2] / "src" / "engineering_platform" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("server_console_services._sse_snapshot", source)
        self.assertNotIn("server_console_services._dashboard_html", source)
        self.assertNotIn("historical_dashboard_configuration", source)
        # The owning SSE route must use the guarded CENTRAL reader, including
        # its repository and instance publication binding. A direct-reader
        # literal predates that authority fence and is no longer the contract.
        tree = ast.parse(source)
        stream = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef) and node.name == "_stream_project_console_events")
        reads = [node for node in ast.walk(stream) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_console_project_read"]
        self.assertGreaterEqual(len(reads), 2)
        for read in reads:
            self.assertEqual([ast.unparse(arg) for arg in read.args],
                             ["data_root", "project_id", "_central_console_project_snapshot", "repository_id", "identity"])
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                             and node.func.id == "_central_console_project_snapshot"
                             for node in ast.walk(stream)))
        references = [line.strip() for line in source.splitlines() if "server_console_services." in line]
        self.assertEqual(len(references), 3)
        self.assertEqual(sum("render_console_document" in line for line in references), 2)
        self.assertEqual(sum("_checkpoint_pull_request_context(checkpoint)" in line for line in references), 1)

    def test_supported_console_cannot_import_historical_dashboard_configuration(self) -> None:
        source = (Path(__file__).parents[2] / "src" / "engineering_platform" / "server_console_services.py").read_text(encoding="utf-8")
        self.assertNotIn("historical_dashboard_configuration", source)
        self.assertNotIn("dashboard_configuration(", source)
        self.assertNotIn("update_dashboard_configuration", source)

    def test_runtime_resolver_has_no_dashboard_configuration_or_inbox_override(self) -> None:
        source = (Path(__file__).parents[2] / "src" / "engineering_platform" / "platform_api.py").read_text(encoding="utf-8")
        self.assertNotIn("dashboard_configuration", source)
        self.assertNotIn("ENGINEERING_PLATFORM_INBOX", source)
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
