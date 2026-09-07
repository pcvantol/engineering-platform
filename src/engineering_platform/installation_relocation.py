"""Crash-safe, installation-owned relocation of the database and File Inbox."""
from __future__ import annotations

import os
import json
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4


class RelocationError(ValueError):
    """A requested local relocation cannot be safely completed."""


def _directory(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RelocationError("LOCATION_REQUIRED")
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
        raise RelocationError("LOCATION_NOT_WRITABLE")
    return path.resolve()


_PENDING = "runtime/pending-relocation.json"
_INBOX_SYSTEM_ENTRIES = {"file-inbox-heartbeat.json", "incoming", "accepted", "processing", "quarantine"}


def _write_json_atomically(path: Path, value: dict[str, str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidate = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        candidate.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        candidate.chmod(0o600)
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def _pending_path(data_root: Path) -> Path:
    return data_root.resolve() / _PENDING


def _inbox_has_items(source: Path) -> bool:
    if any(item.name not in _INBOX_SYSTEM_ENTRIES for item in source.iterdir()):
        return True
    return any(any((source / name).iterdir()) for name in ("incoming", "accepted", "processing", "quarantine") if (source / name).is_dir())


def request(data_root: Path, kind: str, directory: object) -> dict[str, str]:
    """Validate and persist a move request; it runs on the next clean startup."""
    destination_directory = _directory(directory)
    root = data_root.resolve()
    if kind == "DATABASE":
        source, destination = root / "engineering.db", destination_directory / "engineering.db"
        if not source.is_file():
            raise RelocationError("DATABASE_UNAVAILABLE")
        if destination.exists() and destination.resolve() != source.resolve():
            raise RelocationError("DATABASE_DESTINATION_EXISTS")
    elif kind == "FILE_INBOX":
        source, destination = root / "file-inbox", destination_directory / "file-inbox"
        if not source.is_dir():
            raise RelocationError("INBOX_UNAVAILABLE")
        if _inbox_has_items(source):
            raise RelocationError("INBOX_NOT_EMPTY")
        if destination.exists() and any(destination.iterdir()):
            raise RelocationError("INBOX_DESTINATION_NOT_EMPTY")
    else:
        raise RelocationError("RELOCATION_KIND_UNKNOWN")
    pending = _pending_path(root)
    if pending.exists():
        raise RelocationError("RELOCATION_ALREADY_PENDING")
    _write_json_atomically(pending, {"kind": kind, "directory": str(destination_directory)})
    return {"previous": str(source.resolve()), "value": str(destination.resolve())}


def _replace_with_link(source: Path, destination: Path) -> None:
    candidate = source.with_name(f".{source.name}.relocating-{uuid4().hex}")
    candidate.symlink_to(destination, target_is_directory=destination.is_dir())
    os.replace(candidate, source)


def _replace_directory_with_link(source: Path, destination: Path) -> None:
    """macOS cannot replace a directory with a symlink in one rename."""
    retired = source.with_name(f".{source.name}.relocated-{uuid4().hex}")
    os.replace(source, retired)
    try:
        _replace_with_link(source, destination)
    except Exception:
        os.replace(retired, source)
        raise


def relocate_database(data_root: Path, directory: object) -> dict[str, str]:
    """Copy via SQLite backup, verify it, then atomically repoint the live path."""
    destination_directory = _directory(directory)
    source = data_root.resolve() / "engineering.db"
    previous = str(source.resolve())
    destination = destination_directory / "engineering.db"
    if not source.is_file():
        raise RelocationError("DATABASE_UNAVAILABLE")
    if destination.exists() and destination.resolve() == source.resolve():
        return {"previous": previous, "value": str(destination.resolve())}
    if destination.exists():
        raise RelocationError("DATABASE_DESTINATION_EXISTS")
    with tempfile.NamedTemporaryFile(prefix=".engineering-platform-", suffix=".db", dir=destination_directory, delete=False) as temporary:
        candidate = Path(temporary.name)
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as old, sqlite3.connect(candidate) as copy:
            old.backup(copy)
        with sqlite3.connect(f"file:{candidate}?mode=ro", uri=True) as verify:
            if verify.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RelocationError("DATABASE_INTEGRITY_FAILED")
        os.replace(candidate, destination)
        _replace_with_link(source, destination)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise
    return {"previous": previous, "value": str(destination.resolve())}


def relocate_inbox(data_root: Path, directory: object) -> dict[str, str]:
    """Create the destination and atomically repoint an empty File Inbox."""
    destination_directory = _directory(directory)
    source = data_root.resolve() / "file-inbox"
    previous = str(source.resolve())
    destination = destination_directory / "file-inbox"
    if not source.is_dir():
        raise RelocationError("INBOX_UNAVAILABLE")
    if _inbox_has_items(source):
        raise RelocationError("INBOX_NOT_EMPTY")
    if destination.exists() and any(destination.iterdir()):
        raise RelocationError("INBOX_DESTINATION_NOT_EMPTY")
    destination.mkdir(mode=0o700, exist_ok=True)
    for name in ("incoming", "accepted", "processing", "quarantine"):
        (destination / name).mkdir(mode=0o700, exist_ok=True)
    _replace_directory_with_link(source, destination)
    return {"previous": previous, "value": str(destination.resolve())}


def apply_pending(data_root: Path) -> dict[str, str] | None:
    """Perform the durable request before any writer services are started."""
    pending = _pending_path(data_root)
    if not pending.exists():
        return None
    try:
        request_data = json.loads(pending.read_text(encoding="utf-8"))
        kind, directory = request_data["kind"], request_data["directory"]
        if kind == "DATABASE":
            result = relocate_database(data_root, directory)
        elif kind == "FILE_INBOX":
            result = relocate_inbox(data_root, directory)
        else:
            raise RelocationError("RELOCATION_KIND_UNKNOWN")
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RelocationError("RELOCATION_REQUEST_INVALID") from error
    pending.unlink()
    return {**result, "kind": kind}
