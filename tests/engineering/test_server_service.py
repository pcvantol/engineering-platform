from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import plistlib
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

    def loaded_service_output(self, interpreter: Path, data_root: Path | None = None) -> str:
        root = (data_root or self.root).resolve()
        executable = interpreter.absolute()
        return f"""gui/501/{server_service.LABEL} = {{
    program = {executable}
    arguments = {{
        {executable}
        -m
        engineering_platform.server
        serve
        --data-root
        {root}
    }}
    working directory = {root}
}}\n"""

    def test_payload_runs_foreground_server_from_absolute_interpreter(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        payload = server_service.plist_payload(paths, Path("/runtime/bin/python"))
        self.assertEqual(payload["Label"], server_service.LABEL)
        self.assertEqual(payload["ProgramArguments"], ["/runtime/bin/python", "-m", "engineering_platform.server", "serve", "--data-root", str(self.root.resolve())])
        self.assertEqual(payload["WorkingDirectory"], str(self.root.resolve()))
        self.assertEqual(payload["StandardErrorPath"], str(self.root.resolve() / "runtime" / "server-launchagent.err.log"))
        self.assertNotIn("PYTHONPATH", payload["EnvironmentVariables"])

    def test_install_is_idempotent_and_writes_only_owned_plist(self) -> None:
        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            result = server_service.install(self.root, interpreter=Path(sys.executable), home=self.home, runner=self.runner)
        self.assertEqual(result["label"], server_service.LABEL)
        self.assertEqual(result["stderr_log"], str(self.root.resolve() / "runtime" / "server-launchagent.err.log"))
        self.assertTrue((self.home / "Library" / "LaunchAgents" / f"{server_service.LABEL}.plist").is_file())
        self.assertTrue((self.root / "runtime").is_dir())

    def test_installed_interpreter_preserves_virtual_environment_launcher(self) -> None:
        target = Path(sys.executable)
        launcher = Path(self.temporary.name) / "venv-python"
        launcher.symlink_to(target)

        self.assertEqual(server_service._installed_interpreter(launcher), launcher.absolute())

    def test_configured_interpreter_reads_only_the_owned_service_record(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        server_service.write_plist(paths, Path(sys.executable))
        self.assertEqual(server_service.configured_interpreter(self.root, home=self.home), Path(sys.executable).absolute())

    def test_configured_interpreter_rejects_a_service_bound_to_another_data_root(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        server_service.write_plist(paths, Path(sys.executable))
        with paths.plist_path.open("rb") as stream:
            payload = plistlib.load(stream)
        payload["ProgramArguments"][-1] = str(self.root.parent / "other-instance")
        with paths.plist_path.open("wb") as stream:
            plistlib.dump(payload, stream)
        self.assertIsNone(server_service.configured_interpreter(self.root, home=self.home))

    def test_configured_interpreter_rejects_a_different_launchagent_label(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        server_service.write_plist(paths, Path(sys.executable))
        with paths.plist_path.open("rb") as stream:
            payload = plistlib.load(stream)
        payload["Label"] = server_service.LABEL + ".other"
        with paths.plist_path.open("wb") as stream:
            plistlib.dump(payload, stream)
        self.assertIsNone(server_service.configured_interpreter(self.root, home=self.home))

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

    def test_replace_runtime_switches_only_the_expected_owned_interpreter(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        old, new = Path(sys.executable), Path(self.temporary.name) / "replacement-python"
        new.symlink_to(old)
        server_service.write_plist(paths, old)
        calls: list[list[str]] = []

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            output = self.loaded_service_output(new) if arguments[1] == "print" else ""
            return subprocess.CompletedProcess(arguments, 0, output, "")

        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            result = server_service.replace_runtime(self.root, expected_interpreter=old, interpreter=new,
                                                    home=self.home, runner=runner)
        self.assertEqual(result["state"], "replaced")
        self.assertEqual(calls, [
            ["launchctl", "bootout", f"gui/{server_service.os.getuid()}", str(paths.plist_path)],
            ["launchctl", "bootstrap", f"gui/{server_service.os.getuid()}", str(paths.plist_path)],
            ["launchctl", "print", f"gui/{server_service.os.getuid()}/{server_service.LABEL}"],
        ])
        self.assertEqual(server_service.configured_interpreter(self.root, home=self.home), new.absolute())
        calls.clear()
        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            server_service.replace_runtime(self.root, expected_interpreter=old, interpreter=new,
                                           home=self.home, runner=runner)
        self.assertEqual(calls, [
            ["launchctl", "bootstrap", f"gui/{server_service.os.getuid()}", str(paths.plist_path)],
            ["launchctl", "print", f"gui/{server_service.os.getuid()}/{server_service.LABEL}"],
        ])

    def test_retained_update_quiescence_skips_a_second_bootout_and_restores_boot_policy(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        old, new = Path(sys.executable), Path(self.temporary.name) / "replacement-python"
        new.symlink_to(old)
        server_service.write_plist(paths, old)
        retained = server_service.retain_update_quiescence(
            self.root, expected_interpreter=old, home=self.home,
        )
        self.assertEqual(retained["state"], "UPDATE_QUIESCENCE_RETAINED")
        with paths.plist_path.open("rb") as stream:
            maintenance = plistlib.load(stream)
        self.assertIs(maintenance["RunAtLoad"], False)
        self.assertIs(maintenance["KeepAlive"], False)
        calls: list[list[str]] = []
        bootstrapped = False

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal bootstrapped
            calls.append(arguments)
            if arguments[1] == "print":
                if not bootstrapped:
                    return subprocess.CompletedProcess(arguments, 3, "", "Could not find service")
                return subprocess.CompletedProcess(
                    arguments, 0, self.loaded_service_output(new), "",
                )
            if arguments[1] == "bootstrap":
                bootstrapped = True
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            server_service.replace_runtime(
                self.root, expected_interpreter=old, interpreter=new,
                home=self.home, runner=runner,
            )
        self.assertEqual(calls, [
            ["launchctl", "print", f"gui/{server_service.os.getuid()}/{server_service.LABEL}"],
            ["launchctl", "bootstrap", f"gui/{server_service.os.getuid()}", str(paths.plist_path)],
            ["launchctl", "print", f"gui/{server_service.os.getuid()}/{server_service.LABEL}"],
        ])
        with paths.plist_path.open("rb") as stream:
            active = plistlib.load(stream)
        self.assertIs(active["RunAtLoad"], True)
        self.assertEqual(active["KeepAlive"], {"SuccessfulExit": False})

    def test_replace_runtime_unloads_a_login_loaded_maintenance_job(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        old, new = Path(sys.executable), Path(self.temporary.name) / "replacement-python"
        new.symlink_to(old)
        server_service.write_plist(paths, old)
        server_service.retain_update_quiescence(
            self.root, expected_interpreter=old, home=self.home,
        )
        calls: list[list[str]] = []
        bootstrapped = False

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal bootstrapped
            calls.append(arguments)
            output = ""
            if arguments[1] == "print":
                output = self.loaded_service_output(new if bootstrapped else old)
            elif arguments[1] == "bootstrap":
                bootstrapped = True
            return subprocess.CompletedProcess(arguments, 0, output, "")

        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            server_service.replace_runtime(
                self.root, expected_interpreter=old, interpreter=new,
                home=self.home, runner=runner,
            )
        self.assertEqual(calls, [
            ["launchctl", "print", f"gui/{server_service.os.getuid()}/{server_service.LABEL}"],
            ["launchctl", "bootout", f"gui/{server_service.os.getuid()}", str(paths.plist_path)],
            ["launchctl", "bootstrap", f"gui/{server_service.os.getuid()}", str(paths.plist_path)],
            ["launchctl", "print", f"gui/{server_service.os.getuid()}/{server_service.LABEL}"],
        ])

    def test_replace_runtime_rejects_a_stale_same_label_job_after_bootstrap(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        old, new = Path(sys.executable), Path(self.temporary.name) / "replacement-python"
        stale = Path(self.temporary.name) / "stale-python"
        new.symlink_to(old)
        stale.symlink_to(old)
        server_service.write_plist(paths, old)

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            if arguments[1] == "bootstrap":
                return subprocess.CompletedProcess(arguments, 5, "", "Service already loaded")
            if arguments[1] == "print":
                return subprocess.CompletedProcess(
                    arguments, 0, self.loaded_service_output(stale), "",
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            with self.assertRaisesRegex(server_service.ServerServiceError, "admitted runtime binding"):
                server_service.replace_runtime(
                    self.root, expected_interpreter=old, interpreter=new,
                    home=self.home, runner=runner,
                )

    def test_service_loaded_distinguishes_absence_from_inspection_failure(self) -> None:
        def result(code: int, error: str = "", output: str = ""):
            def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(arguments, code, output, error)
            return runner

        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            self.assertTrue(server_service.service_loaded(runner=result(0)))
            self.assertFalse(server_service.service_loaded(runner=result(3, "Could not find service")))
            with self.assertRaisesRegex(server_service.ServerServiceError, "inspect"):
                server_service.service_loaded(runner=result(5, "Input/output error"))

    def test_service_loaded_proves_the_live_runtime_binding(self) -> None:
        expected = Path(sys.executable).absolute()
        output = f"""gui/501/{server_service.LABEL} = {{
    active count = 1
    path = {self.home}/Library/LaunchAgents/{server_service.LABEL}.plist
    state = running
    program = {expected}
    arguments = {{
        {expected}
        -m
        engineering_platform.server
        serve
        --data-root
        {self.root.resolve()}
    }}
    working directory = {self.root.resolve()}
}}\n"""

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 0, output, "")

        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            self.assertTrue(server_service.service_loaded(
                data_root=self.root,
                expected_interpreter=expected,
                runner=runner,
            ))

    def test_service_loaded_rejects_a_stale_loaded_runtime_binding(self) -> None:
        expected = Path(sys.executable).absolute()
        stale = Path(self.temporary.name) / "stale-python"
        stale.symlink_to(expected)
        output = f"""gui/501/{server_service.LABEL} = {{
    program = {stale}
    arguments = {{
        {stale}
        -m
        engineering_platform.server
        serve
        --data-root
        {self.root.parent / 'other-instance'}
    }}
    working directory = {self.root.parent / 'other-instance'}
}}\n"""

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 0, output, "")

        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            with self.assertRaisesRegex(server_service.ServerServiceError, "admitted runtime binding"):
                server_service.service_loaded(
                    data_root=self.root,
                    expected_interpreter=expected,
                    runner=runner,
                )

    def test_replace_runtime_rejects_an_unexpected_or_missing_service(self) -> None:
        with self.assertRaisesRegex(server_service.ServerServiceError, "expected operational interpreter"):
            server_service.replace_runtime(self.root, expected_interpreter=Path(sys.executable),
                                           interpreter=Path(sys.executable), home=self.home, runner=self.runner)

    def test_relocation_rewrites_and_reloads_an_installed_agent(self) -> None:
        paths = server_service.default_paths(self.root, self.home)
        paths.launch_agents_dir.mkdir(parents=True)
        paths.plist_path.write_text("owned", encoding="utf-8")
        destination = Path(self.temporary.name) / "relocated"
        destination.mkdir()
        (destination / "server.json").write_text("{}", encoding="utf-8")
        calls: list[list[str]] = []

        def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            self.assertTrue(server_service.repoint_after_relocation(self.root, destination, home=self.home, runner=runner))
        with paths.plist_path.open("rb") as stream:
            payload = __import__("plistlib").load(stream)
        self.assertEqual(payload["ProgramArguments"][-1], str(destination.resolve()))
        self.assertEqual(calls, [
            ["launchctl", "bootout", f"gui/{server_service.os.getuid()}", str(paths.plist_path)],
            ["launchctl", "bootstrap", f"gui/{server_service.os.getuid()}", str(paths.plist_path)],
        ])

    def test_relocation_does_nothing_when_service_is_not_installed(self) -> None:
        self.assertFalse(server_service.repoint_after_relocation(self.root, self.root, home=self.home))

    def test_launchagent_failures_and_non_macos_fail_closed(self) -> None:
        with patch("engineering_platform.server_service.platform.system", return_value="Darwin"):
            def failed(arguments: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(arguments, 1, "", "permission denied")
            with self.assertRaisesRegex(server_service.ServerServiceError, "Unable to bootstrap"):
                server_service.install(self.root, interpreter=Path(sys.executable), home=self.home, runner=failed)
        with patch("engineering_platform.server_service.platform.system", return_value="Linux"):
            with self.assertRaisesRegex(server_service.ServerServiceError, "macOS"):
                server_service.uninstall(self.root, home=self.home, runner=self.runner)
