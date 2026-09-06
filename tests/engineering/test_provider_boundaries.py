from __future__ import annotations

from pathlib import Path
import unittest


class ProviderBoundaryTest(unittest.TestCase):
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
