"""Bounded, idle-only SQLite maintenance for the Engineering Platform."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from .storage import EngineeringStorageError, open_storage
from .dashboard_configuration import get as dashboard_configuration


_LAST_ATTEMPT_KEY = "database_maintenance.last_attempt_at"
def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _last_attempt(connection: sqlite3.Connection) -> datetime | None:
    row = connection.execute(
        "SELECT value FROM engineering_metadata WHERE key=?", (_LAST_ATTEMPT_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        return _timestamp(json.loads(row[0]))
    except (TypeError, json.JSONDecodeError):
        return None


def _record_attempt(connection: sqlite3.Connection, moment: datetime) -> None:
    connection.execute(
        "INSERT INTO engineering_metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_LAST_ATTEMPT_KEY, json.dumps(moment.isoformat())),
    )


def _has_active_execution_lease(connection: sqlite3.Connection, moment: datetime) -> bool:
    """Use the canonical live lease as the maintenance exclusion boundary."""
    lease = connection.execute(
        "SELECT 1 FROM execution_run_leases "
        "WHERE lease_state='ACTIVE' AND expires_at>=? LIMIT 1",
        (moment.isoformat(),),
    ).fetchone()
    return lease is not None


def run_periodic_database_maintenance(
    root: Path, *, now: datetime | None = None,
) -> dict[str, object]:
    """Compact the local database at its configured safe idle interval.

    A new execution host must first acquire its SQLite lease.  The maintenance
    pass checks that canonical ownership boundary immediately before compacting;
    a concurrently arriving host is serialized by SQLite until this short pass
    finishes.  Existing evidence is never pruned or rewritten.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        connection = open_storage(root)
    except EngineeringStorageError:
        return {"state": "UNAVAILABLE"}
    try:
        interval_seconds = int(dashboard_configuration(root)["database_maintenance_interval_seconds"])
        previous = _last_attempt(connection)
        if previous is not None and moment - previous < timedelta(seconds=interval_seconds):
            return {"state": "NOT_DUE", "next_due_at": (previous + timedelta(seconds=interval_seconds)).isoformat()}
        if _has_active_execution_lease(connection, moment):
            _record_attempt(connection, moment)
            return {"state": "SKIPPED_ACTIVE_RUN"}
        # Do not wait behind an interactive dashboard read long enough to
        # impact the next watcher cycle. SQLite will serialize a newly admitted
        # run behind this operation instead of compacting an already live run.
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("PRAGMA optimize")
        connection.execute("VACUUM")
        _record_attempt(connection, moment)
        return {"state": "COMPACTED", "completed_at": moment.isoformat()}
    except sqlite3.DatabaseError:
        return {"state": "DEFERRED"}
    finally:
        connection.close()
