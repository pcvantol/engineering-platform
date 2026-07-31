from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.engineering.dashboard import _status


class DashboardStatusTest(unittest.TestCase):
    def test_missing_status_uses_a_complete_degraded_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = json.loads(_status(Path(temporary)))

        self.assertEqual(status["watcher_state"], "REMOTE_ENGINEERING_DEGRADED")
        self.assertEqual(status["queue_depth"], 0)
        self.assertEqual(status["repository_state"], "UNKNOWN")
        self.assertEqual(status["workspace_state"], "UNKNOWN")
        self.assertIn("No local engineering status", status["diagnostic"])
