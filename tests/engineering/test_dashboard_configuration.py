from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.engineering.dashboard_configuration import DEFAULTS, get, inbox_root, update, update_inbox_root
from tools.engineering.component_logging import prune_component_logs
from tools.engineering.storage import open_storage


class DashboardConfigurationTest(unittest.TestCase):
    def test_defaults_and_valid_local_update_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(get(root), DEFAULTS)
            event = update(root, "log_retention_days", 180)
            self.assertEqual(event["previous"], 30)
            self.assertEqual(event["value"], 180)
            self.assertEqual(get(root)["log_retention_days"], 180)

    def test_rejects_unknown_or_unbounded_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                update(root, "lease_timeout", 1)
            with self.assertRaises(ValueError):
                update(root, "log_level", "ERROR")

    def test_retention_prunes_only_expired_component_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = open_storage(root)
            try:
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                    ("dashboard", "{}", "2020-01-01T00:00:00+00:00"),
                )
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                    ("dashboard", "{}", "2999-01-01T00:00:00+00:00"),
                )
            finally:
                connection.close()
            prune_component_logs(root, 30)
            connection = open_storage(root)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM engineering_component_logs").fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_inbox_root_requires_an_existing_writable_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "transport"
            candidate.mkdir()
            with self.assertRaises(ValueError):
                update_inbox_root(root, str(candidate))
            (candidate / "Inbox").mkdir()
            event = update_inbox_root(root, str(candidate))
            self.assertEqual(event["value"], str(candidate.resolve()))
            self.assertEqual(inbox_root(root), candidate.resolve())
