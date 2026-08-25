"""Bounded, local-only preferences for the private Engineering dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import json

from .storage import open_storage


DEFAULTS = {
    "log_retention_days": 30,
    "log_level": "INFO",
}
OPTIONS = {
    "log_retention_days": frozenset({30, 60, 90, 120, 180, 360}),
    "log_level": frozenset({"INFO", "DEBUG"}),
}
PREFIX = "dashboard_configuration."
INBOX_ROOT_KEY = PREFIX + "inbox_root"


def get(root: Path) -> dict[str, object]:
    connection = open_storage(root)
    try:
        values = dict(DEFAULTS)
        for key, raw in connection.execute(
            "SELECT key,value FROM engineering_metadata WHERE key LIKE ?", (PREFIX + "%",)
        ):
            name = str(key).removeprefix(PREFIX)
            try:
                value = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if name in OPTIONS and value in OPTIONS[name]:
                values[name] = value
        return values
    finally:
        connection.close()


def update(root: Path, key: str, value: object) -> dict[str, object]:
    if key not in OPTIONS or value not in OPTIONS[key]:
        raise ValueError("Ongeldige dashboardinstelling.")
    connection = open_storage(root)
    try:
        previous = DEFAULTS[key]
        row = connection.execute(
            "SELECT value FROM engineering_metadata WHERE key=?", (PREFIX + key,)
        ).fetchone()
        if row is not None:
            try:
                stored = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                stored = previous
            if stored in OPTIONS[key]:
                previous = stored
        connection.execute(
            "INSERT INTO engineering_metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (PREFIX + key, json.dumps(value)),
        )
        return {"key": key, "previous": previous, "value": value,
                "changed_at": datetime.now(timezone.utc).isoformat()}
    finally:
        connection.close()


def inbox_root(root: Path) -> Path | None:
    """Return the validated host-owned Inbox root override, when configured."""
    connection = open_storage(root)
    try:
        row = connection.execute(
            "SELECT value FROM engineering_metadata WHERE key=?", (INBOX_ROOT_KEY,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    try:
        raw = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, str):
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return None
    return candidate.resolve()


def update_inbox_root(root: Path, value: object) -> dict[str, object]:
    """Persist a writable existing Inbox root only after local validation."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Kies een bestaande lokale Inbox-map.")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("De Inbox-locatie moet een absoluut lokaal pad zijn.")
    candidate = candidate.resolve()
    inbox = candidate / "Inbox"
    if not candidate.is_dir() or not inbox.is_dir() or not os.access(inbox, os.W_OK):
        raise ValueError("De gekozen map bevat geen beschrijfbare Inbox-map.")
    previous = inbox_root(root)
    connection = open_storage(root)
    try:
        connection.execute(
            "INSERT INTO engineering_metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (INBOX_ROOT_KEY, json.dumps(str(candidate))),
        )
    finally:
        connection.close()
    return {
        "key": "inbox_root",
        "previous": str(previous) if previous else None,
        "value": str(candidate),
        "changed_at": datetime.now(timezone.utc).isoformat(),
    }
