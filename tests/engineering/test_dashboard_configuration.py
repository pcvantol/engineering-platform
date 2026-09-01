from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from engineering_platform.dashboard_configuration import DEFAULTS, get, inbox_root, update, update_inbox_root
from engineering_platform.component_logging import prune_component_logs
from engineering_platform.storage import open_storage


class DashboardConfigurationTest(unittest.TestCase):
    def test_defaults_and_valid_local_update_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(get(root), DEFAULTS)
            event = update(root, "log_retention_days", 180)
            self.assertEqual(event["previous"], 30)
            self.assertEqual(event["value"], 180)
            self.assertEqual(get(root)["log_retention_days"], 180)
            telemetry_event = update(root, "telemetry_retention_days", 180)
            self.assertEqual(telemetry_event["previous"], 90)
            self.assertEqual(telemetry_event["value"], 180)
            for key, value in (
                ("inbox_scan_interval_seconds", 30),
                ("open_pr_check_interval_seconds", 60),
                ("dashboard_stream_interval_seconds", 10),
                ("platform_health_refresh_seconds", 60),
                ("component_details_refresh_seconds", 15),
                ("database_maintenance_interval_seconds", 24 * 60 * 60),
                ("provider_readiness_refresh_seconds", 600),
                ("codex_capacity_reserve_percent", 25),
            ):
                self.assertEqual(update(root, key, value)["value"], value)

    def test_provider_readiness_refresh_defaults_to_five_minutes_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(get(root)["provider_readiness_refresh_seconds"], 300)
            self.assertEqual(update(root, "provider_readiness_refresh_seconds", 60)["value"], 60)
            with self.assertRaises(ValueError):
                update(root, "provider_readiness_refresh_seconds", 15)

    def test_database_maintenance_interval_defaults_to_hourly_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(get(root)["database_maintenance_interval_seconds"], 60 * 60)
            for value in (60, 60 * 60, 24 * 60 * 60, 7 * 24 * 60 * 60):
                self.assertEqual(update(root, "database_maintenance_interval_seconds", value)["value"], value)
            with self.assertRaises(ValueError):
                update(root, "database_maintenance_interval_seconds", 30)

    def test_dashboard_stream_interval_defaults_to_one_second_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(get(root)["dashboard_stream_interval_seconds"], 1)
            self.assertEqual(update(root, "dashboard_stream_interval_seconds", 10)["value"], 10)
            with self.assertRaises(ValueError):
                update(root, "dashboard_stream_interval_seconds", 11)

    def test_codex_capacity_reserve_is_disabled_by_default_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(get(root)["codex_capacity_reserve_percent"], 0)
            self.assertEqual(update(root, "codex_capacity_reserve_percent", 75)["value"], 75)
            with self.assertRaises(ValueError):
                update(root, "codex_capacity_reserve_percent", 30)
            with self.assertRaises(ValueError):
                update(root, "codex_capacity_reserve_percent", 75.0)

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
            event = update_inbox_root(root, str(candidate / "Inbox"))
            self.assertEqual(event["value"], str(candidate.resolve()))
            self.assertEqual(inbox_root(root), candidate.resolve())
