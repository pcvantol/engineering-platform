"""Owned FME snapshots and sandbox boundaries, outside the target checkout."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import uuid

from .effect_contract import EffectContractError, contains, require_redacted, safe_path


def child_environment() -> dict[str, str]:
    """No Git credentials, CENTRAL paths, provider tokens or inherited hooks."""
    return {"PATH": os.defpath + ":/opt/homebrew/bin:/usr/local/bin",
            "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0", "PYTHONDONTWRITEBYTECODE": "1"}


def git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ("git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
         "-c", "protocol.ext.allow=never", *args), cwd=root, input=input_bytes,
        env=child_environment(), capture_output=True, timeout=120, check=False,
    )
    if completed.returncode:
        raise EffectContractError("EFFECT_GIT_OBSERVATION_FAILED")
    return completed.stdout


def owned_directory(database: Path, target: Path, run_id: str) -> Path:
    root = database.resolve().parent / "artifacts" / "effects" / run_id
    if root.is_relative_to(target.resolve()) or target.resolve().is_relative_to(root):
        raise EffectContractError("EFFECT_STORAGE_INSIDE_TARGET")
    # All directories below the operator-owned CENTRAL root must be real.
    current = database.resolve().parent
    for part in ("artifacts", "effects", run_id):
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise EffectContractError("UNSAFE_EFFECT_STORAGE")
        if metadata.st_mode & 0o022:
            raise EffectContractError("SHARED_EFFECT_STORAGE")
    return root


def immutable_json(path: Path, payload: object) -> str:
    """Exclusive creation; a conflicting identity can never replace bytes."""
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    expected = hashlib.sha256(data).hexdigest()
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        read_json(path, expected)
        return expected
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return expected


def read_json(path: Path, expected: str | None = None) -> object:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 2_097_152:
            raise EffectContractError("UNSAFE_EFFECT_ARTIFACT")
        data = handle.read()
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        raise EffectContractError("EFFECT_ARTIFACT_INTEGRITY_FAILED")
    return json.loads(data)


def write_file(root: Path, relative: str, content: bytes) -> None:
    """Traverse held directory descriptors; refuse links and special files."""
    safe_path(relative)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            nested = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = nested
        try:
            metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            metadata = None
        if metadata is not None and (not stat.S_ISREG(metadata.st_mode)
                                     or metadata.st_nlink != 1 or metadata.st_mode & 0o111):
            raise EffectContractError("UNSAFE_EFFECT_DESTINATION")
        flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if metadata is None:
            flags |= os.O_CREAT | os.O_EXCL
        output = os.open(parts[-1], flags, 0o600, dir_fd=descriptor)
        with os.fdopen(output, "wb") as handle:
            current = os.fstat(handle.fileno())
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or (metadata is not None and (current.st_ino, current.st_dev)
                        != (metadata.st_ino, metadata.st_dev))):
                raise EffectContractError("EFFECT_DESTINATION_CHANGED")
            handle.truncate(0)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def snapshot(target: Path, destination: Path, contract: dict[str, object]) -> dict[str, str]:
    """Export only explicitly granted regular committed files, never .git."""
    if destination.exists():
        raise EffectContractError("EFFECT_SNAPSHOT_ALREADY_EXISTS")
    destination.mkdir(mode=0o700)
    revision = str(contract["source_revision"])
    entries = git(target, "ls-tree", "-rz", "--full-tree", revision).split(b"\x00")
    manifest = {}
    seen = set()
    total = 0
    for entry in filter(None, entries):
        metadata, encoded_path = entry.split(b"\t", 1)
        path = encoded_path.decode("utf-8")
        if not contains(contract["read_paths"], path):
            continue
        safe_path(path)
        mode, kind, object_id = metadata.decode("ascii").split()
        if kind != "blob" or mode != "100644" or path.casefold() in seen:
            raise EffectContractError("UNSAFE_EFFECT_SOURCE")
        content = git(target, "cat-file", "blob", object_id)
        total += len(content)
        if total > 8_388_608 or len(content) > 1_048_576 or b"\x00" in content:
            raise EffectContractError("EFFECT_SOURCE_LIMIT")
        require_redacted(content.decode("utf-8"))
        write_file(destination, path, content)
        manifest[path] = hashlib.sha256(content).hexdigest()
        seen.add(path.casefold())
    if (not manifest or any(not any(contains([scope], path) for path in manifest)
                            for scope in contract["read_paths"])):
        raise EffectContractError("EFFECT_SOURCE_SCOPE_UNAVAILABLE")
    return manifest


def verify_snapshot(root: Path, manifest: dict[str, str]) -> None:
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise EffectContractError("EFFECT_SOURCE_CHANGED")
    actual = set()
    for path in root.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        relative = str(path.relative_to(root))
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or relative not in manifest or metadata.st_size > 1_048_576
                or hashlib.sha256(path.read_bytes()).hexdigest() != manifest[relative]):
            raise EffectContractError("EFFECT_SOURCE_CHANGED")
        actual.add(relative)
    if actual != set(manifest):
        raise EffectContractError("EFFECT_SNAPSHOT_SCOPE_CHANGED")


def sandbox_options(root: Path, *, scratch: Path | None = None,
                    readable: tuple[Path, ...] = ()) -> tuple[str, ...]:
    filesystem = {":minimal": "read", str(root.resolve()): "read"}
    # The selected installed Python runtime may live outside OS runtime roots.
    for prefix in {sys.base_prefix, sys.prefix}:
        filesystem[str(Path(prefix).absolute())] = "read"
        filesystem[str(Path(prefix).resolve())] = "read"
    if scratch is not None:
        filesystem[str(scratch.resolve())] = "write"
    for path in readable:
        filesystem[str(path.resolve())] = "read"
    entries = ",".join(f"{json.dumps(key)}={json.dumps(value)}" for key, value in filesystem.items())
    return ("-c", f"permissions.ep-effects.filesystem={{{entries}}}",
            "-c", "permissions.ep-effects.network.enabled=false")


def sandbox_command(root: Path, command: tuple[str, ...], *, scratch: Path | None = None,
                    readable: tuple[Path, ...] = ()) -> tuple[str, ...]:
    binary = shutil.which("codex")
    if binary is None:
        raise EffectContractError("EFFECT_SANDBOX_UNAVAILABLE")
    # Execute the granted runtime directly; a venv symlink chain can traverse
    # ungranted intermediate installation paths even when its target is safe.
    if command and command[0] == sys.executable:
        command = (str(Path(sys.executable).resolve()), *command[1:])
    name = "ep-effects-" + uuid.uuid4().hex
    options = tuple(item.replace("ep-effects", name) for item in sandbox_options(root, scratch=scratch, readable=readable))
    return (binary, "sandbox", "-P", name, *options,
            "-C", str(root), "--", *command)


def run_control(command: tuple[str, ...], environment: dict[str, str], *, timeout: int = 1800) -> int:
    """Bound runtime and reap the entire process group owned by this call.

    Control output is deliberately not captured: a validator cannot exhaust
    host memory or persist source/credential material through its stdout.
    """
    with subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            return process.wait(timeout=timeout)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)


def cleanup_scratch(directory: Path) -> None:
    """Delete only private validation scratch; retain reports and source proof."""
    for path in directory.glob("validation-scratch-*"):
        if path.is_symlink() or not path.is_dir():
            raise EffectContractError("UNSAFE_EFFECT_SCRATCH")
        shutil.rmtree(path)
