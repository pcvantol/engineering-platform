from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import providers


class ProviderBoundaryTest(unittest.TestCase):
    def test_validation_environment_preserves_the_ep_venv_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "venv"
            launcher = root / "bin" / "python"
            launcher.parent.mkdir(parents=True)
            (root / "pyvenv.cfg").write_text("home = /base\n", encoding="utf-8")
            launcher.symlink_to(Path(sys.executable))
            with patch("engineering_platform.providers.sys.executable", str(launcher)), patch.dict(os.environ, {"PATH": "/base"}, clear=True):
                environment = providers.installed_python_environment()

        self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(launcher.parent.absolute()))
        self.assertEqual(environment["VIRTUAL_ENV"], str(root.absolute()))

    def test_execution_lifecycle_modules_do_not_spawn_processes_directly(self) -> None:
        engineering = Path(__file__).parents[2] / "src" / "engineering_platform"
        for name in (
            "execution_host.py", "file_inbox.py", "console_presentation.py", "host_preflight.py",
            "workspace_preflight.py", "qualification.py", "report_analysis.py", "codex_chat.py",
            "component_logging.py", "live_status.py",
        ):
            source = (engineering / name).read_text(encoding="utf-8")
            self.assertNotIn("subprocess.run(", source, name)
            self.assertNotIn("subprocess.Popen(", source, name)
