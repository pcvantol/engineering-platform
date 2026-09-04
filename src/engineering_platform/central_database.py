"""Installation-owned CENTRAL database inspection, backup, and maintenance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile


DATABASE_FILENAME = "engineering.db"
MAINTENANCE_INTERVAL_KEY = "central_database.maintenance_interval_seconds"
MAINTENANCE_LAST_ATTEMPT_KEY = "central_database.maintenance_last_attempt_at"
PROVIDER_CAPACITY_HISTORY_KEY = "ep.provider_capacity_history.v1"
CODEX_CAPACITY_RESERVE_KEY = "ep.codex_capacity_reserve_percent"
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 60 * 60
MAINTENANCE_INTERVAL_OPTIONS = frozenset({60, 60 * 60, 24 * 60 * 60, 7 * 24 * 60 * 60})
CODEX_CAPACITY_RESERVE_OPTIONS = frozenset({0, 5, 10, 15, 20, 25, 50, 75})
CONSOLE_CONFIGURATION_OPTIONS = {
    "log_retention_days": frozenset({30, 60, 90, 120, 180, 360}), "telemetry_retention_days": frozenset({30, 60, 90, 120, 180, 360}),
    "log_level": frozenset({"INFO", "DEBUG"}), "inbox_scan_interval_seconds": frozenset({5, 15, 30, 60}),
    "open_pr_check_interval_seconds": frozenset({30, 60}), "dashboard_stream_interval_seconds": frozenset(range(1, 11)),
    "provider_readiness_refresh_seconds": frozenset({60, 300, 600}), "platform_health_refresh_seconds": frozenset({5, 15, 30, 60}),
    "component_details_refresh_seconds": frozenset({5, 15, 30, 60}),
}
CONSOLE_CONFIGURATION_DEFAULTS = {"log_retention_days":30,"telemetry_retention_days":90,"log_level":"INFO","inbox_scan_interval_seconds":15,"open_pr_check_interval_seconds":30,"dashboard_stream_interval_seconds":1,"provider_readiness_refresh_seconds":300,"platform_health_refresh_seconds":15,"component_details_refresh_seconds":5}


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


def capacity_configuration(data_root: Path) -> dict[str, int]:
    """Return the one installation-wide admission reserve for Codex capacity."""
    try:
        with sqlite3.connect(f"file:{path(data_root)}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT value FROM engineering_metadata WHERE key=?", (CODEX_CAPACITY_RESERVE_KEY,)).fetchone()
        value = int(json.loads(row[0])) if row else 0
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError, json.JSONDecodeError):
        value = 0
    return {"codex_capacity_reserve_percent": value if value in CODEX_CAPACITY_RESERVE_OPTIONS else 0}


def capacity_reserve_from_environment() -> int:
    """Resolve the active Server's platform policy for worker-side admission."""
    configured_root = os.environ.get("EP_SERVER_DATA_ROOT")
    if not configured_root:
        return 0
    return capacity_configuration(Path(configured_root))["codex_capacity_reserve_percent"]


def update_capacity_configuration(data_root: Path, reserve_percent: object) -> dict[str, int]:
    """Persist an EP-owned reserve; projects cannot choose different limits."""
    if not isinstance(reserve_percent, int) or isinstance(reserve_percent, bool) or reserve_percent not in CODEX_CAPACITY_RESERVE_OPTIONS:
        raise ValueError("CODEX_CAPACITY_RESERVE_INVALID")
    previous = capacity_configuration(data_root)["codex_capacity_reserve_percent"]
    with sqlite3.connect(path(data_root)) as connection:
        connection.execute(
            "INSERT INTO engineering_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (CODEX_CAPACITY_RESERVE_KEY, json.dumps(reserve_percent)),
        )
    return {"previous": previous, "codex_capacity_reserve_percent": reserve_percent}


