from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import host_admin


class HostAdminTest(unittest.TestCase):
    def test_diagnostics_are_bounded_to_the_explicit_installation_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "engineering_platform.host_admin.managed_codex_runtime.inspect",
            return_value={"state": "READY", "path": "/not-returned", "token": "not-returned"},
        ):
            root = Path(temporary) / "installation"
            root.mkdir()
            result = host_admin.diagnostics(root)
        self.assertEqual(result["scope"], "HOST_ADMIN")
        self.assertEqual(result["root_kind"], "EP_SERVER_INSTALLATION")
        self.assertEqual(result["managed_codex_runtime"], {"state": "READY"})
        self.assertNotIn("path", result["managed_codex_runtime"])
        self.assertFalse(result["project_authority"])
        self.assertFalse(result["execution_authority"])
        self.assertFalse(result["queue_authority"])
        self.assertFalse(result["mutations_supported"])

    def test_installation_root_is_not_derived_from_cwd_or_project_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "installation"
            root.mkdir()
            self.assertEqual(host_admin.installation_root(root), root.resolve())
