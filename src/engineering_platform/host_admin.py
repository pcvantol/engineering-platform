"""Server-owned, containment-safe Host Admin observations and mutation gate.

Host Admin never discovers a target from a request, project, checkout, CWD,
remote, browser, or Finder. Deployments provide an explicit registry; this
module resolves opaque identifiers from that registry only.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterable

from . import managed_codex_runtime
from .component_logging import component_logger, log_event

_TARGET_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")


class HostAdminTargetError(ValueError):
    """A requested Host Admin target is absent, stale, or unsafe."""


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise HostAdminTargetError("HOST_ADMIN_TARGET_STALE") from error


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class HostAdminTarget:
    """A deployment-approved Git/worktree boundary, never request-derived."""

    target_id: str
    containment_root: Path
    primary_worktree: Path
    worktrees: tuple[tuple[str, Path], ...] = ()

    def validated(self) -> "HostAdminTarget":
        if not _TARGET_ID.fullmatch(self.target_id):
            raise HostAdminTargetError("HOST_ADMIN_TARGET_INVALID")
        root, primary = _resolved(self.containment_root), _resolved(self.primary_worktree)
        if not root.is_dir() or not primary.is_dir() or not _contained(primary, root):
            raise HostAdminTargetError("HOST_ADMIN_TARGET_OUTSIDE_CONTAINMENT")
        seen: set[str] = set(); approved: list[tuple[str, Path]] = []
        for worktree_id, worktree in self.worktrees:
            if not _TARGET_ID.fullmatch(worktree_id) or worktree_id in seen:
                raise HostAdminTargetError("HOST_ADMIN_WORKTREE_TARGET_INVALID")
            candidate = _resolved(worktree)
            if not candidate.is_dir() or not _contained(candidate, root):
                raise HostAdminTargetError("HOST_ADMIN_WORKTREE_ESCAPE")
            seen.add(worktree_id); approved.append((worktree_id, candidate))
        return HostAdminTarget(self.target_id, root, primary, tuple(approved))


class HostAdminTargetRegistry:
    """The only source of mutable Host Admin target identity."""

    def __init__(self, targets: Iterable[HostAdminTarget] = ()) -> None:
        values = [target.validated() for target in targets]
        if len({target.target_id for target in values}) != len(values):
            raise HostAdminTargetError("HOST_ADMIN_TARGET_DUPLICATE")
        self._targets = {target.target_id: target for target in values}

    def target(self, target_id: object) -> HostAdminTarget:
        if not isinstance(target_id, str) or target_id not in self._targets:
            raise HostAdminTargetError("HOST_ADMIN_TARGET_UNKNOWN")
        return self._targets[target_id].validated()

    def worktree(self, target_id: object, worktree_id: object) -> Path:
        target = self.target(target_id)
        if not isinstance(worktree_id, str):
            raise HostAdminTargetError("HOST_ADMIN_WORKTREE_TARGET_UNKNOWN")
        for registered_id, path in target.worktrees:
            if registered_id == worktree_id:
                return path
        raise HostAdminTargetError("HOST_ADMIN_WORKTREE_TARGET_UNKNOWN")


def installation_root(data_root: Path) -> Path:
    return data_root.resolve()


def registry(_data_root: Path) -> HostAdminTargetRegistry:
    """Fail closed until deployment provisions explicit targets outside HTTP."""
    return HostAdminTargetRegistry()


def _audit(data_root: Path, event: str, *, target_id: object, outcome: str) -> None:
    log_event(component_logger(data_root, "ep_server"), 20, event,
              diagnostic=f"target_id={target_id!r}; outcome={outcome}")


def worktree_inventory(targets: HostAdminTargetRegistry, target_id: object) -> dict[str, object]:
    """Return registered worktrees that Git currently reports, in containment."""
    target = targets.target(target_id)
    try:
        result = subprocess.run(("git", "-C", str(target.primary_worktree), "worktree", "list", "--porcelain"),
                                check=False, capture_output=True, text=True)
    except OSError as error:
        raise HostAdminTargetError("HOST_ADMIN_WORKTREE_INVENTORY_UNAVAILABLE") from error
    if result.returncode != 0:
        raise HostAdminTargetError("HOST_ADMIN_WORKTREE_INVENTORY_UNAVAILABLE")
    observed = {line.split(" ", 1)[1] for line in result.stdout.splitlines() if line.startswith("worktree ")}
    rows = []
    for worktree_id, path in target.worktrees:
        current = _resolved(path)
        rows.append({"worktree_id": worktree_id, "registered": True,
                     "present": str(current) in observed,
                     "primary": current == target.primary_worktree, "path": str(current)})
    return {"target_id": target.target_id, "worktrees": rows}


def diagnose_git_lock(targets: HostAdminTargetRegistry, target_id: object) -> dict[str, object]:
    """Diagnose exactly the primary index lock; never accept a lock pathname."""
    target = targets.target(target_id)
    lock = target.primary_worktree / ".git" / "index.lock"
    if not lock.parent.is_dir() or not _contained(_resolved(lock.parent), target.containment_root):
        raise HostAdminTargetError("HOST_ADMIN_GIT_LOCK_AMBIGUOUS")
    if lock.is_symlink():
        raise HostAdminTargetError("HOST_ADMIN_GIT_LOCK_ESCAPE")
    active_owner = False
    if lock.exists():
        try:
            probe = subprocess.run(("lsof", "--", str(lock)), check=False,
                                   capture_output=True, text=True)
            active_owner = probe.returncode == 0 and bool(probe.stdout.strip())
        except OSError:
            # No owner probe never authorizes a mutation; repair remains
            # removed, while this bounded diagnostic remains available.
            active_owner = False
    return {"target_id": target.target_id, "lock": str(lock), "exists": lock.exists(),
            "active_owner": active_owner, "repairable": False}


def remove_worktree(data_root: Path, targets: HostAdminTargetRegistry,
                    target_id: object, worktree_id: object) -> dict[str, object]:
    """Gate legacy removal after registration/inventory checks; deletion is removed."""
    try:
        target = targets.target(target_id); worktree = targets.worktree(target_id, worktree_id)
        inventory = worktree_inventory(targets, target_id)
        row = next((item for item in inventory["worktrees"] if item["worktree_id"] == worktree_id), None)
        if not isinstance(row, dict) or not row["present"]:
            raise HostAdminTargetError("HOST_ADMIN_WORKTREE_TARGET_STALE")
        if worktree == target.primary_worktree or row["primary"]:
            raise HostAdminTargetError("HOST_ADMIN_WORKTREE_PRIMARY_PROTECTED")
        # A dirty worktree is never an administrative cleanup candidate. A
        # Git index lock is the bounded active-owner signal available without
        # process discovery; ambiguity fails closed.
        status = subprocess.run(("git", "-C", str(worktree), "status", "--porcelain"),
                                check=False, capture_output=True, text=True)
        if status.returncode != 0:
            raise HostAdminTargetError("HOST_ADMIN_WORKTREE_STATUS_UNAVAILABLE")
        if status.stdout.strip():
            raise HostAdminTargetError("HOST_ADMIN_WORKTREE_DIRTY")
        if (worktree / ".git" / "index.lock").exists():
            raise HostAdminTargetError("HOST_ADMIN_WORKTREE_ACTIVE")
        _audit(data_root, "host_admin_worktree_removal_refused", target_id=target_id, outcome="UNSUPPORTED_REMOVED")
        return {"outcome": "UNSUPPORTED_REMOVED", "worktree_id": worktree_id}
    except HostAdminTargetError as error:
        _audit(data_root, "host_admin_worktree_removal_rejected", target_id=target_id, outcome=str(error))
        raise


def repair_git_lock(data_root: Path, targets: HostAdminTargetRegistry, target_id: object) -> dict[str, object]:
    """Gate legacy repair after exact diagnosis; arbitrary lock deletion is removed."""
    try:
        diagnosis = diagnose_git_lock(targets, target_id)
        if not diagnosis["exists"]:
            raise HostAdminTargetError("HOST_ADMIN_GIT_LOCK_ABSENT")
        if diagnosis["active_owner"]:
            raise HostAdminTargetError("HOST_ADMIN_GIT_LOCK_ACTIVE")
        _audit(data_root, "host_admin_git_lock_repair_refused", target_id=target_id, outcome="UNSUPPORTED_REMOVED")
        return {"outcome": "UNSUPPORTED_REMOVED", "target_id": target_id}
    except HostAdminTargetError as error:
        _audit(data_root, "host_admin_git_lock_repair_rejected", target_id=target_id, outcome=str(error))
        raise


def diagnostics(data_root: Path) -> dict[str, object]:
    root = installation_root(data_root); usage = shutil.disk_usage(root)
    runtime = managed_codex_runtime.inspect(root)
    state = runtime.get("state") if isinstance(runtime, dict) else "UNKNOWN"
    return {"scope": "HOST_ADMIN", "root_kind": "EP_SERVER_INSTALLATION",
            "disk": {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free},
            "managed_codex_runtime": {"state": state}, "registered_targets": 0,
            "mutations_supported": False, "project_authority": False,
            "execution_authority": False, "queue_authority": False}
