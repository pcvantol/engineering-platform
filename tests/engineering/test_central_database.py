from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from engineering_platform import central_database, server


class CentralDatabaseMaintenanceTests(unittest.TestCase):
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
