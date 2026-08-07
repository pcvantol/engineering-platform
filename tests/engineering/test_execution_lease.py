from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from tools.engineering.agent_state import StateStore, TransactionState
from tools.engineering.execution_lease import LeaseConflictError, LeaseHeartbeat, acquire, heartbeat, history, liveness, reconcile_stale, release
from tools.engineering.storage import open_storage


class ExecutionLeaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        StateStore(self.root / ".engineering" / "engineering-runs").save(
            TransactionState("inbox-lease", "repo", "prompt.md", "INITIALIZE")
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_acquires_heartbeats_and_releases_one_canonical_lease(self) -> None:
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        renewed = heartbeat(self.root, lease)
        release(self.root, renewed)
        with open_storage(self.root) as connection:
            self.assertEqual(connection.execute("SELECT lease_state FROM execution_run_leases WHERE lease_id=?", (lease.lease_id,)).fetchone()[0], "RELEASED")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_lease_events").fetchone()[0], 2)

    def test_conflicting_live_owner_fails_closed(self) -> None:
        acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        with self.assertRaises(LeaseConflictError):
            acquire(self.root, "inbox-lease", identity="host", instance_id="instance-b")

    def test_recovery_owner_gets_a_new_lease_after_expiry(self) -> None:
        original = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        with open_storage(self.root) as connection:
            connection.execute(
                "UPDATE execution_run_leases SET expires_at='2020-01-01T00:00:00+00:00' WHERE lease_id=?",
                (original.lease_id,),
            )
        recovered = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-b")
        self.assertNotEqual(recovered.lease_id, original.lease_id)
        with open_storage(self.root) as connection:
            states = connection.execute(
                "SELECT host_instance_id,lease_state FROM execution_run_leases WHERE run_id=? ORDER BY created_at",
                ("inbox-lease",),
            ).fetchall()
        self.assertEqual(states, [("instance-a", "EXPIRED"), ("instance-b", "ACTIVE")])

    def test_expired_active_run_is_reconciled_without_terminal_fabrication(self) -> None:
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        with open_storage(self.root) as connection:
            connection.execute("UPDATE execution_run_leases SET expires_at='2020-01-01T00:00:00+00:00' WHERE lease_id=?", (lease.lease_id,))
        outcome = reconcile_stale(self.root)
        self.assertEqual(outcome[0]["outcome"], "RECOVERABLE")
        with open_storage(self.root) as connection:
            self.assertEqual(connection.execute("SELECT phase FROM engineering_transactions WHERE run_id='inbox-lease'").fetchone()[0], "INITIALIZE")

    def test_reconciles_a_proven_terminal_payload_after_lease_expiry(self) -> None:
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        with open_storage(self.root) as connection:
            connection.execute(
                "UPDATE engineering_transactions SET payload=?,phase='EXECUTE_AGENT' WHERE run_id='inbox-lease'",
                (json.dumps({"phase": "COMPLETE", "terminal": True}),),
            )
            connection.execute("UPDATE execution_run_leases SET expires_at='2020-01-01T00:00:00+00:00' WHERE lease_id=?", (lease.lease_id,))
        outcome = reconcile_stale(self.root)
        self.assertEqual(outcome[0]["outcome"], "TERMINAL_EVIDENCE_PRESENT")
        with open_storage(self.root) as connection:
            self.assertEqual(connection.execute("SELECT phase FROM engineering_transactions WHERE run_id='inbox-lease'").fetchone()[0], "COMPLETE")

    def test_active_transaction_without_lease_is_operator_visible(self) -> None:
        outcomes = reconcile_stale(self.root)
        self.assertEqual(outcomes[0]["outcome"], "OPERATOR_INTERVENTION_REQUIRED")
        self.assertEqual(liveness(self.root, "inbox-lease")["state"], "STALE")
        with open_storage(self.root) as connection:
            self.assertEqual(
                connection.execute("SELECT outcome FROM execution_run_reconciliations WHERE run_id='inbox-lease'").fetchone()[0],
                "OPERATOR_INTERVENTION_REQUIRED",
            )

    def test_background_heartbeat_stops_without_releasing_ownership(self) -> None:
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        pulse = LeaseHeartbeat(self.root, lease, interval_seconds=1)
        pulse.start()
        stopped = pulse.stop()
        self.assertIsNone(pulse.error)
        self.assertEqual(stopped.lease_id, lease.lease_id)

    def test_history_retains_released_lease_evidence(self) -> None:
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        release(self.root, lease)
        evidence = history(self.root, "inbox-lease")
        self.assertEqual(evidence["lease_state"], "RELEASED")
        self.assertEqual(evidence["host_instance_id"], "instance-a")
