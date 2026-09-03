"""Redacted, rotating local logs for Engineering Platform components."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sqlite3
from collections.abc import Iterable, Iterator, Mapping

from .agent_state import redact_diagnostic
from .dashboard_configuration import get as dashboard_configuration
from .storage import EngineeringStorageError, open_storage
from .providers import GitProvider

LOG_LEVEL_ENVIRONMENT = "DJCONNECT_ENGINEERING_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"
MAX_LOG_BYTES = 1_000_000
BACKUP_COUNT = 3
COMPONENT_LOG_PAGE_SIZE = 50
MAX_COMPONENT_LOG_PAGE_SIZE = 200
VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
LOG_LEVELS_AT_OR_ABOVE = {
    "INFO": ("INFO", "WARNING", "ERROR"),
    "WARNING": ("WARNING", "ERROR"),
    "ERROR": ("ERROR",),
}
LIFECYCLE_CONTEXT_KEYS = frozenset(
    {
        "application_version",
        "git_commit",
        "launchd_label",
        "launch_agent_path",
        "target_component",
        "shutdown_signal",
    }
)


def configured_level(value: str | None = None) -> int:
    """Resolve the supported local logging level without silently accepting typos."""
    name = (value or os.environ.get(LOG_LEVEL_ENVIRONMENT, DEFAULT_LOG_LEVEL)).upper()
    return getattr(logging, name if name in VALID_LEVELS else DEFAULT_LOG_LEVEL)


class SecureRotatingFileHandler(RotatingFileHandler):
    """Keep both a newly opened log and its rotated predecessor private."""

    def _open(self) -> object:
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


class RedactingJsonFormatter(logging.Formatter):
    """Persist only bounded, redacted structured component events."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "component": redact_diagnostic(str(getattr(record, "component", record.name)), limit=80),
            "run_id": redact_diagnostic(str(getattr(record, "run_id", "")), limit=80) or None,
            "event": redact_diagnostic(record.getMessage(), limit=500),
        }
        diagnostic = getattr(record, "diagnostic", None)
        if diagnostic is not None:
            payload["diagnostic"] = redact_diagnostic(str(diagnostic), limit=500)
        context = getattr(record, "context", {})
        if isinstance(context, Mapping):
            for key in sorted(LIFECYCLE_CONTEXT_KEYS):
                if (value := context.get(key)) is not None:
                    payload[key] = redact_diagnostic(str(value), limit=500)
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


