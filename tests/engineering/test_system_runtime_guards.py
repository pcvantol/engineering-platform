from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import providers
from engineering_platform import server
from engineering_platform import system_instance_provisioner


class SystemRuntimeGuardTests(unittest.TestCase):
    def test_server_rejects_wrong_persisted_instance_before_listening(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "server"
            identity = server.initialize(root)
            with self.assertRaisesRegex(
                server.ServerConfigurationError,
                "EP_SERVER_INSTANCE_ID_MISMATCH",
            ):
                server.serve(root, expected_instance_id=identity.instance_id + "-wrong")

    def test_system_github_selection_never_falls_back_to_path(self) -> None:
        with patch.dict(
            os.environ,
            {providers.MANAGED_GITHUB_CLI_EXECUTABLE_ENVIRONMENT: "/missing/owned/gh"},
            clear=False,
        ), patch("engineering_platform.providers.shutil.which", return_value="/usr/local/bin/gh") as which:
            self.assertIsNone(providers.github_cli_executable())
            which.assert_not_called()

    def test_system_provisioner_has_no_project_agent_lifecycle_dependency(self) -> None:
        source = Path(system_instance_provisioner.__file__).read_text(encoding="utf-8")
        self.assertNotIn("project_agent", source)
        self.assertNotIn("LaunchAgent", source)


if __name__ == "__main__":
    unittest.main()
