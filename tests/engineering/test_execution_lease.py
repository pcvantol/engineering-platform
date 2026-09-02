from __future__ import annotations

from pathlib import Path
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.execution_lease import LeaseConflictError, LeaseHeartbeat, acquire, heartbeat, history, liveness, reconcile_stale, release
from engineering_platform.storage import open_storage
from engineering_platform import server


class ExecutionLeaseTest(unittest.TestCase):
    def test_central_context_owns_lease_without_a_local_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"; checkout = Path(temporary) / "checkout"; checkout.mkdir()
            server.initialize(data)
            previous = os.environ.get("EP_CENTRAL_OPERATIONAL_DATABASE")
            os.environ["EP_CENTRAL_OPERATIONAL_DATABASE"] = str(data / "engineering.db")
            try:
                StateStore(checkout / ".engineering" / "engineering-runs").save(
                    TransactionState("inbox-central-lease", "repo", "prompt.md", "INITIALIZE")
                )
                lease = acquire(checkout, "inbox-central-lease", identity="host", instance_id="central")
                release(checkout, lease)
            finally:
                if previous is None: os.environ.pop("EP_CENTRAL_OPERATIONAL_DATABASE", None)
                else: os.environ["EP_CENTRAL_OPERATIONAL_DATABASE"] = previous
            self.assertFalse((checkout / ".engineering" / "engineering.db").exists())
            self.assertFalse((checkout / ".engineering" / "engineering-runs").exists())
            with sqlite3.connect(data / "engineering.db") as connection:
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM execution_run_leases WHERE run_id='inbox-central-lease'"
                ).fetchone())
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

    def test_verified_recovery_provider_retains_run_lease_after_host_expiry(self) -> None:
        from engineering_platform.provider_recovery import (
            claim_replacement_launch, create_recovery_available, record_provider_started,
            transition_recovery_state,
        )

        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="departed-host")
        create_recovery_available(
            self.root, run_id="inbox-lease", triggering_invocation_id="attempt-one",
            lifecycle_phase="EXECUTE_AGENT", branch="topic", worktree_identity=str(self.root),
            lease_id=lease.lease_id,
        )
        self.assertTrue(transition_recovery_state(
            self.root, run_id="inbox-lease", expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
        ))
        claim = claim_replacement_launch(self.root, run_id="inbox-lease")
        assert claim is not None
        self.assertTrue(record_provider_started(
            self.root, run_id="inbox-lease", receipt_id=str(claim["receipt_id"]),
            pid=os.getpid(), process_group=os.getpgrp(),
        ))
        with open_storage(self.root) as connection:
            connection.execute(
                "UPDATE execution_run_leases SET expires_at='2020-01-01T00:00:00+00:00' WHERE lease_id=?",
                (lease.lease_id,),
            )
        outcomes = reconcile_stale(self.root)
        self.assertEqual(outcomes[0]["outcome"], "RECOVERY_PROVIDER_STILL_ACTIVE")
        self.assertEqual(liveness(self.root, "inbox-lease")["state"], "LIVE")
        with self.assertRaises(LeaseConflictError):
            acquire(self.root, "inbox-lease", identity="host", instance_id="competing-host")

    def test_ambiguous_recovery_process_also_retains_lease_for_operator_resolution(self) -> None:
        from engineering_platform.provider_recovery import (
            claim_replacement_launch, create_recovery_available, record_provider_started,
            transition_recovery_state,
        )

        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="departed-host")
        create_recovery_available(
            self.root, run_id="inbox-lease", triggering_invocation_id="attempt-one",
            lifecycle_phase="EXECUTE_AGENT", branch="topic", worktree_identity=str(self.root),
            lease_id=lease.lease_id,
        )
        transition_recovery_state(
            self.root, run_id="inbox-lease", expected="RECOVERY_AVAILABLE", target="RECOVERY_STARTING",
        )
        claim = claim_replacement_launch(self.root, run_id="inbox-lease")
        assert claim is not None
        record_provider_started(
            self.root, run_id="inbox-lease", receipt_id=str(claim["receipt_id"]),
            pid=os.getpid(), process_group=os.getpgrp(),
        )
        with open_storage(self.root) as connection:
            connection.execute(
                "UPDATE execution_run_leases SET expires_at='2020-01-01T00:00:00+00:00' WHERE lease_id=?",
                (lease.lease_id,),
            )
        with patch("engineering_platform.execution_lease.verify_process_identity", return_value="MISMATCH"):
            outcomes = reconcile_stale(self.root)
        self.assertEqual(outcomes[0]["outcome"], "RECOVERY_PROVIDER_AMBIGUOUS")
        self.assertEqual(liveness(self.root, "inbox-lease")["state"], "LIVE")

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
