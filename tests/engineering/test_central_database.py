from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
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

    def test_snapshot_and_details_read_only_operate_on_the_central_database(self) -> None:
        """A Console backup is a consistent CENTRAL snapshot, never a project file."""
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "central"
            server.initialize(data_root)
            database = central_database.path(data_root)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO engineering_metadata(key,value) VALUES(?,?)",
                    ("coverage.snapshot", json.dumps("central-only")),
                )

            details = central_database.details(data_root)
            self.assertEqual(details["path"], str(database))
            self.assertGreater(details["size_bytes"], 0)
            self.assertEqual(details["integrity"], "PASS")
            backup = central_database.snapshot(data_root)
            self.assertIsNotNone(backup)
            with tempfile.NamedTemporaryFile(suffix=".db") as restored:
                restored.write(backup or b"")
                restored.flush()
                with sqlite3.connect(restored.name) as connection:
                    self.assertEqual(
                        connection.execute("SELECT value FROM engineering_metadata WHERE key='coverage.snapshot'").fetchone(),
                        (json.dumps("central-only"),),
                    )

            missing_root = Path(temporary) / "not-a-server"
            self.assertIsNone(central_database.snapshot(missing_root))
            self.assertEqual(central_database.details(missing_root)["integrity"], "UNAVAILABLE")