class SQLiteLogHandler(logging.Handler):
    """Persist component events in canonical storage, with file fallback on failure."""

    def __init__(self, root: Path, component: str, *, central_database: Path | None = None) -> None:
        super().__init__()
        self.root = root.resolve()
        self.component = component
        self.central_database = central_database.resolve() if central_database is not None else None
        self._fallback: SecureRotatingFileHandler | None = None

    def _fallback_handler(self) -> SecureRotatingFileHandler:
        if self._fallback is None:
            directory = self.root / ".engineering" / "logs"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._fallback = SecureRotatingFileHandler(
                directory / f"{self.component}.log",
                maxBytes=MAX_LOG_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            self._fallback.setFormatter(self.formatter)
        return self._fallback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = self.format(record)
            parsed = json.loads(payload)
            created_at = parsed.get("timestamp")
            connection = (
                sqlite3.connect(self.central_database, isolation_level=None)
                if self.central_database is not None else open_storage(self.root)
            )
            try:
                connection.execute(
                    "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                    (self.component, payload, created_at),
                )
            finally:
                connection.close()
        except (EngineeringStorageError, OSError, sqlite3.DatabaseError, TypeError, ValueError):
            try:
                self._fallback_handler().emit(record)
            except OSError:
                pass

    def close(self) -> None:
        if self._fallback is not None:
            self._fallback.close()
        super().close()


def component_logger(
    root: Path, component: str, *, level: str | None = None,
    central_database: Path | None = None,
) -> logging.Logger:
    """Return the single private SQLite logger for one EP component."""
    logger = logging.getLogger(f"djconnect.engineering.{component}")
    configured = level
    if configured is None and central_database is None:
        try:
            configured = str(dashboard_configuration(root)["log_level"])
        except (EngineeringStorageError, KeyError, TypeError, ValueError):
            configured = None
    logger.setLevel(configured_level(configured))
    logger.propagate = False
    for handler in tuple(logger.handlers):
        if (
            isinstance(handler, SQLiteLogHandler)
            and handler.root == root.resolve()
            and handler.central_database == (central_database.resolve() if central_database is not None else None)
        ):
            handler.setLevel(logger.level)
            return logger
        logger.removeHandler(handler)
        handler.close()
    handler = SQLiteLogHandler(root, component, central_database=central_database)
    handler.setLevel(logger.level)
    handler.setFormatter(RedactingJsonFormatter())
    logger.addHandler(handler)
    return logger


def component_log(root: Path, component: str, *, limit: int = 100) -> bytes:
    """Read canonical SQLite logs; use private files only if SQLite is unavailable."""
    if component not in {"inbox", "dashboard"}:
        return b""
    try:
        connection = open_storage(root)
        try:
            rows = connection.execute(
                "SELECT payload FROM engineering_component_logs WHERE component=? ORDER BY id DESC LIMIT ?",
                (component, limit),
            ).fetchall()
        finally:
            connection.close()
        lines = [str(row[0]) for row in reversed(rows)]
        return ("\n".join(lines) or "Nog geen applicatielog beschikbaar.").encode()
    except (EngineeringStorageError, OSError, sqlite3.DatabaseError):
        return _fallback_component_log(root, component, limit=limit)


def component_log_page(
    root: Path,
    component: str,
    *,
    page: int = 1,
    page_size: int = COMPONENT_LOG_PAGE_SIZE,
    start_at: str | None = None,
    end_at: str | None = None,
    inclusive_end: bool = False,
    search: str = "",
    level: str = "",
    events: Iterable[str] = (),
    sort_key: str = "timestamp",
    direction: str = "desc",
) -> dict[str, object]:
    """Return one bounded, filtered page from canonical component-log storage.

    Filtering happens before pagination so a historical date, event or text
    search never disappears merely because newer rows filled an arbitrary
    client-side sample.
    """
    if component not in {"inbox", "dashboard"}:
        raise ValueError("Onbekende componentlog.")
    if not isinstance(page, int) or page < 1:
        raise ValueError("Ongeldige logpagina.")
    if not isinstance(page_size, int) or not 1 <= page_size <= MAX_COMPONENT_LOG_PAGE_SIZE:
        raise ValueError("Ongeldige logpaginagrootte.")
    normalized_level = level.upper().strip()
    if normalized_level and normalized_level not in VALID_LEVELS:
        raise ValueError("Ongeldig logniveau.")
    normalized_search = str(search).strip()
    if len(normalized_search) > 160:
        raise ValueError("Zoekterm voor logs is te lang.")
    normalized_events = tuple(
        sorted({str(event).strip() for event in events if str(event).strip()})
    )
    if len(normalized_events) > 50 or any(len(event) > 160 for event in normalized_events):
        raise ValueError("Ongeldige gebeurtenisfilter.")
    sort_columns = {
        "line": "id",
        "timestamp": "created_at",
        "level": "json_extract(payload, '$.level')",
        "event": "json_extract(payload, '$.event')",
        "runId": "COALESCE(json_extract(payload, '$.run_id'), '')",
        "details": "COALESCE(json_extract(payload, '$.diagnostic'), '')",
    }
    if sort_key not in sort_columns or direction not in {"asc", "desc"}:
        raise ValueError("Ongeldige logsorteervolgorde.")

    clauses = ["component=?"]
    parameters: list[object] = [component]
    if start_at:
        clauses.append("created_at >= ?")
        parameters.append(start_at)
    if end_at:
        clauses.append("created_at <= ?" if inclusive_end else "created_at < ?")
        parameters.append(end_at)
    if normalized_search:
        escaped = normalized_search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("LOWER(payload) LIKE ? ESCAPE '\\'")
        parameters.append(f"%{escaped}%")
    # The dashboard's level picker is a minimum-severity filter. DEBUG is
    # deliberately unbounded so it includes every retained record, including
    # a future or legacy level the dashboard does not yet classify.
    if normalized_level in LOG_LEVELS_AT_OR_ABOVE:
        levels = LOG_LEVELS_AT_OR_ABOVE[normalized_level]
        clauses.append("json_extract(payload, '$.level') IN (" + ",".join("?" for _ in levels) + ")")
        parameters.extend(levels)
    event_option_clauses = list(clauses)
    event_option_parameters = list(parameters)
    if normalized_events:
        clauses.append("json_extract(payload, '$.event') IN (" + ",".join("?" for _ in normalized_events) + ")")
        parameters.extend(normalized_events)
    where = " WHERE " + " AND ".join(clauses)

    try:
        connection = open_storage(root)
    except (EngineeringStorageError, OSError, sqlite3.DatabaseError):
        # Schema activation is deliberately deferred during a managed run.
        # The dashboard remains a read-only consumer, so it must retain its
        # bounded response contract rather than terminate the HTTP handler.
        return {
            "entries": [],
            "page": page,
            "page_size": page_size,
            "total": 0,
            "events": [],
        }
    try:
        total = int(connection.execute(
            "SELECT COUNT(*) FROM engineering_component_logs" + where,
            parameters,
        ).fetchone()[0])
        rows = connection.execute(
            "SELECT id,payload FROM engineering_component_logs" + where
            + f" ORDER BY {sort_columns[sort_key]} {direction.upper()}, id {direction.upper()} LIMIT ? OFFSET ?",
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
        event_rows = connection.execute(
            "SELECT DISTINCT json_extract(payload, '$.event') FROM engineering_component_logs WHERE "
            + " AND ".join(event_option_clauses)
            + " AND json_extract(payload, '$.event') IS NOT NULL ORDER BY 1 LIMIT 500",
            event_option_parameters,
        ).fetchall()
        records: list[dict[str, object]] = []
        for row_id, raw in rows:
            try:
                record = json.loads(str(raw))
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                record["line"] = int(row_id)
                records.append(record)
        return {
            "entries": records,
            "page": page,
            "page_size": page_size,
            "total": total,
            "events": [str(row[0]) for row in event_rows if row[0]],
        }
    finally:
        connection.close()


def component_log_version(root: Path, component: str) -> str:
    """Return a lightweight SQLite revision, falling back to legacy file metadata."""
    if component not in {"inbox", "dashboard"}:
        return "missing"
    try:
        connection = open_storage(root)
        try:
            count, newest = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM engineering_component_logs WHERE component=?",
                (component,),
            ).fetchone()
        finally:
            connection.close()
        return f"sqlite:{count}:{newest}"
    except (EngineeringStorageError, OSError, sqlite3.DatabaseError):
        try:
            observed = (root / ".engineering" / "logs" / f"{component}.log").stat()
            return f"fallback:{observed.st_mtime_ns}:{observed.st_size}"
        except OSError:
            return "missing"


def clear_component_log(root: Path, component: str) -> None:
    """Clear one canonical component log, falling back only when SQLite is unavailable."""
    if component not in {"inbox", "dashboard"}:
        raise ValueError("Onbekende componentlog.")
    try:
        connection = open_storage(root)
        try:
            connection.execute("DELETE FROM engineering_component_logs WHERE component=?", (component,))
        finally:
            connection.close()
        return
    except (EngineeringStorageError, sqlite3.DatabaseError):
        path = root / ".engineering" / "logs" / f"{component}.log"
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        except OSError as error:
            raise OSError("Applicatielog kon niet worden gewist.") from error


def prune_component_logs(root: Path, retention_days: int) -> None:
    """Remove only expired, local component-log rows for an approved retention period."""
    if retention_days not in {30, 60, 90, 120, 180, 360}:
        raise ValueError("Ongeldige logbewaartermijn.")
    cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86_400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    connection = open_storage(root)
    try:
        connection.execute(
            "DELETE FROM engineering_component_logs WHERE created_at < ?", (cutoff_iso,)
        )
    finally:
        connection.close()


def _fallback_component_log(root: Path, component: str, *, limit: int) -> bytes:
    try:
        lines = (root / ".engineering" / "logs" / f"{component}.log").read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return b"Nog geen applicatielog beschikbaar."
    return ("\n".join(lines[-limit:])[-64_000:] or "Nog geen applicatielog beschikbaar.").encode()


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    run_id: str | None = None,
    diagnostic: str | None = None,
    context: Mapping[str, object] | None = None,
) -> None:
    """Write one redacted, structured event without exposing arbitrary extras."""
    logger.log(
        level,
        redact_diagnostic(event, limit=500),
        extra={
            "component": logger.name.rsplit(".", 1)[-1],
            "run_id": run_id or "",
            "diagnostic": diagnostic,
            "context": dict(context or {}),
        },
    )


