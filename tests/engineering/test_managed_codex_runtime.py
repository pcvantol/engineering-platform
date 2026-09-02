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
