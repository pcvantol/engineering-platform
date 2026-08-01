"""Redacted, rotating local logs for Engineering Platform components."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

from .agent_state import redact_diagnostic

LOG_LEVEL_ENVIRONMENT = "DJCONNECT_ENGINEERING_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"
MAX_LOG_BYTES = 1_000_000
BACKUP_COUNT = 3
VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


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
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def component_logger(root: Path, component: str, *, level: str | None = None) -> logging.Logger:
    """Return the single private rotating logger for one EP component."""
    directory = root / ".djconnect" / "logs"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{component}.log"
    logger = logging.getLogger(f"djconnect.engineering.{component}")
    logger.setLevel(configured_level(level))
    logger.propagate = False
    for handler in tuple(logger.handlers):
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == path:
            handler.setLevel(logger.level)
            return logger
        logger.removeHandler(handler)
        handler.close()
    handler = SecureRotatingFileHandler(
        path, maxBytes=MAX_LOG_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(logger.level)
    handler.setFormatter(RedactingJsonFormatter())
    logger.addHandler(handler)
    return logger


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    run_id: str | None = None,
    diagnostic: str | None = None,
) -> None:
    """Write one redacted, structured event without exposing arbitrary extras."""
    logger.log(
        level,
        redact_diagnostic(event, limit=500),
        extra={
            "component": logger.name.rsplit(".", 1)[-1],
            "run_id": run_id or "",
            "diagnostic": diagnostic,
        },
    )
