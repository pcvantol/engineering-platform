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
import subprocess
from collections.abc import Iterator, Mapping

from .agent_state import redact_diagnostic
from .storage import EngineeringStorageError, open_storage

LOG_LEVEL_ENVIRONMENT = "DJCONNECT_ENGINEERING_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"
MAX_LOG_BYTES = 1_000_000
BACKUP_COUNT = 3
VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
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

    def __init__(self, root: Path, component: str) -> None:
        super().__init__()
        self.root = root.resolve()
        self.component = component
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
            connection = open_storage(self.root)
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


def component_logger(root: Path, component: str, *, level: str | None = None) -> logging.Logger:
    """Return the single private SQLite logger for one EP component."""
    logger = logging.getLogger(f"djconnect.engineering.{component}")
    logger.setLevel(configured_level(level))
    logger.propagate = False
    for handler in tuple(logger.handlers):
        if isinstance(handler, SQLiteLogHandler) and handler.root == root.resolve():
            handler.setLevel(logger.level)
            return logger
        logger.removeHandler(handler)
        handler.close()
    handler = SQLiteLogHandler(root, component)
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
        result = subprocess.run(
            ("git", "-C", str(root.resolve()), "rev-parse", "--short=12", "HEAD"),
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
        commit = result.stdout.strip() if result.returncode == 0 else "onbekend"
    except (OSError, subprocess.SubprocessError):
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
