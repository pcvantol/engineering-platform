"""Fail-closed identity migration coverage for the loopback Local API."""

from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engineering_platform import central_store_migration, local_api


class LocalApiIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _install(self, launchd: "FakeLaunchd", *, legacy_plist: bool = True, probe=True) -> Path:
        home = self.repo.parent / "home"
        legacy = home / "Library" / "LaunchAgents" / f"{local_api.LEGACY_LABEL}.plist"
        if legacy_plist:
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text("legacy", encoding="utf-8")
        with patch("engineering_platform.local_api.Path.home", return_value=home), patch(
            "engineering_platform.local_api.LaunchdProvider", return_value=launchd
        ):
            functional_probe = probe if callable(probe) else lambda _port: probe
            return local_api.install(self.repo, probe=functional_probe)

    def test_new_install_and_central_lifecycle_order_use_only_the_neutral_label(self) -> None:
        launchd = FakeLaunchd()
        plist = self._install(launchd, legacy_plist=False)
        self.assertEqual(plist.name, "com.engineeringplatform.local-api.plist")
        self.assertEqual(launchd.loaded, {local_api.LABEL})
        self.assertIn(local_api.LABEL, central_store_migration.SERVICE_STOP_ORDER)
        self.assertIn(local_api.LABEL, central_store_migration.SERVICE_START_ORDER)
        self.assertNotIn(local_api.LEGACY_LABEL, central_store_migration.SERVICE_STOP_ORDER)
        self.assertNotIn(local_api.LEGACY_LABEL, central_store_migration.SERVICE_START_ORDER)

    def test_loaded_legacy_is_verified_absent_before_neutral_bootstrap(self) -> None:
        launchd = FakeLaunchd({local_api.LEGACY_LABEL}, sticky={local_api.LEGACY_LABEL})
        with self.assertRaisesRegex(local_api.LocalApiCutoverError, "LEGACY_UNLOAD_UNVERIFIED"):
            self._install(launchd)
        self.assertEqual(launchd.loaded, {local_api.LEGACY_LABEL})
        self.assertEqual(launchd.installs, [])

    def test_neutral_bootstrap_failure_restores_the_loaded_legacy_state(self) -> None:
        launchd = FakeLaunchd({local_api.LEGACY_LABEL}, install_error_for=local_api.LABEL)
        with self.assertRaisesRegex(local_api.LocalApiCutoverError, "LOCAL_API_CUTOVER_ROLLED_BACK"):
            self._install(launchd)
        self.assertEqual(launchd.loaded, {local_api.LEGACY_LABEL})

    def test_neutral_plist_staging_failure_restores_the_loaded_legacy_state(self) -> None:
        home = self.repo.parent / "staging-home"
        legacy = home / "Library" / "LaunchAgents" / f"{local_api.LEGACY_LABEL}.plist"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy", encoding="utf-8")
        launchd = FakeLaunchd({local_api.LEGACY_LABEL})
        with patch("engineering_platform.local_api.Path.home", return_value=home), patch(
            "engineering_platform.local_api.LaunchdProvider", return_value=launchd
        ), patch("engineering_platform.local_api.launch_agent", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(local_api.LocalApiCutoverError, "LOCAL_API_CUTOVER_ROLLED_BACK"):
                local_api.install(self.repo, probe=lambda _port: True)
        self.assertEqual(launchd.loaded, {local_api.LEGACY_LABEL})

    def test_neutral_process_or_health_failure_rolls_back_before_retiring_legacy(self) -> None:
        for launchd, probe in (
            (FakeLaunchd({local_api.LEGACY_LABEL}, inactive={local_api.LABEL}), True),
            (FakeLaunchd({local_api.LEGACY_LABEL}), False),
        ):
            with self.assertRaisesRegex(local_api.LocalApiCutoverError, "LOCAL_API_CUTOVER_ROLLED_BACK"):
                self._install(launchd, probe=probe)
            self.assertEqual(launchd.loaded, {local_api.LEGACY_LABEL})

    def test_rollback_keeps_a_previously_inactive_legacy_plist_inactive(self) -> None:
        launchd = FakeLaunchd(install_error_for=local_api.LABEL)
        with self.assertRaisesRegex(local_api.LocalApiCutoverError, "LOCAL_API_CUTOVER_ROLLED_BACK"):
            self._install(launchd)
        self.assertNotIn(local_api.LEGACY_LABEL, launchd.loaded)

    def test_rollback_never_restores_legacy_until_neutral_is_proven_absent(self) -> None:
        launchd = FakeLaunchd(
            {local_api.LEGACY_LABEL}, install_error_after_load_for=local_api.LABEL, sticky={local_api.LABEL}
        )
        with self.assertRaisesRegex(local_api.LocalApiCutoverError, "ROLLBACK_NEUTRAL_UNLOAD_UNVERIFIED"):
            self._install(launchd)
        self.assertEqual(launchd.loaded, {local_api.LABEL})

    def test_existing_neutral_state_is_denied_without_touching_legacy(self) -> None:
        launchd = FakeLaunchd({local_api.LABEL})
        with self.assertRaisesRegex(local_api.LocalApiCutoverError, "NEUTRAL_ALREADY_PRESENT"):
            self._install(launchd)
        self.assertEqual(launchd.loaded, {local_api.LABEL})


class FakeLaunchd:
    def __init__(
        self, loaded=(), *, sticky=frozenset(), inactive=frozenset(), install_error_for=None,
        install_error_after_load_for=None,
    ) -> None:
        self.loaded = set(loaded)
        self.sticky = set(sticky)
        self.inactive = set(inactive)
        self.install_error_for = install_error_for
        self.install_error_after_load_for = install_error_after_load_for
        self.installs: list[tuple[str, Path]] = []

    def inspect(self, label: str) -> bool:
        return label in self.loaded

    def runtime_status(self, label: str) -> SimpleNamespace:
        return SimpleNamespace(qualified=label in self.loaded and label not in self.inactive)

    def uninstall(self, plist: Path) -> None:
        if plist.stem not in self.sticky:
            self.loaded.discard(plist.stem)

    def install(self, label: str, plist: Path) -> None:
        self.installs.append((label, plist))
        if label == self.install_error_for:
            raise OSError("bootstrap failed")
        self.loaded.add(label)
        if label == self.install_error_after_load_for:
            raise OSError("bootstrap uncertain")