def component_lifecycle_context(
    root: Path,
    *,
    version: str,
    launchd_label: str,
    launch_agent_path: Path,
) -> dict[str, str]:
    """Return bounded, non-secret identity data for a component lifecycle event."""
    try:
        result = GitProvider().execute(root.resolve(), "git", "rev-parse", "--short=12", "HEAD")
        commit = result.stdout.strip() if result.returncode == 0 else "onbekend"
    except OSError:
        commit = "onbekend"
    return {
        "application_version": version,
        "git_commit": commit or "onbekend",
        "launchd_label": launchd_label,
        "launch_agent_path": str(launch_agent_path),
    }


@contextmanager
def shutdown_signal_logging(
    logger: logging.Logger,
    context: Mapping[str, object],
) -> Iterator[None]:
    """Log a managed shutdown request before ending the local service loop."""
    previous_handlers: dict[int, signal.Handlers] = {}

    def request_shutdown(signum: int, _frame: object) -> None:
        signal_name = signal.Signals(signum).name
        log_event(
            logger,
            logging.INFO,
            "component_shutdown_trigger_received",
            context={**context, "shutdown_signal": signal_name},
        )
        raise KeyboardInterrupt

    for current_signal in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[current_signal] = signal.getsignal(current_signal)
        signal.signal(current_signal, request_shutdown)
    try:
        yield
    finally:
        for current_signal, previous in previous_handlers.items():
            signal.signal(current_signal, previous)
