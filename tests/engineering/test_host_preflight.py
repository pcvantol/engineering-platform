from __future__ import annotations

from collections import namedtuple
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering import host_preflight
from tools.engineering.platform_api import PlatformConfigurationError


ROOT = Path(__file__).resolve().parents[2]


class HostPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        target = self.root / "tools" / "engineering"
        target.mkdir(parents=True)
        (target / "ENGINEERING_PLATFORM_CONFIG.json").write_text(
            (ROOT / "tools" / "engineering" / "ENGINEERING_PLATFORM_CONFIG.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        for name in ("status", "reports", "logs", "inbox-processing"):
            (self.root / ".engineering" / name).mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _execute(self):
        managed_prefix = self.root / "managed-codex-cli"
        executable = managed_prefix / "bin" / "codex"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        with patch("tools.engineering.platform_api.engineering_platform_codex_cli_prefix", return_value=managed_prefix), patch(
            "tools.engineering.providers.subprocess.run",
            return_value=subprocess.CompletedProcess(("codex", "--version"), 0, "codex 1.0", ""),
        ):
            return host_preflight.execute(self.root, run_id="inbox-preflight")

    def test_healthy_host_persists_compact_pass_evidence(self) -> None:
        result = self._execute()
        self.assertEqual(result.outcome, "PASS")
        evidence = host_preflight.latest(self.root)
        self.assertEqual(evidence["outcome"], "PASS")
        self.assertEqual(evidence["run_id"], "inbox-preflight")
        self.assertEqual(evidence["execution_host"], "Engineering Platform")

    def test_missing_runtime_executable_fails_closed(self) -> None:
        with patch(
            "tools.engineering.platform_api.engineering_platform_codex_cli_prefix",
            return_value=self.root / "missing-managed-cli",
        ):
            result = host_preflight.execute(self.root)
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("runtime_executable", {check.identifier for check in result.checks if check.outcome == "FAIL"})

    def test_insufficient_disk_space_fails_closed(self) -> None:
        usage = namedtuple("Usage", "total used free")(10, 9, 1)
        with patch("tools.engineering.host_preflight.shutil.disk_usage", return_value=usage), patch.dict(
            "os.environ", {host_preflight.MINIMUM_FREE_BYTES_ENVIRONMENT: "2"}
        ):
            result = self._execute()
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("disk_space", {check.identifier for check in result.checks if check.outcome == "FAIL"})

    def test_unwritable_reports_directory_fails_closed(self) -> None:
        original = host_preflight._writable
        with patch("tools.engineering.host_preflight._writable", side_effect=lambda path: False if path.name == "reports" else original(path)):
            result = self._execute()
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("writable_reports", {check.identifier for check in result.checks if check.outcome == "FAIL"})

    def test_sqlite_unavailable_fails_without_corrupting_state(self) -> None:
        with patch("tools.engineering.host_preflight.open_storage", side_effect=sqlite3.DatabaseError("unavailable")):
            result = self._execute()
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("telemetry_storage", {check.identifier for check in result.checks if check.outcome == "FAIL"})

    def test_configuration_unavailable_fails_closed(self) -> None:
        with patch("tools.engineering.host_preflight.PlatformConfiguration.load", side_effect=PlatformConfigurationError("missing")):
            result = self._execute()
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("configuration", {check.identifier for check in result.checks if check.outcome == "FAIL"})
