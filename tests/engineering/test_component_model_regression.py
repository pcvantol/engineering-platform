from __future__ import annotations

from pathlib import Path
import unittest

from engineering_platform.platform_components import PLATFORM_COMPONENT_IDS


class ComponentModelRegressionTest(unittest.TestCase):
    def test_supported_component_inventory_is_the_canonical_model(self) -> None:
        root = Path(__file__).parents[2]
        server = (root / "src" / "engineering_platform" / "server.py").read_text(encoding="utf-8")
        logging = (root / "src" / "engineering_platform" / "component_logging.py").read_text(encoding="utf-8")
        self.assertIn("PLATFORM_COMPONENT_IDS", server)
        self.assertIn("PLATFORM_COMPONENT_IDS", logging)
        self.assertNotIn('{"inbox", "dashboard"}', logging)
        self.assertNotIn('"dashboard":', server)
        self.assertNotIn('"inbox":', server)
        self.assertIn("operations_console", PLATFORM_COMPONENT_IDS)
        self.assertIn("file_inbox_ingress", PLATFORM_COMPONENT_IDS)

    def test_legacy_aliases_cannot_be_component_authority(self) -> None:
        root = Path(__file__).parents[2]
        services = (root / "src" / "engineering_platform" / "server_console_services.py").read_text(encoding="utf-8")
        logging = (root / "src" / "engineering_platform" / "component_logging.py").read_text(encoding="utf-8")
        for source in (services, logging):
            self.assertNotIn("COMPONENT_LABELS", source)
            self.assertNotIn("RESTARTABLE_COMPONENTS", source)
            self.assertNotIn('"inbox", "dashboard"', source)
        self.assertNotIn('"dashboard":', services)

    def test_console_actions_require_canonical_server_capability(self) -> None:
        root = Path(__file__).parents[2]
        server = (root / "src" / "engineering_platform" / "server.py").read_text(encoding="utf-8")
        routes = (root / "src" / "engineering_platform" / "console_route_ownership.py").read_text(encoding="utf-8")
        self.assertIn("restart_supported", server)
        self.assertIn("PLATFORM_COMPONENT_ROUTE_PATTERN", routes)
        self.assertNotIn("/api/components/dashboard/restart", routes)
