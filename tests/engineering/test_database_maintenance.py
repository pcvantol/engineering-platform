from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tools.engineering.database_maintenance import run_periodic_database_maintenance
from tools.engineering.execution_lease import acquire, host_identity, host_instance_id, release
from tools.engineering.storage import open_storage


class DatabaseMaintenanceTest(unittest.TestCase):
    def test_compacts_at_most_once_per_hour_without_deleting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timestamp = datetime(2026, 8, 28, 18, tzinfo=timezone.utc)
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                    ("inbox-terminal", "{}", "COMPLETE", timestamp.isoformat()),
                )

            self.assertEqual(run_periodic_database_maintenance(root, now=timestamp)["state"], "COMPACTED")
            self.assertEqual(
                run_periodic_database_maintenance(root, now=timestamp + timedelta(minutes=59))["state"],
                "NOT_DUE",
            )
            with open_storage(root) as connection:
                self.assertEqual(
                    connection.execute("SELECT phase FROM engineering_transactions WHERE run_id='inbox-terminal'").fetchone()[0],
                    "COMPLETE",
                )

    def test_skips_compaction_when_a_canonical_execution_lease_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                    ("inbox-live", "{}", "EXECUTE_AGENT", "2026-08-28T18:00:00+00:00"),
                )
            lease = acquire(
                root, "inbox-live", identity=host_identity(), instance_id=host_instance_id(), process_id=1,
            )
            try:
                outcome = run_periodic_database_maintenance(root)
                self.assertEqual(outcome["state"], "SKIPPED_ACTIVE_RUN")
            finally:
                release(root, lease)

    def test_skips_compaction_for_an_unresolved_transaction_without_a_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                    ("inbox-recovery-required", "{}", "EXECUTE_AGENT", "2026-08-28T18:00:00+00:00"),
                )

            self.assertEqual(run_periodic_database_maintenance(root)["state"], "SKIPPED_ACTIVE_RUN")

    def test_safe_skip_is_throttled_before_the_next_hourly_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timestamp = datetime.now(timezone.utc).replace(microsecond=0)
            with open_storage(root) as connection:
                connection.execute(
                    "INSERT INTO engineering_transactions(run_id,payload,phase,updated_at) VALUES(?,?,?,?)",
                    ("inbox-finishing", "{}", "FINALIZE", timestamp.isoformat()),
                )

            self.assertEqual(
                run_periodic_database_maintenance(root, now=timestamp)["state"],
                "SKIPPED_ACTIVE_RUN",
            )
            with open_storage(root) as connection:
                connection.execute(
                    "UPDATE engineering_transactions SET phase='COMPLETE' WHERE run_id='inbox-finishing'"
                )

            self.assertEqual(
                run_periodic_database_maintenance(root, now=timestamp + timedelta(minutes=59))["state"],
                "NOT_DUE",
            )
            self.assertEqual(
                run_periodic_database_maintenance(root, now=timestamp + timedelta(hours=1))["state"],
                "COMPACTED",
            )
