"""Owner-operated recovery for one abandoned Managed execution checkout."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile

from .execution_errors import RunnerError
from .execution_repository import SubprocessRepositoryClient
from .local_repository_binding import resolve_execution_repository
from .providers import GitProvider


class ManagedWorkspaceRecoveryError(ValueError):
    """Stable fail-closed error for abandoned-workspace recovery."""


_OPERATION = re.compile(r"[a-z0-9][a-z0-9-]{7,95}")
_BRANCH = re.compile(r"(?!main$)[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_SHA = re.compile(r"[0-9a-f]{40}")
_MAX_FILES = 2048
_MAX_BYTES = 64 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def _command(provider: GitProvider, root: Path, *arguments: str) -> str:
    try:
        return provider.command(root, "git", *arguments)
    except RuntimeError as error:
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_GIT_FAILED") from error


def _relative_untracked(provider: GitProvider, root: Path) -> tuple[Path, ...]:
    raw = _command(provider, root, "ls-files", "--others", "--exclude-standard", "-z")
    values = tuple(value for value in raw.split("\0") if value)
    if len(values) > _MAX_FILES:
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_TOO_MANY_FILES")
    paths: list[Path] = []
    total = 0
    for value in values:
        posix = PurePosixPath(value)
        if posix.is_absolute() or not posix.parts or any(part in {"", ".", ".."} for part in posix.parts):
            raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_PATH_INVALID")
        source = root.joinpath(*posix.parts)
        try:
            metadata = source.lstat()
        except OSError as error:
            raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_SOURCE_UNAVAILABLE") from error
        if not source.is_file() or source.is_symlink():
            raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_SOURCE_UNSUPPORTED")
        total += metadata.st_size
        if total > _MAX_BYTES:
            raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_TOO_LARGE")
        paths.append(source)
    return tuple(paths)


def _write_recovery_backup(*, root: Path, destination: Path, operation_id: str,
                           project_id: str, repository_id: str, branch: str,
                           head_sha: str, provider: GitProvider) -> dict[str, object]:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists():
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_DESTINATION_EXISTS")
    staging = Path(tempfile.mkdtemp(prefix=f".{operation_id}-", dir=destination.parent))
    os.chmod(staging, 0o700)
    try:
        patch = staging / "tracked.patch"
        patch.write_text(_command(provider, root, "diff", "--binary", "--full-index", "HEAD"), encoding="utf-8")
        patch.chmod(0o600)
        untracked_root = staging / "untracked"
        untracked_root.mkdir(mode=0o700)
        files: list[dict[str, object]] = []
        for source in _relative_untracked(provider, root):
            relative = source.relative_to(root)
            target = untracked_root / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, target, follow_symlinks=False)
            target.chmod(0o600)
            if _digest(source) != _digest(target):
                raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_BACKUP_MISMATCH")
            files.append({"path": relative.as_posix(), "size": target.stat().st_size,
                          "sha256": _digest(target)})
        manifest: dict[str, object] = {
            "contract_version": "managed-workspace-recovery/v1",
            "operation_id": operation_id, "project_id": project_id,
            "repository_id": repository_id, "branch": branch, "head_sha": head_sha,
            "created_at": _now(), "tracked_patch": {"path": "tracked.patch", "sha256": _digest(patch)},
            "untracked_files": files,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
        os.replace(staging, destination)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def recover(*, connection: sqlite3.Connection, data_root: Path, project_id: str,
            repository_id: str, operation_id: str, expected_branch: str,
            expected_head: str, backup_root: Path,
            provider: GitProvider | None = None) -> dict[str, object]:
    """Preserve abandoned WIP, restore synchronized main, and remove its local branch."""
    if _OPERATION.fullmatch(operation_id) is None:
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_OPERATION_INVALID")
    if _BRANCH.fullmatch(expected_branch) is None or ".." in expected_branch or expected_branch.endswith("/"):
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_BRANCH_INVALID")
    if _SHA.fullmatch(expected_head) is None:
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_HEAD_INVALID")
    try:
        backup_root = backup_root.expanduser().resolve()
        data_root = data_root.resolve()
    except OSError as error:
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_PATH_UNAVAILABLE") from error
    if backup_root == data_root or backup_root.is_relative_to(data_root):
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_BACKUP_UNSAFE")
    active = connection.execute(
        "SELECT COUNT(*) FROM execution_run_leases WHERE lease_state='ACTIVE'"
    ).fetchone()
    if active is None or int(active[0]) != 0:
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_ACTIVE_LEASE")
    binding = resolve_execution_repository(
        connection, project_id=project_id, repository_id=repository_id, data_root=data_root,
    )
    root = binding.local_root
    git = provider or GitProvider()
    client = SubprocessRepositoryClient(git)
    before = client.inspect(root)
    if (before.branch != expected_branch or before.head_sha != expected_head
            or before.repository != client.trusted_origin_identity(root)
            or client.workspace_operation_active(root)):
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_EXPECTATION_MISMATCH")
    destination = backup_root / operation_id
    manifest = _write_recovery_backup(
        root=root, destination=destination, operation_id=operation_id,
        project_id=project_id, repository_id=repository_id, branch=expected_branch,
        head_sha=expected_head, provider=git,
    )
    _command(git, root, "reset", "--hard", expected_head)
    _command(git, root, "clean", "-fd")
    client.synchronize_main(root)
    if git.execute(root, "git", "show-ref", "--verify", "--quiet", f"refs/heads/{expected_branch}").returncode == 0:
        _command(git, root, "branch", "-D", expected_branch)
    after = client.inspect(root)
    if (after.branch != "main" or not after.clean or not after.main_contains_head
            or after.repository != before.repository):
        raise ManagedWorkspaceRecoveryError("MANAGED_WORKSPACE_RECOVERY_RESULT_UNCERTAIN")
    receipt = {
        **manifest, "completed_at": _now(), "result": "RECOVERED",
        "backup_path": str(destination), "final_branch": after.branch,
        "final_head_sha": after.head_sha, "final_clean": after.clean,
    }
    receipt_path = destination / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    receipt_path.chmod(0o600)
    return receipt
