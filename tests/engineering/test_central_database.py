from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from engineering_platform import central_database, server


class CentralDatabaseMaintenanceTests(unittest.TestCase):
    def test_provider_capacity_history_and_reserve_are_installation_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "central"
            server.initialize(data_root)
            early = datetime(2026, 9, 2, 10, 13, tzinfo=timezone.utc)
            late = early + timedelta(minutes=24)

            self.assertEqual(central_database.capacity_configuration(data_root), {"codex_capacity_reserve_percent": 0})
            self.assertEqual(
                central_database.update_capacity_configuration(data_root, 20),
                {"previous": 0, "codex_capacity_reserve_percent": 20},
            )
            self.assertEqual(
                central_database.record_provider_capacity(
                    data_root, provider="Codex CLI", remaining_percent=72, observed_at=early,
                ),
                [{"at": "2026-09-02T10:00:00+00:00", "remaining_percent": 72.0}],
            )
            # A second read in the same bucket retains the conservative low.
            self.assertEqual(
                central_database.record_provider_capacity(
                    data_root, provider="Codex CLI", remaining_percent=81, observed_at=late,
                ),
                [{"at": "2026-09-02T10:00:00+00:00", "remaining_percent": 72.0}],
            )

    def test_maintenance_compacts_only_the_installation_owned_database_at_its_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "central"
            server.initialize(data_root)
            moment = datetime(2026, 9, 2, tzinfo=timezone.utc)

            self.assertEqual(central_database.path(data_root), data_root.resolve() / "engineering.db")
            self.assertEqual(central_database.update_maintenance_configuration(data_root, 60)["interval_seconds"], 60)
            self.assertEqual(central_database.run_periodic_maintenance(data_root, now=moment)["state"], "COMPACTED")
            self.assertEqual(
                central_database.run_periodic_maintenance(data_root, now=moment + timedelta(seconds=59))["state"],
                "NOT_DUE",
            )
            self.assertEqual(
                central_database.run_periodic_maintenance(data_root, now=moment + timedelta(seconds=60))["state"],
                "COMPACTED",
            )
