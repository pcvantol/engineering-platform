from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys
import unittest
from unittest.mock import patch

from engineering_platform import server_service


class ServerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / "instance"
        self.home = Path(self.temporary.name) / "home"
        self.root.mkdir()
        (self.root / "server.json").write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def test_payload_runs_foreground_server_from_absolute_interpreter(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        payload = server_service.plist_payload(paths, Path("/runtime/bin/python"))
        self.assertEqual(payload["Label"], server_service.LABEL)
        self.assertEqual(payload["ProgramArguments"], ["/runtime/bin/python", "-m", "engineering_platform.server", "serve", "--data-root", str(self.root.resolve())])
        self.assertEqual(payload["WorkingDirectory"], str(self.root.resolve()))
        self.assertNotIn("PYTHONPATH", payload["EnvironmentVariables"])

    def test_install_is_idempotent_and_writes_only_owned_plist(self) -> None:
        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            result = server_service.install(self.root, interpreter=Path(sys.executable), home=self.home, runner=self.runner)
        self.assertEqual(result["label"], server_service.LABEL)
        self.assertTrue((self.home / "Library" / "LaunchAgents" / f"{server_service.LABEL}.plist").is_file())

    def test_uninitialized_data_root_fails_closed(self) -> None:
        with self.assertRaisesRegex(server_service.ServerServiceError, "initialized"):
            server_service.install(self.root.parent / "missing", interpreter=Path(__file__).resolve(), home=self.home, runner=self.runner)

    def test_uninstall_boots_out_only_the_owned_agent(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        paths.launch_agents_dir.mkdir(parents=True)
        paths.plist_path.write_text("owned", encoding="utf-8")
        calls: list[list[str]] = []

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            result = server_service.uninstall(self.root, home=self.home, runner=runner)
        self.assertEqual(result["state"], "uninstalled")
        self.assertFalse(paths.plist_path.exists())
        self.assertEqual(calls, [["launchctl", "bootout", f"gui/{server_service.os.getuid()}", str(paths.plist_path)]])

    def test_launchagent_failures_and_non_macos_fail_closed(self) -> None:
        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            def failed(arguments: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(arguments, 1, "", "permission denied")
            with self.assertRaisesRegex(server_service.ServerServiceError, "Unable to bootstrap"):
                server_service.install(self.root, interpreter=Path(sys.executable), home=self.home, runner=failed)
        with patch("engineering_platform.server_service.platform.system", return_value="Linux"):
            with self.assertRaisesRegex(server_service.ServerServiceError, "macOS"):
                server_service.uninstall(self.root, home=self.home, runner=self.runner)
