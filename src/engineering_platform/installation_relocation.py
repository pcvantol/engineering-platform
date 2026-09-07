"""Crash-safe relocation of the one canonical platform-data directory.

The data root is one unit of persistence. Splitting the database and File
Inbox across arbitrary folders made a restore impossible to reason about, so
the legacy per-resource relocation requests are deliberately retired.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from . import central_database


class RelocationError(ValueError):
    """A requested local relocation cannot be safely completed."""


_PENDING = "runtime/pending-platform-data-relocation.json"
_PREPARED = ".engineering-platform-relocation-prepared"


def _directory(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RelocationError("LOCATION_REQUIRED")
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
        raise RelocationError("LOCATION_NOT_WRITABLE")
    return path.resolve()


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


def _destination(root: Path, directory: object) -> Path:
    parent = _directory(directory)
    target = parent / root.name
    if target == root or root in target.parents or target in root.parents:
        raise RelocationError("PLATFORM_DATA_DESTINATION_INVALID")
    return target


def _prepared_marker(destination: Path) -> Path:
    return destination / _PREPARED


def _remove_prepared_destination(destination: Path) -> None:
    """Remove exactly the empty, EP-owned destination prepared for a move."""
    marker = _prepared_marker(destination)
    if not marker.is_file() or marker.read_text(encoding="utf-8") != "prepared":
        raise RelocationError("PLATFORM_DATA_DESTINATION_NOT_PREPARED")
    marker.unlink()
    try:
        destination.rmdir()
    except OSError as error:
        # Never remove a directory to which an operator added content.
        marker.write_text("prepared", encoding="utf-8")
        raise RelocationError("PLATFORM_DATA_DESTINATION_NOT_EMPTY") from error


def prepare(data_root: Path, directory: object) -> dict[str, str]:
    """Create and prove write access to the exact future data-root folder."""
    root = Path(data_root).expanduser().resolve()
    if not central_database.path(root).is_file():
        raise RelocationError("PLATFORM_DATA_UNAVAILABLE")
    destination = _destination(root, directory)
    if destination.exists():
        raise RelocationError("PLATFORM_DATA_DESTINATION_EXISTS")
    destination.mkdir(mode=0o700)
    marker = _prepared_marker(destination)
    try:
        marker.write_text("prepared", encoding="utf-8")
        if marker.read_text(encoding="utf-8") != "prepared":
            raise OSError("PLATFORM_DATA_DESTINATION_NOT_WRITABLE")
    except Exception:
        marker.unlink(missing_ok=True)
        destination.rmdir()
        raise
    return {"previous": str(root), "directory": str(_directory(directory)), "value": str(destination)}


def discard_prepared(data_root: Path, directory: object) -> None:
    """Remove only the empty destination directory created by ``prepare``."""
    destination = _destination(Path(data_root).expanduser().resolve(), directory)
    marker = _prepared_marker(destination)
    if not marker.is_file() or marker.read_text(encoding="utf-8") != "prepared":
        return
    _remove_prepared_destination(destination)


def request(data_root: Path, kind: str, directory: object) -> dict[str, str]:
    """Persist one whole-platform move for the next clean Server startup."""
    if kind != "PLATFORM_DATA":
        raise RelocationError("RELOCATION_KIND_RETIRED")
    root = data_root.resolve()
    if not central_database.path(root).is_file():
        raise RelocationError("PLATFORM_DATA_UNAVAILABLE")
    destination = _destination(root, directory)
    if destination.exists() and not _prepared_marker(destination).is_file() and destination.resolve() != root:
        raise RelocationError("PLATFORM_DATA_DESTINATION_EXISTS")
    if not _prepared_marker(destination).is_file():
        raise RelocationError("PLATFORM_DATA_DESTINATION_NOT_PREPARED")
    pending = _pending_path(root)
    if pending.exists():
        raise RelocationError("RELOCATION_ALREADY_PENDING")
    _write_json_atomically(pending, {"kind": kind, "directory": str(_directory(directory))})
    return {"previous": str(root), "value": str(destination)}


def relocate_platform_data(data_root: Path, directory: object) -> dict[str, str]:
    """Move the entire data root to its new canonical location.

    A relocation is a real move: no symlink is retained at the old location.
    The service supervisor is repointed separately after this atomic rename.
    """
    root = Path(data_root).expanduser().resolve()
    parent = _directory(directory)
    destination = parent / root.name
    if destination == root or root in destination.parents or destination in root.parents:
        raise RelocationError("PLATFORM_DATA_DESTINATION_INVALID")
    if not root.is_dir() or not central_database.path(root).is_file():
        raise RelocationError("PLATFORM_DATA_UNAVAILABLE")
    if destination.exists() and not _prepared_marker(destination).is_file():
        raise RelocationError("PLATFORM_DATA_DESTINATION_EXISTS")
    if destination.exists():
        _remove_prepared_destination(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.replace(root, destination)
    return {"previous": str(root), "value": str(destination.resolve())}


def apply_pending(data_root: Path) -> dict[str, str] | None:
    """Perform a durable whole-directory request before writers are started."""
    pending = _pending_path(data_root)
    if not pending.exists():
        return None
    try:
        request_data = json.loads(pending.read_text(encoding="utf-8"))
        kind, directory = request_data["kind"], request_data["directory"]
        if kind != "PLATFORM_DATA":
            raise RelocationError("RELOCATION_KIND_RETIRED")
        result = relocate_platform_data(data_root, directory)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RelocationError("RELOCATION_REQUEST_INVALID") from error
    # The request file moved with the data root, so remove it there rather
    # than recreating an old location merely to clear it.
    _pending_path(Path(result["value"])).unlink(missing_ok=True)
    return {**result, "kind": "PLATFORM_DATA"}
