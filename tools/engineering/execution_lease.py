"""Canonical active-run ownership leases backed solely by Engineering SQLite."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
from threading import Event, Thread
import uuid

from .storage import EngineeringStorageError, open_storage

LEASE_VERSION = 1
HEARTBEAT_INTERVAL_SECONDS = 15
LEASE_TIMEOUT_SECONDS = 90
TERMINAL_PHASES = frozenset({"COMPLETE", "BLOCKED", "FAILED"})


class LeaseConflictError(EngineeringStorageError):
    """Raised when another non-expired host instance owns the run."""


@dataclass(frozen=True)
class Lease:
    lease_id: str
    run_id: str
    host_identity: str
    host_instance_id: str
    acquired_at: str
    last_heartbeat_at: str
    expires_at: str
    lease_state: str


def host_identity() -> str:
    return socket.gethostname()[:120] or "execution-host"


def host_instance_id() -> str:
    return f"{uuid.uuid4()}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(connection: object, lease_id: str, run_id: str, event: str, outcome: str | None = None) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO execution_lease_events(lease_id,run_id,event_type,outcome,recorded_at) VALUES(?,?,?,?,?)",
        (lease_id, run_id, event, outcome or "", _now().isoformat()),
    )


class LeaseHeartbeat:
    """Bounded background heartbeat; lifecycle state remains runner-owned."""

    def __init__(self, root: Path, lease: Lease, *, interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS) -> None:
        self.root = root
        self.lease = lease
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self.error: Exception | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name=f"engineering-lease-{self.lease.run_id}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.lease = heartbeat(self.root, self.lease)
            except Exception as error:  # The expiry boundary remains fail-closed.
                self.error = error
                return

    def stop(self) -> Lease:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1, self.interval_seconds))
        return self.lease


def acquire(root: Path, run_id: str, *, identity: str, instance_id: str, process_id: int | None = None, timeout_seconds: int = LEASE_TIMEOUT_SECONDS) -> Lease:
    if not 0 < HEARTBEAT_INTERVAL_SECONDS < timeout_seconds:
        raise EngineeringStorageError("Active-run lease policy is invalid.")
    now = _now()
    expiry = now + timedelta(seconds=timeout_seconds)
    connection = open_storage(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE execution_run_leases SET lease_state='EXPIRED',updated_at=? WHERE run_id=? AND lease_state='ACTIVE' AND expires_at<?",
            (now.isoformat(), run_id, now.isoformat()),
        )
        active = connection.execute(
            "SELECT lease_id,host_instance_id FROM execution_run_leases WHERE run_id=? AND lease_state='ACTIVE'", (run_id,)
        ).fetchone()
        if active:
            raise LeaseConflictError("A live Execution Host instance already owns this run.")
        lease_id = f"lease-{uuid.uuid4()}"
        values = (lease_id, run_id, identity, instance_id, process_id, now.isoformat(), now.isoformat(), expiry.isoformat(), "ACTIVE", LEASE_VERSION, now.isoformat(), now.isoformat())
        connection.execute("INSERT INTO execution_run_leases(lease_id,run_id,host_identity,host_instance_id,process_id,acquired_at,last_heartbeat_at,expires_at,lease_state,lease_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", values)
        _event(connection, lease_id, run_id, "LEASE_ACQUIRED")
        connection.execute("COMMIT")
        return Lease(lease_id, run_id, identity, instance_id, now.isoformat(), now.isoformat(), expiry.isoformat(), "ACTIVE")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def heartbeat(root: Path, lease: Lease, *, timeout_seconds: int = LEASE_TIMEOUT_SECONDS) -> Lease:
    now = _now(); expiry = now + timedelta(seconds=timeout_seconds)
    connection = open_storage(root)
    try:
        updated = connection.execute("UPDATE execution_run_leases SET last_heartbeat_at=?,expires_at=?,updated_at=? WHERE lease_id=? AND run_id=? AND host_instance_id=? AND lease_state='ACTIVE'", (now.isoformat(), expiry.isoformat(), now.isoformat(), lease.lease_id, lease.run_id, lease.host_instance_id)).rowcount
        if updated != 1:
            raise LeaseConflictError("Execution Host no longer owns the active-run lease.")
    finally:
        connection.close()
    return Lease(lease.lease_id, lease.run_id, lease.host_identity, lease.host_instance_id, lease.acquired_at, now.isoformat(), expiry.isoformat(), "ACTIVE")


def release(root: Path, lease: Lease) -> None:
    connection = open_storage(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute("UPDATE execution_run_leases SET lease_state='RELEASED',updated_at=? WHERE lease_id=? AND host_instance_id=? AND lease_state='ACTIVE'", (_now().isoformat(), lease.lease_id, lease.host_instance_id)).rowcount
        if changed:
            _event(connection, lease.lease_id, lease.run_id, "LEASE_RELEASED")
        connection.execute("COMMIT")
    finally:
        connection.close()


def reconcile_stale(root: Path) -> list[dict[str, str]]:
    """Reconcile datastore ownership only; never fabricate a terminal lifecycle state."""
    now = _now().isoformat(); outcomes: list[dict[str, str]] = []
    connection = open_storage(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT lease_id,run_id,host_instance_id,last_heartbeat_at FROM execution_run_leases WHERE lease_state='ACTIVE' AND expires_at<?", (now,)).fetchall()
        for lease_id, run_id, instance, heartbeat_at in rows:
            transaction = connection.execute(
                "SELECT phase,payload FROM engineering_transactions WHERE run_id=?", (run_id,)
            ).fetchone()
            phase = transaction[0] if transaction else None
            terminal_phase: str | None = phase if phase in TERMINAL_PHASES else None
            if transaction and terminal_phase is None:
                try:
                    payload = json.loads(transaction[1])
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                candidate = payload.get("phase") if isinstance(payload, dict) and payload.get("terminal") is True else None
                if candidate in TERMINAL_PHASES:
                    terminal_phase = candidate
                    connection.execute(
                        "UPDATE engineering_transactions SET phase=?,updated_at=? WHERE run_id=?",
                        (terminal_phase, now, run_id),
                    )
            outcome = "TERMINAL_EVIDENCE_PRESENT" if terminal_phase else "RECOVERABLE"
            connection.execute("UPDATE execution_run_leases SET lease_state='EXPIRED',updated_at=? WHERE lease_id=?", (now, lease_id))
            _event(connection, lease_id, run_id, "LEASE_EXPIRED")
            _event(connection, lease_id, run_id, "STALE_DETECTED", outcome)
            _event(connection, lease_id, run_id, "STALE_RECONCILED", outcome)
            connection.execute(
                "INSERT INTO execution_run_reconciliations(run_id,outcome,reason,reconciled_at,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET outcome=excluded.outcome,reason=excluded.reason,reconciled_at=excluded.reconciled_at,updated_at=excluded.updated_at",
                (run_id, outcome, f"lease_expired;terminal_phase={terminal_phase or 'none'}", now, now),
            )
            outcomes.append({"run_id": run_id, "host_instance_id": instance, "last_heartbeat": heartbeat_at, "outcome": outcome})
        active_runs = connection.execute(
            "SELECT run_id,phase FROM engineering_transactions WHERE phase NOT IN ('COMPLETE','BLOCKED','FAILED')"
        ).fetchall()
        for run_id, phase in active_runs:
            has_live_lease = connection.execute(
                "SELECT 1 FROM execution_run_leases WHERE run_id=? AND lease_state='ACTIVE' AND expires_at>=?",
                (run_id, now),
            ).fetchone()
            if has_live_lease:
                continue
            prior = connection.execute(
                "SELECT 1 FROM execution_run_reconciliations WHERE run_id=?", (run_id,)
            ).fetchone()
            if prior:
                continue
            outcome = "INCONSISTENT" if phase in TERMINAL_PHASES else "OPERATOR_INTERVENTION_REQUIRED"
            connection.execute(
                "INSERT INTO execution_run_reconciliations(run_id,outcome,reason,reconciled_at,updated_at) VALUES(?,?,?,?,?)",
                (run_id, outcome, "active_transaction_without_live_lease", now, now),
            )
            outcomes.append({"run_id": run_id, "host_instance_id": "unknown", "last_heartbeat": "unknown", "outcome": outcome})
        connection.execute("COMMIT")
    finally:
        connection.close()
    return outcomes


def liveness(root: Path, run_id: object) -> dict[str, object]:
    """Project canonical liveness without consulting status files or processes."""
    if not isinstance(run_id, str):
        return {"state": "UNAVAILABLE"}
    now = _now().isoformat(); connection = open_storage(root)
    try:
        row = connection.execute("SELECT host_identity,host_instance_id,last_heartbeat_at,expires_at,lease_state FROM execution_run_leases WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
        reconciliation = connection.execute(
            "SELECT outcome,reason,reconciled_at FROM execution_run_reconciliations WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        result: dict[str, object] = {"state": "STALE"}
        if reconciliation:
            result.update({"reconciliation_outcome": reconciliation[0], "reconciliation_reason": reconciliation[1], "reconciled_at": reconciliation[2]})
        return result
    identity, instance, heartbeat_at, expires_at, state = row
    result = {"state": "LIVE" if state == "ACTIVE" and expires_at >= now else "STALE", "lease_state": state, "host_identity": identity, "host_instance_id": instance, "last_heartbeat": heartbeat_at, "lease_expiry": expires_at}
    if reconciliation:
        result.update({"reconciliation_outcome": reconciliation[0], "reconciliation_reason": reconciliation[1], "reconciled_at": reconciliation[2]})
    return result


def history(root: Path, run_id: str) -> dict[str, object]:
    """Return bounded canonical lease evidence for reports and history views."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT host_identity,host_instance_id,acquired_at,last_heartbeat_at,expires_at,lease_state "
            "FROM execution_run_leases WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return {"state": "UNAVAILABLE"}
    return {"host_identity": row[0], "host_instance_id": row[1], "acquired_at": row[2], "last_heartbeat": row[3], "lease_expiry": row[4], "lease_state": row[5]}
