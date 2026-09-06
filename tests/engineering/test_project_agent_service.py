from __future__ import annotations

import json
import stat
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import project_agent_service as service
from engineering_platform.server import default_data_root


def successful(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, "", "")


class ProjectAgentServiceTests(unittest.TestCase):
    def paths(self, temporary: str) -> service.AgentPaths:
        return service.default_paths(Path(temporary) / "home")

    def executable(self, temporary: str) -> Path:
        path = Path(temporary) / "installed" / "bin" / "engineering-project-agent"
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_user_level_macos_paths_and_server_coexistence(self) -> None:
        paths = self.paths("/tmp/example")
        self.assertEqual(paths.launch_agents_dir, Path("/tmp/example/home/Library/LaunchAgents"))
        self.assertIn("Application Support/Engineering Platform/Project Agent", str(paths.config_path))
        with patch("engineering_platform.server.Path.home", return_value=Path("/tmp/example/home")), patch("engineering_platform.server.sys.platform", "darwin"):
            self.assertNotEqual(paths.config_dir, default_data_root())

    def test_plist_is_deterministic_and_has_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.paths(temporary)
            config = service.initialize(paths)
            payload = service.plist_payload(paths, self.executable(temporary), config)
            encoded = service.plist_text(payload)
        self.assertEqual(payload["Label"], service.LABEL)
        self.assertNotIn("secret", encoded.lower())
        self.assertNotIn("password", encoded.lower())
        self.assertEqual(payload["StandardOutPath"], "/dev/null")
        self.assertEqual(payload["StandardErrorPath"], "/dev/null")
        self.assertEqual(plistlib.loads(encoded.encode())["ProgramArguments"][0], str(Path(temporary) / "installed/bin/engineering-project-agent"))
        self.assertNotIn("engineering-platform-b6b", encoded)

    @patch("engineering_platform.project_agent_service.platform.system", return_value="Darwin")
    def test_install_writes_private_config_and_non_secret_plist(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths, executable = self.paths(temporary), self.executable(temporary)
            service.install(paths, executable=executable, runner=successful)
            self.assertEqual(stat.S_IMODE(paths.config_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(paths.plist_path.stat().st_mode), 0o644)
            self.assertNotIn("credential_reference", paths.plist_path.read_text(encoding="utf-8"))

    def test_configuration_allows_only_b6a_extension_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.paths(temporary)
            service.initialize(paths)
            paths.config_path.write_text(json.dumps({"version": 1, "toolchain_paths": ["/opt/tool/bin"], "b6a": {"server_endpoint": "https://ep.example", "credential_reference": "keychain://agent"}}), encoding="utf-8")
            loaded = service.AgentConfiguration.load(paths.config_path)
            self.assertEqual(service.runtime_path(loaded), "/opt/tool/bin:" + service.DEFAULT_PATH)
            paths.config_path.write_text(json.dumps({"version": 1, "toolchain_paths": [], "b6a": {"secret": "no"}}), encoding="utf-8")
            with self.assertRaises(service.AgentServiceError):
                service.AgentConfiguration.load(paths.config_path)

    @patch("engineering_platform.project_agent_service.platform.system", return_value="Darwin")
    def test_install_is_idempotent_and_agent_only(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths, executable = self.paths(temporary), self.executable(temporary)
            first = service.install(paths, executable=executable, runner=successful)
            second = service.install(paths, executable=executable, runner=successful)
            self.assertEqual(first["state"], "installed")
            self.assertEqual(second["plist"], str(paths.plist_path))
            self.assertTrue(paths.config_path.exists())
            self.assertEqual(service.status(paths, runner=successful)["state"], "running")

    @patch("engineering_platform.project_agent_service.platform.system", return_value="Darwin")
    def test_start_stop_status_and_uninstall_preservation_boundary(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths, executable = self.paths(temporary), self.executable(temporary)
            service.install(paths, executable=executable, runner=successful)
            repository = Path(temporary) / "repository"
            repository.mkdir()
            (repository / "keep.txt").write_text("keep", encoding="utf-8")
            paths.state_dir.joinpath("transient").write_text("state", encoding="utf-8")
            service.start(paths, runner=successful)
            service.stop(paths, runner=successful)
            service.uninstall(paths, runner=successful)
            self.assertFalse(paths.plist_path.exists())
            self.assertFalse(paths.state_dir.exists())
            self.assertTrue(paths.config_path.exists())
            self.assertTrue((repository / "keep.txt").exists())

    @patch("engineering_platform.project_agent_service.platform.system", return_value="Darwin")
    def test_status_distinguishes_misconfigured_and_stopped(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths, executable = self.paths(temporary), self.executable(temporary)
            service.install(paths, executable=executable, runner=successful)
            stopped = lambda command: subprocess.CompletedProcess(command, 1, "", "not loaded")
            self.assertEqual(service.status(paths, runner=stopped)["state"], "stopped")
            paths.config_path.write_text("{}", encoding="utf-8")
            self.assertEqual(service.status(paths, runner=successful)["state"], "misconfigured")
