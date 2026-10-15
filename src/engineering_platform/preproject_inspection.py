"""Owner-bound local INSPECT_READ; source handles never confer project identity.

The physical reader operates on no-follow directory descriptors. Transport and
the private inspection ledger cannot choose additional filesystem targets.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import time
import unicodedata
from datetime import datetime, timezone
import json

CONTRACT_VERSION = "1.0"
STORE_SCHEMA_VERSION = 1
PROFILE = "LOCAL_GENESIS_INSPECT_READ_V1"


class InspectionError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def component(value: object) -> str:
    """One literal placement component, never traversal or an aliased spelling."""
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)
            or value in {".", ".."} or unicodedata.normalize("NFC", value) != value):
        raise InspectionError("INSPECTION_PATH_INVALID")
    return value


@contextmanager
def directory(path: Path):
    """Open every absolute ancestor without following symlinks, retaining identity."""
    if not path.is_absolute() or ".." in path.parts:
        raise InspectionError("INSPECTION_PLACEMENT_INVALID", 403)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        yield fd
    finally:
        os.close(fd)


def identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def selected_bytes(parent: int, name: str, *, maximum: int, deadline: float) -> tuple[bytes, str]:
    """Bounded regular text input; FIFO/device and replacement races fail closed."""
    if not name or name in {".", ".."} or any(c in name for c in ("/", "\\", "\x00")):
        raise InspectionError("INSPECTION_PATH_INVALID")
    if time.monotonic() >= deadline:
        raise InspectionError("INSPECTION_TIME_LIMIT", 409)
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise InspectionError("INSPECTION_FILE_UNSUPPORTED", 422)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or identity(before) != identity(opened):
            raise InspectionError("INSPECTION_SOURCE_DRIFT", 409)
        raw = bytearray()
        while len(raw) <= maximum:
            if time.monotonic() >= deadline:
                raise InspectionError("INSPECTION_TIME_LIMIT", 409)
            chunk = os.read(fd, min(65536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(current):
            raise InspectionError("INSPECTION_SOURCE_DRIFT", 409)
        if len(raw) > maximum:
            raise InspectionError("INSPECTION_BYTE_LIMIT", 409)
        return bytes(raw), hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)


def relative_parts(value: object) -> list[str]:
    if (not isinstance(value, str) or len(value) > 256 or "\\" in value
            or value.startswith("/") or "\x00" in value):
        raise InspectionError("INSPECTION_PATH_INVALID")
    parts = value.split("/")
    if len(parts) > 16 or any(p in {"", ".", ".."} or unicodedata.normalize("NFC", p) != p for p in parts):
        raise InspectionError("INSPECTION_PATH_INVALID")
    return parts


def read_relative(root: int, path: str, *, maximum: int, deadline: float) -> bytes:
    parts = relative_parts(path)
    fd = os.dup(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return selected_bytes(fd, parts[-1], maximum=maximum, deadline=deadline)[0]
    finally:
        os.close(fd)


def inventory(root: int, *, deadline: float, maximum: int = 2048) -> dict[str, tuple[int, ...]]:
    """Metadata only; never follow links, read file bodies or execute tools."""
    result = {}
    def visit(fd: int, prefix: str, depth: int) -> None:
        if time.monotonic() >= deadline:
            raise InspectionError("INSPECTION_TIME_LIMIT", 409)
        names = []
        with os.scandir(fd) as candidates:
            for candidate in candidates:
                names.append(candidate.name)
                if len(result) + len(names) > maximum or time.monotonic() >= deadline:
                    raise InspectionError("INSPECTION_ENTRY_OR_TIME_LIMIT", 409)
        folded = [unicodedata.normalize("NFC", n).casefold() for n in names]
        if len(set(folded)) != len(folded) or any(unicodedata.normalize("NFC", n) != n for n in names):
            raise InspectionError("INSPECTION_NAME_COLLISION", 422)
        if len(result) + len(names) > maximum or depth > 16:
            raise InspectionError("INSPECTION_ENTRY_LIMIT", 409)
        for name in sorted(names):
            path = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            result[path] = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if identity(os.fstat(child)) != identity(info):
                        raise InspectionError("INSPECTION_SOURCE_DRIFT", 409)
                    visit(child, path + "/", depth + 1)
                finally:
                    os.close(child)
    visit(root, "", 0)
    return result


def placement(binding: dict[str, object]) -> tuple[int, int | None]:
    """Rewalk owning placement instead of trusting an earlier held descriptor."""
    root = Path(str(binding["workspace_root"]))
    with directory(root) as fd:
        if list(identity(os.fstat(fd))) != binding["root_identity"]:
            raise InspectionError("INSPECTION_SOURCE_DRIFT", 409)
        name = component(binding["child"])
        matches, count = [], 0
        with os.scandir(fd) as candidates:
            for candidate in candidates:
                count += 1
                if count > 2048:
                    raise InspectionError("INSPECTION_WORKSPACE_ENTRY_LIMIT", 409)
                if unicodedata.normalize("NFC", candidate.name).casefold() == name.casefold():
                    matches.append(candidate.name)
        if matches and matches != [name]:
            raise InspectionError("INSPECTION_NAME_COLLISION", 422)
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            if binding["target_identity"] is not None:
                raise InspectionError("INSPECTION_SOURCE_DRIFT", 409)
            return os.dup(fd), None
        if not stat.S_ISDIR(info.st_mode):
            raise InspectionError("INSPECTION_TARGET_UNSUPPORTED", 422)
        target = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        if (identity(os.fstat(target)) != identity(info)
                or list(identity(info)) != binding["target_identity"]):
            os.close(target)
            raise InspectionError("INSPECTION_SOURCE_DRIFT", 409)
        return os.dup(fd), target


def validate_selection(paths: object, content_paths: object) -> tuple[list[str], list[str]]:
    manifests = {"pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle"}
    if (not isinstance(paths, list) or not isinstance(content_paths, list)
            or len(paths) > 16 or not paths or len(set(paths)) != len(paths)
            or len(set(content_paths)) != len(content_paths) or not set(content_paths) <= set(paths)):
        raise InspectionError("INSPECTION_SELECTION_INVALID")
    for path in paths:
        parts = relative_parts(path)
        declaration = path == ".engineering-platform/repository.json"
        if (not declaration and (any(p.startswith(".") for p in parts)
                or not (path in manifests or Path(path).suffix.casefold() in {".md", ".txt", ".rst"}))) or any(
                s in path.casefold() for s in ("secret", "credential", "password", "private-key", "id_rsa", ".env")):
            raise InspectionError("INSPECTION_SELECTION_FORBIDDEN", 403)
        if path in content_paths and (declaration or path in manifests):
            raise InspectionError("INSPECTION_CONTENT_FORBIDDEN", 403)
    return sorted(paths), sorted(content_paths)


def scan(binding: dict[str, object], selection: list[str], content_paths: list[str], *, seconds: int = 10,
         isolated: bool = False) -> dict[str, object]:
    """The isolated worker's only physical operation. No process or network call."""
    from . import preproject_git
    deadline = time.monotonic() + seconds
    if isolated:
        # The owning parent rewalks root/direct-child placement before, during
        # and after this worker. The native worker has NO parent/sibling read
        # permission. Its only filesystem capability is the exact selected
        # target, whose device/inode must match before any content is read.
        root, target = None, None
        if binding["target_identity"] is not None:
            target = os.open(Path(str(binding["workspace_root"])) / str(binding["child"]),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if list(identity(os.fstat(target))) != binding["target_identity"]:
                os.close(target)
                raise InspectionError("INSPECTION_SOURCE_DRIFT", 409)
    else:
        root, target = placement(binding)
    try:
        observed = datetime.now(timezone.utc).isoformat()
        if target is None:
            result = {"state": "ABSENT", "git": None, "documents": [
                {"path": p, "state": "ABSENT", "sha256": None, "content": None} for p in selection],
                "observed_entries": 0, "selection": selection, "exclusions": ["ALL_UNSELECTED_CONTENT"],
                "assessments": "NOT_ASSESSED", "completeness": "COMPLETE_WITHIN_SELECTION", "observed_at": observed}
        else:
            before = inventory(target, deadline=deadline)
            git = preproject_git.observe(target, before, deadline=deadline)
            documents, total = [], 0
            declaration = {"state": "NOT_INSPECTED", "identity": None}
            for path in selection:
                parts = relative_parts(path)
                for end in range(1, len(parts)):
                    ancestor = "/".join(parts[:end])
                    if ancestor in before and not stat.S_ISDIR(before[ancestor][2]):
                        raise InspectionError("INSPECTION_ANCESTOR_UNSUPPORTED", 422)
                if path not in before:
                    documents.append({"path": path, "state": "ABSENT", "sha256": None, "content": None})
                    continue
                raw = read_relative(target, path, maximum=65536, deadline=deadline)
                total += len(raw)
                if total > 262144:
                    raise InspectionError("INSPECTION_BYTE_LIMIT", 409)
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise InspectionError("INSPECTION_TEXT_UNSUPPORTED", 422) from error
                if "\x00" in text:
                    raise InspectionError("INSPECTION_TEXT_UNSUPPORTED", 422)
                documents.append({"path": path, "state": "PRESENT", "sha256": hashlib.sha256(raw).hexdigest(),
                    "content": text if path in content_paths else None})
                if path == ".engineering-platform/repository.json":
                    from .repository_attachment import parse_repository_attachment, RepositoryAttachmentError
                    try:
                        parsed = parse_repository_attachment(json.loads(text))
                        declaration = {"state": "OBSERVED_DECLARATION_ONLY", "identity": {
                            "project_id": parsed.project_id, "repository_id": parsed.repository_id,
                            "authority_repository_id": parsed.authority_repository_id, "role": parsed.repository_role}}
                    except (ValueError, RepositoryAttachmentError):
                        declaration = {"state": "INVALID_OR_CONFLICTING", "identity": None}
            if before != inventory(target, deadline=deadline):
                raise InspectionError("INSPECTION_SOURCE_DRIFT", 409)
            result = {"state": "EMPTY_DIRECTORY" if not before else ("DIRECTORY" if git is None else git["state"]),
                "git": git, "documents": documents, "observed_entries": len(before), "selection": selection,
                "exclusions": ["ALL_UNSELECTED_CONTENT", "ASSESSMENT_AND_REPOSITORY_EXECUTION"],
                "assessments": "NOT_ASSESSED", "completeness": "COMPLETE_WITHIN_SELECTION", "observed_at": observed}
            if ".engineering-platform/repository.json" in selection and ".engineering-platform/repository.json" not in before:
                declaration = {"state": "ABSENT", "identity": None}
            result["canonical_declaration"] = declaration
        # Prove the descriptor is still the selected direct child, including absence.
        if isolated:
            if target is not None:
                current = os.stat(Path(str(binding["workspace_root"])) / str(binding["child"]), follow_symlinks=False)
                if not stat.S_ISDIR(current.st_mode) or list(identity(current)) != binding["target_identity"]:
                    raise InspectionError("INSPECTION_SOURCE_DRIFT", 409)
        else:
            check_root, check_target = placement(binding)
            os.close(check_root)
            if check_target is not None:
                os.close(check_target)
        result["source_fingerprint"] = hashlib.sha256(repr((binding["root_identity"], binding["target_identity"],
            result["git"], result["documents"])).encode()).hexdigest()
        if "canonical_declaration" not in result:
            result["canonical_declaration"] = {"state": "ABSENT" if ".engineering-platform/repository.json" in selection
                else "NOT_INSPECTED", "identity": None}
        return result
    finally:
        if root is not None:
            os.close(root)
        if target is not None:
            os.close(target)
