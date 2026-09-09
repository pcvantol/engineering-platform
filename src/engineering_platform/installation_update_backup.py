"""Verified, retained CENTRAL backup action for one EP update operation."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile

from . import central_database
from .installation_update_plan import InstallationUpdatePlan


class InstallationUpdateBackupError(ValueError):
    """The operation cannot retain a verified CENTRAL recovery artifact."""


def _evidence(destination: Path) -> dict[str, object]:
    """Return durable evidence only for a readable, consistent SQLite backup."""
    try:
        with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as connection:
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        payload = destination.read_bytes()
    except (OSError, sqlite3.DatabaseError):
        raise InstallationUpdateBackupError("CENTRAL backup could not be verified") from None
    if integrity != ["ok"]:
        raise InstallationUpdateBackupError("CENTRAL backup integrity verification failed")
    return {
        "path": str(destination),
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "integrity": "PASS",
        "retention": "UPDATE_RECOVERY",
    }


def backup(plan: InstallationUpdatePlan) -> dict[str, object]:
    """Create an fsynced, integrity-checked backup outside disposable targets."""
    root = Path(plan.data_root).resolve()
    destination = root / "operations" / plan.operation_id / "backup" / "central.sqlite"
    if destination.exists():
        return _evidence(destination)
    payload = central_database.snapshot(root)
    if payload is None:
        raise InstallationUpdateBackupError("CENTRAL backup source is unavailable")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".central-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, destination)
    except (OSError, sqlite3.DatabaseError):
        Path(temporary).unlink(missing_ok=True)
        raise InstallationUpdateBackupError("CENTRAL backup could not be retained") from None
    return _evidence(destination)
