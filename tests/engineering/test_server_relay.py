from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import server_relay


class ServerRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "central"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_binary_path_is_installation_owned_not_checkout_owned(self) -> None:
        self.assertEqual(
            server_relay.relay_binary(self.root),
            self.root.resolve() / "runtime" / "engineering-dashboard-relay",
        )

    def test_install_uses_the_canonical_component_label_and_server_paths(self) -> None:
        binary = self.root / "runtime" / "engineering-dashboard-relay"
        plist = self.root / "LaunchAgents" / "com.djconnect.engineering-dashboard-relay.plist"
        with patch("engineering_platform.server_relay.build_relay", return_value=binary), patch(
            "engineering_platform.server_relay.render_launch_agent", return_value=plist
        ), patch("engineering_platform.server_relay.LaunchdProvider") as launchd:
            result = server_relay.install(self.root)
        launchd.return_value.install.assert_called_once_with("com.djconnect.engineering-dashboard-relay", plist)
        self.assertEqual(result["component"], "dashboard_relay")
        self.assertEqual(result["binary"], str(binary))

    def test_uninstall_removes_only_the_relay_launch_agent(self) -> None:
        plist = self.root / "LaunchAgents" / "com.djconnect.engineering-dashboard-relay.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("relay", encoding="utf-8")
        with patch("engineering_platform.server_relay.launch_agent_path", return_value=plist), patch(
            "engineering_platform.server_relay.LaunchdProvider"
        ) as launchd:
            result = server_relay.uninstall()
        launchd.return_value.uninstall.assert_called_once_with(plist)
        self.assertFalse(plist.exists())
        self.assertEqual(result["component"], "dashboard_relay")
