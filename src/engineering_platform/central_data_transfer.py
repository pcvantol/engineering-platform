"""Validated export and deferred import of durable platform data.

An export intentionally contains *state*, not the installed Python runtime or
live process files. This lets a clean installation on another Mac retain its
own qualified runtime while receiving the complete CENTRAL state.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
from uuid import uuid4
import zipfile

from . import central_database


FORMAT_VERSION = 1
MANIFEST_NAME = "central-data-manifest.json"
PACKAGE_EXTENSION = ".epdata"
EXCLUDED_TOP_LEVEL = frozenset({"runtime"})
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000


class CentralDataTransferError(ValueError):
    """An archive is malformed, unsafe, or unavailable for transfer."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _content_checksum(entries: list[dict[str, object]]) -> str:
    """Checksum the complete canonical manifest of every durable file."""
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(canonical)


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or path.parts[0] in EXCLUDED_TOP_LEVEL:
        raise CentralDataTransferError("CENTRAL_ARCHIVE_MEMBER_INVALID")
    return path


def _durable_files(data_root: Path) -> list[tuple[PurePosixPath, Path]]:
    """List durable files by logical path, dereferencing legacy local links."""
    root = data_root.resolve()
    files: list[tuple[PurePosixPath, Path]] = []
    for top_level in sorted(root.iterdir(), key=lambda item: item.name):
        if top_level.name in EXCLUDED_TOP_LEVEL:
            continue
        logical = PurePosixPath(top_level.name)
        resolved = top_level.resolve()
        if resolved.is_file():
            files.append((logical, resolved))
        elif resolved.is_dir():
            for item in sorted(resolved.rglob("*")):
                if item.is_file():
                    files.append((logical / item.relative_to(resolved).as_posix(), item.resolve()))
    return files


