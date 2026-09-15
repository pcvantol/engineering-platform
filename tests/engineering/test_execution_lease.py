from __future__ import annotations

from engineering_platform.storage import sqlite_connection

from pathlib import Path
from datetime import timedelta
import json
import os
import sqlite3
import tempfile
from threading import Event, Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.execution_lease import Lease, LeaseConflictError, LeaseHeartbeat, LeaseHeartbeatError, _now, acquire, heartbeat, history, liveness, reconcile_stale, release
from engineering_platform.storage import open_storage
from engineering_platform import server


class ExecutionLeaseTest(unittest.TestCase):
    def test_central_context_owns_lease_without_a_local_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"; checkout = Path(temporary) / "checkout"; checkout.mkdir()
            server.initialize(data)
            database = data / server.SERVER_DATABASE_FILENAME
            StateStore(
                checkout / ".engineering" / "engineering-runs", central_database=database,
                emit_local_projection=False,
            ).save(TransactionState("inbox-central-lease", "repo", "prompt.md", "INITIALIZE"))
            lease = acquire(checkout, "inbox-central-lease", identity="host", instance_id="central", central_database=database)
            self.assertEqual(liveness(checkout, lease.run_id, central_database=database)["state"], "LIVE")
            release(checkout, lease, central_database=database)
            self.assertFalse((checkout / ".engineering" / "engineering.db").exists())
            self.assertFalse((checkout / ".engineering" / "engineering-runs").exists())
            with sqlite_connection(data / server.SERVER_DATABASE_FILENAME) as connection:
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

    def test_heartbeat_retries_one_transient_io_error_while_same_lease_is_valid(self) -> None:
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        import engineering_platform.execution_lease as execution_lease
        original_connection = execution_lease._connection
        attempts = 0

        class FailFirstUpdate:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, statement: str, *arguments: object) -> object:
                if statement.startswith("UPDATE execution_run_leases"):
                    raise sqlite3.OperationalError("disk I/O error")
                return self.connection.execute(statement, *arguments)

            def close(self) -> None:
                self.connection.close()

        def connect(*arguments: object, **keywords: object) -> sqlite3.Connection:
            nonlocal attempts
            attempts += 1
            connection = original_connection(*arguments, **keywords)
            return FailFirstUpdate(connection) if attempts == 1 else connection  # type: ignore[return-value]

        with patch("engineering_platform.execution_lease._connection", side_effect=connect), \
             patch("engineering_platform.execution_lease.sleep") as delay:
            renewed = heartbeat(self.root, lease)
        self.assertEqual(attempts, 2)
        delay.assert_called_once_with(0.02)
        self.assertEqual(renewed.lease_id, lease.lease_id)
        self.assertEqual(renewed.host_instance_id, lease.host_instance_id)

    def test_checkpoint_and_heartbeat_share_sqlite_without_duplicate_run_or_lease(self) -> None:
        """A transient lease write is retried while checkpoint work stays canonical."""
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        store = StateStore(self.root / ".engineering" / "engineering-runs")
        store.save(TransactionState("inbox-lease", "repo", "prompt.md", "EXECUTE_AGENT"))
        import engineering_platform.execution_lease as execution_lease
        original_connection = execution_lease._connection
        attempts = 0

        class FailFirstUpdate:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, statement: str, *arguments: object) -> object:
                if statement.startswith("UPDATE execution_run_leases"):
                    raise sqlite3.OperationalError("disk I/O error")
                return self.connection.execute(statement, *arguments)

            def close(self) -> None:
                self.connection.close()

        def connect(*arguments: object, **keywords: object) -> sqlite3.Connection:
            nonlocal attempts
            attempts += 1
            connection = original_connection(*arguments, **keywords)
            return FailFirstUpdate(connection) if attempts == 1 else connection  # type: ignore[return-value]

        with patch("engineering_platform.execution_lease._connection", side_effect=connect), \
             patch("engineering_platform.execution_lease.sleep"):
            renewed = heartbeat(self.root, lease)
        store.save(TransactionState("inbox-lease", "repo", "prompt.md", "LOCAL_REPOSITORY_VALIDATION"))
        self.assertEqual(store.load("inbox-lease").phase, "LOCAL_REPOSITORY_VALIDATION")
        self.assertEqual(liveness(self.root, "inbox-lease")["state"], "LIVE")
        self.assertEqual(renewed.lease_id, lease.lease_id)
        with open_storage(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM execution_run_leases WHERE run_id=?", (lease.run_id,)).fetchone()[0], 1)

    def test_heartbeat_preserves_initial_transient_error_when_retry_also_fails(self) -> None:
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        import engineering_platform.execution_lease as execution_lease
        original_connection = execution_lease._connection
        attempts = 0

        class FailUpdate:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, statement: str, *arguments: object) -> object:
                if statement.startswith("UPDATE execution_run_leases"):
                    raise sqlite3.OperationalError(f"disk I/O error {attempts}")
                return self.connection.execute(statement, *arguments)

            def close(self) -> None:
                self.connection.close()

        def connect(*arguments: object, **keywords: object) -> sqlite3.Connection:
            nonlocal attempts
            attempts += 1
            return FailUpdate(original_connection(*arguments, **keywords))  # type: ignore[return-value]

        with patch("engineering_platform.execution_lease._connection", side_effect=connect), \
             patch("engineering_platform.execution_lease.sleep") as delay:
            with self.assertRaisesRegex(sqlite3.OperationalError, "error 1"):
                heartbeat(self.root, lease)
        self.assertEqual(attempts, 2)
        delay.assert_called_once_with(0.02)

    def test_heartbeat_never_renews_an_expired_lease(self) -> None:
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        with open_storage(self.root) as connection:
            connection.execute("UPDATE execution_run_leases SET expires_at='2020-01-01T00:00:00+00:00' WHERE lease_id=?", (lease.lease_id,))
        with self.assertRaises(LeaseConflictError):
            heartbeat(self.root, lease)
        self.assertEqual(liveness(self.root, lease.run_id)["state"], "STALE")

    def test_heartbeat_rejects_lease_that_expires_while_waiting_for_writer_lock(self) -> None:
        """A real SQLite writer wait must not retain authority from before it."""
        database = (self.root / ".engineering" / "engineering.db").resolve()
        lease = acquire(
            self.root, "inbox-lease", identity="host", instance_id="instance-a", central_database=database,
        )
        expires_at = _now() + timedelta(seconds=0.2)
        with open_storage(self.root) as connection:
            connection.execute(
                "UPDATE execution_run_leases SET expires_at=? WHERE lease_id=?",
                (expires_at.isoformat(), lease.lease_id),
            )
            before = connection.execute(
                "SELECT last_heartbeat_at,expires_at FROM execution_run_leases WHERE lease_id=?", (lease.lease_id,)
            ).fetchone()

        # Connection A holds the real SQLite writer lock while heartbeat uses
        # its own connection and blocks on BEGIN IMMEDIATE.
        blocker = sqlite3.connect(database, isolation_level=None)
        blocker.execute("PRAGMA busy_timeout=10000")
        blocker.execute("BEGIN IMMEDIATE")
        started = Event()
        lock_attempted = Event()
        outcome: list[object] = []
        import engineering_platform.execution_lease as execution_lease
        original_connection = execution_lease._connection

        def traced_connection(*arguments: object, **keywords: object) -> sqlite3.Connection:
            connection = original_connection(*arguments, **keywords)
            class LockAttemptConnection:
                def execute(self, statement: str, *parameters: object) -> object:
                    if statement == "BEGIN IMMEDIATE":
                        lock_attempted.set()
                    return connection.execute(statement, *parameters)

                def close(self) -> None:
                    connection.close()

            return LockAttemptConnection()  # type: ignore[return-value]

        def renew() -> None:
            started.set()
            try:
                outcome.append(heartbeat(self.root, lease, central_database=database))
            except Exception as error:
                outcome.append(error)

        with patch("engineering_platform.execution_lease._connection", side_effect=traced_connection):
            worker = Thread(target=renew)
            worker.start()
            self.assertTrue(started.wait(1))
            # The trace callback fires for heartbeat's actual BEGIN IMMEDIATE,
            # while connection A still owns the writer lock.
            self.assertTrue(lock_attempted.wait(1))
            deadline = monotonic() + 2
            while _now() <= expires_at and monotonic() < deadline:
                sleep(0.01)
            self.assertGreater(_now(), expires_at)
            blocker.execute("COMMIT")
            blocker.close()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], LeaseConflictError)
        with open_storage(self.root) as connection:
            after = connection.execute(
                "SELECT last_heartbeat_at,expires_at FROM execution_run_leases WHERE lease_id=?", (lease.lease_id,)
            ).fetchone()
            count = connection.execute(
                "SELECT COUNT(*) FROM execution_run_leases WHERE run_id=?", (lease.run_id,)
            ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(count, 1)

    def test_heartbeat_renews_after_writer_lock_releases_while_lease_is_still_valid(self) -> None:
        database = (self.root / ".engineering" / "engineering.db").resolve()
        lease = acquire(
            self.root, "inbox-lease", identity="host", instance_id="instance-a", central_database=database,
        )
        blocker = sqlite3.connect(database, isolation_level=None)
        blocker.execute("PRAGMA busy_timeout=10000")
        blocker.execute("BEGIN IMMEDIATE")
        started = Event()
        lock_attempted = Event()
        outcome: list[object] = []
        import engineering_platform.execution_lease as execution_lease
        original_connection = execution_lease._connection

        def traced_connection(*arguments: object, **keywords: object) -> sqlite3.Connection:
            connection = original_connection(*arguments, **keywords)
            class LockAttemptConnection:
                def execute(self, statement: str, *parameters: object) -> object:
                    if statement == "BEGIN IMMEDIATE":
                        lock_attempted.set()
                    return connection.execute(statement, *parameters)

                def close(self) -> None:
                    connection.close()

            return LockAttemptConnection()  # type: ignore[return-value]

        def renew() -> None:
            started.set()
            try:
                outcome.append(heartbeat(self.root, lease, central_database=database))
            except Exception as error:
                outcome.append(error)

        with patch("engineering_platform.execution_lease._connection", side_effect=traced_connection):
            worker = Thread(target=renew)
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertTrue(lock_attempted.wait(1))
            blocker.execute("COMMIT")
            blocker.close()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], Lease)
        renewed = outcome[0]
        assert isinstance(renewed, Lease)
        self.assertEqual(renewed.lease_id, lease.lease_id)
        self.assertGreater(renewed.expires_at, lease.expires_at)

    def test_background_heartbeat_retains_operation_and_original_cause(self) -> None:
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        pulse = LeaseHeartbeat(self.root, lease, interval_seconds=1)
        with patch("engineering_platform.execution_lease.heartbeat", side_effect=sqlite3.OperationalError("disk I/O error")), \
             patch.object(pulse._stop, "wait", side_effect=[False, True]):
            pulse._run()
        self.assertIsInstance(pulse.error, LeaseHeartbeatError)
        assert pulse.error is not None
        self.assertEqual(pulse.error.operation, "lease_heartbeat")
        self.assertIsInstance(pulse.error.__cause__, sqlite3.OperationalError)

    def test_history_retains_released_lease_evidence(self) -> None:
        lease = acquire(self.root, "inbox-lease", identity="host", instance_id="instance-a")
        release(self.root, lease)
        evidence = history(self.root, "inbox-lease")
        self.assertEqual(evidence["lease_state"], "RELEASED")
        self.assertEqual(evidence["host_instance_id"], "instance-a")
