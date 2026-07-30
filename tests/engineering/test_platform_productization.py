from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from tools.engineering.platform_api import PlatformConfiguration, capabilities, provider_registry
from tools.engineering.platform_bootstrap import provision_workspace


ROOT = Path(__file__).resolve().parents[2]


class PlatformProductizationTest(unittest.TestCase):
    def test_identity_and_configuration_are_canonical(self) -> None:
        configuration = PlatformConfiguration.load(ROOT)
        self.assertEqual(configuration.platform.id, "engineering-platform")
        self.assertEqual(configuration.platform.version, "1.5.0")
        self.assertEqual(configuration.workspace.id, "djconnect")
        self.assertEqual(configuration.providers["runtime"], "codex_cli")

    def test_public_api_has_all_productization_capabilities(self) -> None:
        registered = set(capabilities())
        self.assertTrue({"runner", "runtime_provider", "repository_provider", "service_manager_provider", "remote_submission_provider", "private_remote_access_provider"} <= registered)

    def test_provider_registry_is_configuration_backed(self) -> None:
        providers = provider_registry(ROOT)
        self.assertEqual(providers["repository"]["selected"], "github")
        self.assertIn("status", providers["runtime"])

    def test_workspace_provisioning_is_idempotent(self) -> None:
        first = provision_workspace(ROOT)
        second = provision_workspace(ROOT)
        self.assertEqual(first, second)
        self.assertTrue(first["status"].is_dir())

    def test_unknown_local_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "tools/engineering"
            target.mkdir(parents=True)
            (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text((ROOT / "tools/engineering/ENGINEERING_PLATFORM_CONFIG.json").read_text())
            local = root / ".djconnect"
            local.mkdir()
            (local / "engineering-platform.local.json").write_text(json.dumps({"providers": {"runtime": "other"}}))
            with self.assertRaises(ValueError):
                PlatformConfiguration.load(root)