def _database_schema_version(content: bytes) -> int:
    """Read the migration version from an archived SQLite database safely."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="ep-central-schema-", suffix=".db", delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        with sqlite3.connect(f"file:{temporary_path}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT MAX(version) FROM engineering_schema_migrations").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise CentralDataTransferError("CENTRAL_ARCHIVE_SCHEMA_INVALID") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def export_snapshot(data_root: Path) -> tuple[str, bytes]:
    """Create a portable immutable archive while Server writers are quiesced."""
    root = data_root.resolve()
    database = central_database.snapshot(root)
    if database is None:
        raise CentralDataTransferError("CENTRAL_DATABASE_UNAVAILABLE")
    entries: list[dict[str, object]] = []
    with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b") as output:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for logical, source in _durable_files(root):
                content = database if logical.as_posix() == "engineering.db" else source.read_bytes()
                archive.writestr(logical.as_posix(), content)
                entries.append({"path": logical.as_posix(), "sha256": _sha256(content), "size": len(content)})
            manifest = {
                "format_version": FORMAT_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "kind": "engineering-platform-central-data",
                "schema_version": central_database.details(root)["schema_version"],
                "entries": entries,
                "content_sha256": _content_checksum(entries),
            }
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        output.seek(0)
        content = output.read()
    filename = f"engineering-platform-central-data-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}{PACKAGE_EXTENSION}"
    return filename, content


def inspect_archive(path: Path) -> dict[str, object]:
    """Validate an archive completely before it can become a pending import."""
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise CentralDataTransferError("CENTRAL_ARCHIVE_SIZE_INVALID")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS or any(item.is_dir() for item in infos):
                raise CentralDataTransferError("CENTRAL_ARCHIVE_MEMBER_INVALID")
            names = {item.filename for item in infos}
            if MANIFEST_NAME not in names or len(names) != len(infos):
                raise CentralDataTransferError("CENTRAL_ARCHIVE_MANIFEST_INVALID")
            manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
            if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION or manifest.get("kind") != "engineering-platform-central-data":
                raise CentralDataTransferError("CENTRAL_ARCHIVE_MANIFEST_INVALID")
            schema_version = manifest.get("schema_version")
            if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 0:
                raise CentralDataTransferError("CENTRAL_ARCHIVE_SCHEMA_INVALID")
            entries = manifest.get("entries")
            if not isinstance(entries, list) or not entries:
                raise CentralDataTransferError("CENTRAL_ARCHIVE_MANIFEST_INVALID")
            expected: dict[str, dict[str, object]] = {}
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str) or not isinstance(entry.get("size"), int):
                    raise CentralDataTransferError("CENTRAL_ARCHIVE_MANIFEST_INVALID")
                name = _safe_member(entry["path"]).as_posix()
                expected[name] = entry
            checksum = manifest.get("content_sha256")
            if not isinstance(checksum, str) or len(checksum) != 64 or checksum != _content_checksum(entries):
                raise CentralDataTransferError("CENTRAL_ARCHIVE_INTEGRITY_INVALID")
            if set(expected) != names - {MANIFEST_NAME} or "engineering.db" not in expected:
                raise CentralDataTransferError("CENTRAL_ARCHIVE_MANIFEST_INVALID")
            for name, entry in expected.items():
                content = archive.read(name)
                if len(content) != entry["size"] or _sha256(content) != entry["sha256"]:
                    raise CentralDataTransferError("CENTRAL_ARCHIVE_INTEGRITY_INVALID")
            if _database_schema_version(archive.read("engineering.db")) != schema_version:
                raise CentralDataTransferError("CENTRAL_ARCHIVE_SCHEMA_INVALID")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CentralDataTransferError("CENTRAL_ARCHIVE_INVALID") from error
    return {"entries": len(expected), "size_bytes": path.stat().st_size, "schema_version": schema_version}


def stage_import(data_root: Path, source: Path) -> dict[str, object]:
    """Copy a validated upload into runtime-owned staging for a clean restart."""
    root = data_root.resolve()
    imports = root / "runtime" / "central-data-imports"
    imports.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = imports / f"{uuid4().hex}{PACKAGE_EXTENSION}"
    shutil.copyfile(source, target)
    target.chmod(0o600)
    details = inspect_archive(target)
    target_schema = central_database.details(root)["schema_version"]
    if details["schema_version"] != target_schema:
        target.unlink(missing_ok=True)
        raise CentralDataTransferError("CENTRAL_ARCHIVE_SCHEMA_INCOMPATIBLE")
    pending = root / "runtime" / "pending-central-data-import.json"
    if pending.exists():
        target.unlink(missing_ok=True)
        raise CentralDataTransferError("CENTRAL_IMPORT_ALREADY_PENDING")
    temporary = pending.with_name(f".{pending.name}.{uuid4().hex}")
    try:
        temporary.write_text(json.dumps({"archive": str(target)}, sort_keys=True), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, pending)
    finally:
        temporary.unlink(missing_ok=True)
    return details


def apply_pending_import(data_root: Path) -> dict[str, object] | None:
    """Replace durable state before initialization starts any writer.

    The current runtime stays in place, which is essential when restoring on a
    clean host with its own compatible EP installation.
    """
    root = data_root.resolve()
    pending = root / "runtime" / "pending-central-data-import.json"
    if not pending.exists():
        return None
    try:
        payload = json.loads(pending.read_text(encoding="utf-8"))
        archive_path = Path(payload["archive"])
        imports = (root / "runtime" / "central-data-imports").resolve()
        if imports not in archive_path.resolve().parents:
            raise CentralDataTransferError("CENTRAL_IMPORT_REQUEST_INVALID")
        details = inspect_archive(archive_path)
        staging = Path(tempfile.mkdtemp(prefix=".central-data-import-", dir=root.parent))
        retired = root.with_name(f".{root.name}.pre-import-{uuid4().hex}")
        try:
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
                for entry in manifest["entries"]:
                    logical = _safe_member(entry["path"])
                    target = staging.joinpath(*logical.parts)
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    target.write_bytes(archive.read(logical.as_posix()))
                    target.chmod(0o600)
            # Switch a fully prepared replacement directory, then transplant
            # only the target host's installed runtime. A failure restores the
            # original root rather than leaving a partially copied state.
            os.replace(root, retired)
            try:
                os.replace(staging, root)
                os.replace(retired / "runtime", root / "runtime")
            except Exception:
                if root.exists():
                    if (root / "runtime").exists() and not (retired / "runtime").exists():
                        os.replace(root / "runtime", retired / "runtime")
                    shutil.rmtree(root)
                os.replace(retired, root)
                raise
            shutil.rmtree(retired)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise CentralDataTransferError("CENTRAL_IMPORT_REQUEST_INVALID") from error
    pending.unlink(missing_ok=True)
    archive_path.unlink(missing_ok=True)
    return details
