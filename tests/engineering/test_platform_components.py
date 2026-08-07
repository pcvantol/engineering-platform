from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering import platform_bootstrap
from tools.engineering.platform_api import PlatformConfigurationError
from tools.engineering.providers import (
    CodexCliProvider,
    GitProvider,
    GitHubProvider,
    ICloudInboxProvider,
    LaunchdProvider,
    TailscaleProvider,
    registry,
)


class PlatformBootstrapTest(unittest.TestCase):
    @patch("tools.engineering.platform_bootstrap.PlatformConfiguration.load")
    def test_workspace_provisioning_is_idempotent_and_private(self, load: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = platform_bootstrap.provision_workspace(Path(temporary))
            repeated = platform_bootstrap.provision_workspace(Path(temporary))

            self.assertEqual(paths, repeated)
            self.assertTrue(all(path.is_dir() for path in paths.values()))
            load.assert_called()

    @patch("tools.engineering.platform_bootstrap.PlatformConfiguration.load", return_value="configuration")
    def test_repository_validation_requires_bootstrap_and_git(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(PlatformConfigurationError):
                platform_bootstrap.validate_repository(root)
            (root / "BOOTSTRAP.md").write_text("contract", encoding="utf-8")
            (root / ".git").mkdir()
            self.assertEqual(platform_bootstrap.validate_repository(root), "configuration")

    def test_template_rendering_is_valid_and_never_overwrites_existing_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "nested" / "workspace.json"
            platform_bootstrap.render_template(destination, {"$WORKSPACE_NAME": "Test"})
            self.assertTrue(destination.is_file())
            json.loads(destination.read_text(encoding="utf-8"))
            destination.write_text('{"kept":true}\n', encoding="utf-8")
            self.assertEqual(platform_bootstrap.render_template(destination, {}), destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), '{"kept":true}\n')


class ProviderContractTest(unittest.TestCase):
    @patch("tools.engineering.providers.shutil.which", return_value=None)
    def test_unavailable_runtime_and_service_providers_are_unqualified(self, _: object) -> None:
        self.assertFalse(CodexCliProvider().status().qualified)
        self.assertFalse(LaunchdProvider().status().qualified)
        self.assertFalse(TailscaleProvider().status().qualified)

    @patch("tools.engineering.providers.subprocess.run")
    @patch("tools.engineering.providers.shutil.which", return_value="/usr/local/bin/tailscale")
    def test_tailscale_accepts_only_its_routable_ipv4_range(self, _: object, run: object) -> None:
        run.return_value = __import__("subprocess").CompletedProcess(
            ("tailscale",), 0, "127.0.0.1\n100.100.100.100\nnot-an-ip\n", ""
        )
        self.assertEqual(TailscaleProvider().ipv4_address(), "100.100.100.100")
        run.return_value = __import__("subprocess").CompletedProcess(("tailscale",), 1, "", "")
        self.assertIsNone(TailscaleProvider().ipv4_address())
        self.assertFalse(TailscaleProvider().status().qualified)

    @patch("tools.engineering.providers.subprocess.run")
    def test_github_provider_contract_reports_and_raises_failures(self, run: object) -> None:
        root = Path("/repository")
        run.return_value = __import__("subprocess").CompletedProcess(("git",), 0, "git@github.com:pcvantol/djconnect.git\n", "")
        provider = GitHubProvider()
        self.assertTrue(provider.status(root).qualified)
        self.assertEqual(
            GitProvider().execute(root, "git", "status").stdout.strip(),
            "git@github.com:pcvantol/djconnect.git",
        )
        self.assertEqual(
            GitProvider().command(root, "git", "status"),
            "git@github.com:pcvantol/djconnect.git",
        )
        self.assertEqual(provider.github("pr", "view"), "git@github.com:pcvantol/djconnect.git")
        run.return_value = __import__("subprocess").CompletedProcess(("git",), 1, "", "failed")
        self.assertEqual(GitProvider().execute(root, "git", "status").returncode, 1)
        with self.assertRaisesRegex(RuntimeError, "failed"):
            GitProvider().command(root, "git", "status")
        with self.assertRaisesRegex(RuntimeError, "failed"):
            provider.github("pr", "view")

    @patch("tools.engineering.providers.subprocess.run")
    def test_launchd_install_and_uninstall_use_owned_plist(self, run: object) -> None:
        plist = Path("/tmp/com.example.plist")
        provider = LaunchdProvider()
        provider.install("com.example", plist)
        provider.uninstall(plist)
        self.assertEqual(run.call_count, 3)

    @patch("tools.engineering.providers.TailscaleProvider.status")
    @patch("tools.engineering.providers.LaunchdProvider.status")
    @patch("tools.engineering.providers.GitHubProvider.status")
    @patch("tools.engineering.providers.CodexCliProvider.status")
    def test_registry_has_the_complete_provider_boundary(
        self, codex: object, github: object, launchd: object, tailscale: object
    ) -> None:
        for mocked, name in ((codex, "codex_cli"), (github, "github"), (launchd, "launchd"), (tailscale, "tailscale")):
            mocked.return_value = __import__("tools.engineering.providers", fromlist=["ProviderStatus"]).ProviderStatus(name, "configured", True, "ok")
        providers = registry(Path("/repository"))
        self.assertEqual(set(providers), {"runtime", "repository", "service_manager", "remote_submission", "private_remote_access"})
        self.assertTrue(ICloudInboxProvider().status().qualified)
