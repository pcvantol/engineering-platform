from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from types import SimpleNamespace

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
        plist = self.root / "LaunchAgents" / "com.engineeringplatform.dashboard-relay.plist"
        launchd = FakeLaunchd()
        with patch("engineering_platform.server_relay.build_relay", return_value=binary), patch(
            "engineering_platform.server_relay.render_launch_agent", return_value=plist
        ), patch("engineering_platform.server_relay.LaunchdProvider", return_value=launchd), patch(
            "engineering_platform.server_relay.pre_cutover_state",
            return_value=server_relay.PreCutoverState(False, False, False, None),
        ):
            result = server_relay.install(self.root, probe=lambda: True)
        self.assertEqual(launchd.installs, [("com.engineeringplatform.dashboard-relay", plist)])
        self.assertEqual(result["component"], "dashboard_relay")
        self.assertEqual(result["binary"], str(binary))

    def test_uninstall_removes_only_the_relay_launch_agent(self) -> None:
        plist = self.root / "LaunchAgents" / "com.engineeringplatform.dashboard-relay.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("relay", encoding="utf-8")
        with patch("engineering_platform.server_relay.launch_agent_path", return_value=plist), patch(
            "engineering_platform.server_relay.LaunchdProvider"
        ) as launchd:
            result = server_relay.uninstall()
        launchd.return_value.uninstall.assert_called_once_with(plist)
        self.assertFalse(plist.exists())
        self.assertEqual(result["component"], "dashboard_relay")

    def test_install_migrates_the_single_legacy_relay_without_dual_authority(self) -> None:
        home = self.root.parent / "home"
        legacy = home / "Library" / "LaunchAgents" / "com.djconnect.engineering-dashboard-relay.plist"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy", encoding="utf-8")
        binary = self.root / "runtime" / "engineering-dashboard-relay"
        neutral = home / "Library" / "LaunchAgents" / "com.engineeringplatform.dashboard-relay.plist"
        launchd = FakeLaunchd({server_relay.LEGACY_RELAY_LABEL})
        with patch("engineering_platform.server_relay.Path.home", return_value=home), patch(
            "engineering_platform.server_relay.build_relay", return_value=binary
        ), patch("engineering_platform.server_relay.render_launch_agent", return_value=neutral), patch(
            "engineering_platform.server_relay.LaunchdProvider", return_value=launchd
        ), patch("engineering_platform.server_relay.relay_functional_probe", return_value=True):
            server_relay.install(self.root, probe=lambda: True)
        self.assertNotIn(server_relay.LEGACY_RELAY_LABEL, launchd.loaded)
        self.assertIn("com.engineeringplatform.dashboard-relay", launchd.loaded)
        self.assertFalse(legacy.exists())

    def _cutover(self, launchd: "FakeLaunchd", *, legacy_plist: bool = True, probe=True):
        home = self.root.parent / "cutover-home"
        legacy = home / "Library" / "LaunchAgents" / f"{server_relay.LEGACY_RELAY_LABEL}.plist"
        neutral = home / "Library" / "LaunchAgents" / "com.engineeringplatform.dashboard-relay.plist"
        if legacy_plist:
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text("legacy", encoding="utf-8")
        neutral.parent.mkdir(parents=True, exist_ok=True)
        neutral.write_text("neutral", encoding="utf-8")
        binary = self.root / "runtime" / "engineering-dashboard-relay"
        with patch("engineering_platform.server_relay.Path.home", return_value=home), patch(
            "engineering_platform.server_relay.build_relay", return_value=binary
        ), patch("engineering_platform.server_relay.render_launch_agent", return_value=neutral), patch(
            "engineering_platform.server_relay.LaunchdProvider", return_value=launchd
        ):
            return server_relay.install(self.root, probe=(lambda: probe))

    def test_denies_neutral_bootstrap_when_legacy_bootout_raises(self) -> None:
        launchd = FakeLaunchd({server_relay.LEGACY_RELAY_LABEL}, uninstall_error_for=server_relay.LEGACY_RELAY_LABEL)
        with self.assertRaises(OSError):
            self._cutover(launchd)
        self.assertNotIn("com.engineeringplatform.dashboard-relay", launchd.loaded)
        self.assertIn(server_relay.LEGACY_RELAY_LABEL, launchd.loaded)

    def test_denies_neutral_bootstrap_when_legacy_remains_loaded(self) -> None:
        launchd = FakeLaunchd({server_relay.LEGACY_RELAY_LABEL}, sticky={server_relay.LEGACY_RELAY_LABEL})
        with self.assertRaisesRegex(server_relay.RelayCutoverError, "LEGACY_UNLOAD_UNVERIFIED"):
            self._cutover(launchd)
        self.assertNotIn("com.engineeringplatform.dashboard-relay", launchd.loaded)

    def test_denies_cutover_when_neutral_is_already_loaded(self) -> None:
        neutral = "com.engineeringplatform.dashboard-relay"
        launchd = FakeLaunchd({neutral})
        with self.assertRaisesRegex(server_relay.RelayCutoverError, "NEUTRAL_ALREADY_LOADED"):
            self._cutover(launchd)
        self.assertEqual(launchd.loaded, {neutral})

    def test_denies_cutover_when_neutral_inspection_is_ambiguous(self) -> None:
        neutral = "com.engineeringplatform.dashboard-relay"
        launchd = Mock()
        launchd.inspect.side_effect = lambda label: Mock() if label == neutral else False
        with self.assertRaisesRegex(server_relay.RelayCutoverError, "NEUTRAL_ALREADY_LOADED"):
            self._cutover(launchd)
        launchd.install.assert_not_called()

    def test_neutral_bootstrap_failure_restores_loaded_legacy(self) -> None:
        launchd = FakeLaunchd({server_relay.LEGACY_RELAY_LABEL}, install_error_for="com.engineeringplatform.dashboard-relay")
        with self.assertRaisesRegex(server_relay.RelayCutoverError, "RELAY_CUTOVER_ROLLED_BACK"):
            self._cutover(launchd)
        self.assertEqual(launchd.loaded, {server_relay.LEGACY_RELAY_LABEL})

    def test_inactive_neutral_process_rolls_back(self) -> None:
        launchd = FakeLaunchd({server_relay.LEGACY_RELAY_LABEL}, inactive={"com.engineeringplatform.dashboard-relay"})
        with self.assertRaisesRegex(server_relay.RelayCutoverError, "RELAY_CUTOVER_ROLLED_BACK"):
            self._cutover(launchd)
        self.assertEqual(launchd.loaded, {server_relay.LEGACY_RELAY_LABEL})

    def test_failed_neutral_functional_probe_rolls_back(self) -> None:
        launchd = FakeLaunchd({server_relay.LEGACY_RELAY_LABEL})
        with self.assertRaisesRegex(server_relay.RelayCutoverError, "RELAY_CUTOVER_ROLLED_BACK"):
            self._cutover(launchd, probe=False)
        self.assertEqual(launchd.loaded, {server_relay.LEGACY_RELAY_LABEL})

    def test_rollback_does_not_restore_legacy_when_neutral_unload_is_unverified(self) -> None:
        neutral = "com.engineeringplatform.dashboard-relay"
        launchd = FakeLaunchd({server_relay.LEGACY_RELAY_LABEL}, install_error_after_load_for=neutral, sticky={neutral})
        with self.assertRaisesRegex(server_relay.RelayCutoverError, "ROLLBACK_NEUTRAL_UNLOAD_UNVERIFIED"):
            self._cutover(launchd)
        self.assertNotIn(server_relay.LEGACY_RELAY_LABEL, launchd.loaded)
        self.assertIn(neutral, launchd.loaded)

    def test_rollback_keeps_previously_inactive_legacy_inactive(self) -> None:
        launchd = FakeLaunchd(set(), install_error_for="com.engineeringplatform.dashboard-relay")
        with self.assertRaisesRegex(server_relay.RelayCutoverError, "RELAY_CUTOVER_ROLLED_BACK"):
            self._cutover(launchd, legacy_plist=True)
        self.assertNotIn(server_relay.LEGACY_RELAY_LABEL, launchd.loaded)


class FakeLaunchd:
    def __init__(self, loaded=(), *, sticky=frozenset(), inactive=frozenset(), uninstall_error_for=None, install_error_for=None, install_error_after_load_for=None):
        self.loaded = set(loaded)
        self.sticky = set(sticky)
        self.inactive = set(inactive)
        self.uninstall_error_for = uninstall_error_for
        self.install_error_for = install_error_for
        self.install_error_after_load_for = install_error_after_load_for
        self.installs = []

    def inspect(self, label): return label in self.loaded
    def runtime_status(self, label): return SimpleNamespace(qualified=label in self.loaded and label not in self.inactive)
    def uninstall(self, plist):
        label = plist.stem
        if label == self.uninstall_error_for: raise OSError("bootout failed")
        if label not in self.sticky: self.loaded.discard(label)
    def install(self, label, plist):
        self.installs.append((label, plist))
        if label == self.install_error_for: raise OSError("bootstrap failed")
        self.loaded.add(label)
        if label == self.install_error_after_load_for: raise OSError("bootstrap uncertain")
