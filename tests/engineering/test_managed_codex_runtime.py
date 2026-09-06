"""Managed Codex runtime lifecycle retains the historical explicit repair flow."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import managed_codex_runtime as runtime


class ManagedCodexRuntimeTests(unittest.TestCase):
    def test_missing_runtime_is_an_actionable_ep_owned_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "codex-cli"
            with patch("engineering_platform.managed_codex_runtime.engineering_platform_codex_cli_prefix", return_value=prefix), patch(
                "engineering_platform.managed_codex_runtime.npm_executable", return_value="/usr/local/bin/npm"
            ):
                self.assertEqual(
                    runtime.inspect(Path(directory)),
                    {"state": "MISSING", "path": str(prefix / "bin" / "codex"), "remediation_available": True},
                )

    def test_non_executable_runtime_is_never_accepted_as_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "codex-cli"
            executable = prefix / "bin" / "codex"
            executable.parent.mkdir(parents=True)
            executable.write_text("not executable", encoding="utf-8")
            executable.chmod(0o600)
            with patch("engineering_platform.managed_codex_runtime.engineering_platform_codex_cli_prefix", return_value=prefix), patch(
                "engineering_platform.managed_codex_runtime.npm_executable", return_value=None
            ):
                self.assertEqual(
                    runtime.inspect(Path(directory)),
                    {"state": "BROKEN", "path": str(executable), "remediation_available": False},
                )

    def test_version_parser_accepts_the_preserved_cli_output(self) -> None:
        self.assertEqual(runtime.version("codex-cli 0.152.1"), "0.152.1")

    def test_version_parser_rejects_malformed_output_and_orders_prereleases(self) -> None:
        self.assertIsNone(runtime.version("not a version"))
        self.assertGreater(runtime.version_key("0.152.1"), runtime.version_key("0.152.1-rc.1"))

    def test_executable_with_unreadable_version_is_broken(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "codex-cli"; executable = prefix / "bin" / "codex"
            executable.parent.mkdir(parents=True); executable.write_text("stub", encoding="utf-8"); executable.chmod(0o700)
            with patch("engineering_platform.managed_codex_runtime.engineering_platform_codex_cli_prefix", return_value=prefix), patch(
                "engineering_platform.managed_codex_runtime.npm_executable", return_value="npm"
            ), patch("engineering_platform.managed_codex_runtime.CodexCliProvider.command", side_effect=OSError("offline")):
                self.assertEqual(runtime.inspect(Path(directory))["state"], "BROKEN")

    def test_explicit_repair_requires_npm_and_invalid_registry_data_fails_closed(self) -> None:
        with patch("engineering_platform.managed_codex_runtime.npm_executable", return_value=None):
            with self.assertRaisesRegex(runtime.ManagedCodexRuntimeError, "unavailable"):
                runtime.provision(Path("/workspace"))
        with patch("engineering_platform.managed_codex_runtime.LocalProcessProvider.execute", return_value=subprocess.CompletedProcess(("npm",), 0, '"bad"', "")):
            with self.assertRaisesRegex(runtime.ManagedCodexRuntimeError, "unavailable"):
                runtime._published_version(Path("/workspace"), "npm")

    def test_explicit_provision_is_pinned_and_verified(self) -> None:
        root = Path("/workspace")
        prefix = Path("/managed/codex-cli")
        completed = subprocess.CompletedProcess
        with patch("engineering_platform.managed_codex_runtime.engineering_platform_codex_cli_prefix", return_value=prefix), patch(
            "engineering_platform.managed_codex_runtime.npm_executable", return_value="/usr/local/bin/npm"
        ), patch("engineering_platform.managed_codex_runtime.inspect", side_effect=[
            {"state": "MISSING"}, {"state": "READY", "version": "0.150.0"},
        ]), patch("engineering_platform.managed_codex_runtime.LocalProcessProvider.execute", side_effect=[
            completed(("npm", "view"), 0, '"0.150.0"', ""),
            completed(("npm", "install"), 0, "installed", ""),
        ]) as execute:
            self.assertEqual(runtime.provision(root), {"updated": True, "current_version": "0.150.0"})
        self.assertEqual(
            execute.call_args_list[1].args[1],
            ("/usr/local/bin/npm", "install", "--global", "--prefix", str(prefix), "@openai/codex@0.150.0"),
        )

    def test_failed_install_never_reports_ready(self) -> None:
        with patch("engineering_platform.managed_codex_runtime.npm_executable", return_value="/usr/local/bin/npm"), patch(
            "engineering_platform.managed_codex_runtime.inspect", return_value={"state": "MISSING"}
        ), patch("engineering_platform.managed_codex_runtime.LocalProcessProvider.execute", side_effect=[
            subprocess.CompletedProcess(("npm", "view"), 0, '"0.150.0"', ""),
            subprocess.CompletedProcess(("npm", "install"), 1, "", "permission denied"),
        ]):
            with self.assertRaisesRegex(runtime.ManagedCodexRuntimeError, "permissions_required"):
                runtime.provision(Path("/workspace"))

    def test_current_runtime_is_not_reinstalled(self) -> None:
        with patch("engineering_platform.managed_codex_runtime.npm_executable", return_value="/usr/local/bin/npm"), patch(
            "engineering_platform.managed_codex_runtime.inspect", return_value={"state": "READY", "version": "0.150.0"}
        ), patch("engineering_platform.managed_codex_runtime.LocalProcessProvider.execute", return_value=subprocess.CompletedProcess(
            ("npm", "view"), 0, '"0.150.0"', ""
        )) as execute:
            self.assertEqual(runtime.provision(Path("/workspace")), {"updated": False, "current_version": "0.150.0"})
        self.assertEqual(execute.call_count, 1)

    def test_operator_can_decline_an_already_ready_runtime_without_registry_access(self) -> None:
        with patch("engineering_platform.managed_codex_runtime.npm_executable", return_value="npm"), patch(
            "engineering_platform.managed_codex_runtime.inspect", return_value={"state": "READY", "version": "0.150.0"}
        ), patch("engineering_platform.managed_codex_runtime.LocalProcessProvider.execute") as execute:
            self.assertEqual(runtime.provision(Path("/workspace"), allow_current=False), {"updated": False, "current_version": "0.150.0"})
        execute.assert_not_called()