def console_interval_configuration(data_root: Path) -> dict[str, object]:
    result = dict(CONSOLE_CONFIGURATION_DEFAULTS)
    try:
        with sqlite3.connect(f"file:{path(data_root)}?mode=ro", uri=True) as connection:
            for key, value in connection.execute("SELECT key,value FROM engineering_metadata WHERE key LIKE 'console.%'"):
                name = str(key).removeprefix("console.")
                parsed = json.loads(value)
                if name in result and parsed in CONSOLE_CONFIGURATION_OPTIONS[name]: result[name] = parsed
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return result


def update_console_interval_configuration(data_root: Path, key: object, value: object) -> dict[str, object]:
    if not isinstance(key, str) or key not in CONSOLE_CONFIGURATION_OPTIONS or value not in CONSOLE_CONFIGURATION_OPTIONS[key]:
        raise ValueError("CONSOLE_CONFIGURATION_INVALID")
    previous = console_interval_configuration(data_root)[key]
    with sqlite3.connect(path(data_root)) as connection:
        connection.execute("INSERT INTO engineering_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("console." + key, json.dumps(value)))
    return {"key": key, "previous": previous, "value": value}


def record_provider_capacity(
    data_root: Path, *, provider: str, remaining_percent: float, observed_at: datetime | None = None,
) -> list[dict[str, object]]:
    """Store a bounded, account-wide two-hour capacity series in CENTRAL."""
    provider = provider.strip()[:120]
    try:
        remaining = float(remaining_percent)
    except (TypeError, ValueError):
        return []
    if not provider or not 0 <= remaining <= 100:
        return []
    timestamp = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bucket = timestamp.replace(hour=timestamp.hour - timestamp.hour % 2, minute=0, second=0, microsecond=0)
    cutoff = bucket - timedelta(days=7)
    try:
        with sqlite3.connect(path(data_root)) as connection:
            row = connection.execute("SELECT value FROM engineering_metadata WHERE key=?", (PROVIDER_CAPACITY_HISTORY_KEY,)).fetchone()
            try:
                payload = json.loads(row[0]) if row else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            providers = payload.get("providers") if isinstance(payload, dict) else None
            providers = providers if isinstance(providers, dict) else {}
            samples = providers.get(provider)
            samples = samples if isinstance(samples, dict) else {}
            key = bucket.isoformat()
            current = samples.get(key)
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                remaining = min(remaining, float(current))
            samples[key] = remaining
            filtered: dict[str, float] = {}
            for sample_at, sample_value in samples.items():
                try:
                    parsed = datetime.fromisoformat(str(sample_at)).astimezone(timezone.utc)
                except ValueError:
                    continue
                if parsed >= cutoff and isinstance(sample_value, (int, float)) and not isinstance(sample_value, bool) and 0 <= float(sample_value) <= 100:
                    filtered[str(sample_at)] = float(sample_value)
            providers[provider] = filtered
            connection.execute(
                "INSERT INTO engineering_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (PROVIDER_CAPACITY_HISTORY_KEY, json.dumps({"providers": providers}, separators=(",", ":"))),
            )
    except (OSError, sqlite3.DatabaseError):
        return []
    return provider_capacity_history(data_root, provider=provider)


def provider_capacity_history(data_root: Path, *, provider: str, hours: int = 168) -> list[dict[str, object]]:
    """Read CENTRAL-only provider capacity evidence; it is never project data."""
    provider = provider.strip()[:120]
    if not provider or hours < 1:
        return []
    try:
        with sqlite3.connect(f"file:{path(data_root)}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT value FROM engineering_metadata WHERE key=?", (PROVIDER_CAPACITY_HISTORY_KEY,)).fetchone()
        payload = json.loads(row[0]) if row else {}
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError, json.JSONDecodeError):
        return []
    samples = payload.get("providers", {}).get(provider, {}) if isinstance(payload, dict) and isinstance(payload.get("providers"), dict) else {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result: list[dict[str, object]] = []
    if not isinstance(samples, dict):
        return result
    for observed_at, remaining in sorted(samples.items()):
        try:
            parsed = datetime.fromisoformat(str(observed_at)).astimezone(timezone.utc)
        except ValueError:
            continue
        if parsed >= cutoff and isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and 0 <= float(remaining) <= 100:
            result.append({"at": str(observed_at), "remaining_percent": float(remaining)})
    return result


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
