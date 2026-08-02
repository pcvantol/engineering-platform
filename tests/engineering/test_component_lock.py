from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering.component_lock import DuplicateComponentInstanceError, single_instance


class ComponentLockTest(unittest.TestCase):
    def test_single_instance_persists_safe_owner_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with single_instance(root, "dashboard"):
                metadata = (root / ".engineering" / "locks" / "dashboard.lock").read_text(encoding="utf-8")
                self.assertIn('"component": "dashboard"', metadata)

    def test_second_instance_is_refused_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "tools.engineering.component_lock.fcntl.flock",
            side_effect=(BlockingIOError(), None),
        ):
            with self.assertRaises(DuplicateComponentInstanceError):
                with single_instance(Path(temporary), "inbox-watcher"):
                    pass

