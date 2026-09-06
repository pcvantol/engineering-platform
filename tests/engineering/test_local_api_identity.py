"""Retirement coverage for the historical Local Consumer API service."""

from __future__ import annotations

from pathlib import Path
import unittest

from engineering_platform import central_store_migration, local_api
from engineering_platform.platform_components import PLATFORM_COMPONENT_IDS


class LocalApiRetirementTests(unittest.TestCase):
    def test_historical_labels_have_no_active_service_surface(self) -> None:
        status = local_api.retirement_status()
        self.assertEqual(status["state"], "RETIRED")
        self.assertEqual(
            status["historical_labels"],
            (
                "com.djconnect.engineering-local-api",
                "com.engineeringplatform.local-api",
            ),
        )
        self.assertFalse(status["installable"])
        self.assertFalse(status["lifecycle_owned"])
        self.assertFalse(status["supported_submission_ingress"])
        for name in (
            "run", "install", "uninstall", "launch_agent", "launch_agent_path",
            "legacy_launch_agent_path", "local_api_functional_probe", "main",
        ):
            self.assertFalse(hasattr(local_api, name), name)

    def test_local_api_is_not_a_component_or_central_migration_service(self) -> None:
        self.assertNotIn("local_api", PLATFORM_COMPONENT_IDS)
        labels = {
            "com.djconnect.engineering-local-api",
            "com.engineeringplatform.local-api",
        }
        self.assertTrue(labels.isdisjoint(central_store_migration.SERVICE_STOP_ORDER))
        self.assertTrue(labels.isdisjoint(central_store_migration.SERVICE_START_ORDER))

    def test_repository_has_no_local_api_launchagent_definition(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (root / "src" / "engineering_platform" / "local_api.py").read_text(
            encoding="utf-8"
        )
        for active_surface in (
            "LaunchdProvider", "RunAtLoad", "KeepAlive", "<plist", "def install(",
            "def uninstall(", "def run(",
        ):
            self.assertNotIn(active_surface, source)
