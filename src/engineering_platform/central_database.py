"""Installation-owned CENTRAL database inspection, backup, and maintenance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile


DATABASE_FILENAME = "engineering.db"
MAINTENANCE_INTERVAL_KEY = "central_database.maintenance_interval_seconds"
MAINTENANCE_LAST_ATTEMPT_KEY = "central_database.maintenance_last_attempt_at"
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 60 * 60
MAINTENANCE_INTERVAL_OPTIONS = frozenset({60, 60 * 60, 24 * 60 * 60, 7 * 24 * 60 * 60})


def path(data_root: Path) -> Path:
    return data_root.resolve() / DATABASE_FILENAME


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def details(data_root: Path) -> dict[str, object]:
    """Read CENTRAL identity facts without creating or mutating it."""
    database = path(data_root)
    result: dict[str, object] = {"path": str(database), "size_bytes": 0, "schema_version": 0, "integrity": "UNAVAILABLE"}
    try:
        result["size_bytes"] = database.stat().st_size
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            result["schema_version"] = _schema_version(connection)
            result["integrity"] = "PASS" if [str(row[0]) for row in connection.execute("PRAGMA integrity_check")] == ["ok"] else "FAILED"
    except (OSError, sqlite3.DatabaseError):
        pass
    return result


def snapshot(data_root: Path) -> bytes | None:
    """Return a consistent, read-only backup of the one CENTRAL database."""
    database = path(data_root)
    if not database.is_file():
        return None
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="ep-central-backup-", suffix=".db", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source, sqlite3.connect(temporary_path) as backup:
            source.backup(backup)
        return temporary_path.read_bytes()
    except (OSError, sqlite3.DatabaseError):
        return None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def maintenance_configuration(data_root: Path) -> dict[str, int]:
    try:
        with sqlite3.connect(f"file:{path(data_root)}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT value FROM engineering_metadata WHERE key=?", (MAINTENANCE_INTERVAL_KEY,)).fetchone()
    except (OSError, sqlite3.DatabaseError):
        return {"interval_seconds": DEFAULT_MAINTENANCE_INTERVAL_SECONDS}
    try:
        value = int(json.loads(row[0])) if row else DEFAULT_MAINTENANCE_INTERVAL_SECONDS
    except (TypeError, ValueError, json.JSONDecodeError):
        value = DEFAULT_MAINTENANCE_INTERVAL_SECONDS
    return {"interval_seconds": value if value in MAINTENANCE_INTERVAL_OPTIONS else DEFAULT_MAINTENANCE_INTERVAL_SECONDS}


def update_maintenance_configuration(data_root: Path, interval_seconds: object) -> dict[str, int]:
    if not isinstance(interval_seconds, int) or isinstance(interval_seconds, bool) or interval_seconds not in MAINTENANCE_INTERVAL_OPTIONS:
        raise ValueError("CENTRAL_DATABASE_MAINTENANCE_INTERVAL_INVALID")
    with sqlite3.connect(path(data_root)) as connection:
        previous = maintenance_configuration(data_root)["interval_seconds"]
        connection.execute("INSERT INTO engineering_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (MAINTENANCE_INTERVAL_KEY, json.dumps(interval_seconds)))
    return {"previous": previous, "interval_seconds": interval_seconds}


def run_periodic_maintenance(data_root: Path, *, now: datetime | None = None) -> dict[str, object]:
    """Compact CENTRAL only while no lifecycle is active; never touch project stores."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    interval = maintenance_configuration(data_root)["interval_seconds"]
    try:
        with sqlite3.connect(path(data_root)) as connection:
            row = connection.execute("SELECT value FROM engineering_metadata WHERE key=?", (MAINTENANCE_LAST_ATTEMPT_KEY,)).fetchone()
            try:
                previous = datetime.fromisoformat(json.loads(row[0]).replace("Z", "+00:00")) if row else None
            except (TypeError, ValueError, json.JSONDecodeError):
                previous = None
            if previous is not None and moment - previous.astimezone(timezone.utc) < timedelta(seconds=interval):
                return {"state": "NOT_DUE"}
            active = connection.execute("SELECT 1 FROM ep_parity_lifecycle_dispatches WHERE state IN ('CLAIMED','RUNNING') LIMIT 1").fetchone()
            if active:
                return {"state": "SKIPPED_ACTIVE_RUN"}
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("PRAGMA optimize")
            connection.execute("VACUUM")
            connection.execute("INSERT INTO engineering_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (MAINTENANCE_LAST_ATTEMPT_KEY, json.dumps(moment.isoformat())))
        return {"state": "COMPACTED"}
    except (OSError, sqlite3.DatabaseError):
        return {"state": "DEFERRED"}
