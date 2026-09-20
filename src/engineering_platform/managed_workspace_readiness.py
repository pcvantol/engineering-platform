"""Read-only, project-scoped Managed workspace capability inspection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from .execution_repository import trusted_github_repository_slug
from .providers import GitProvider


def project_readiness(connection: sqlite3.Connection, *, project_id: str,
                      repository_id: str) -> dict[str, object]:
    """Report known blockers without planning or changing the checkout."""
    observed_at = datetime.now(timezone.utc).isoformat()
    binding = connection.execute(
        "SELECT b.local_root FROM ep_local_repository_bindings b "
        "JOIN ep_repository_registrations r ON r.repository_id=b.repository_id "
        "AND r.project_id=b.project_id AND r.role='authority' "
        "WHERE b.project_id=? AND b.repository_id=? AND b.state='BOUND'",
        (project_id, repository_id),
    ).fetchone()
    response: dict[str, object] = {
        "contract_version": "1.0", "project_id": project_id,
        "repository_id": repository_id,
        "managed_workspace_id": f"{project_id}:{repository_id}",
        "execution_mode": "MANAGED", "repository_identity": None,
        "origin": None, "head_sha": None, "branch": None, "clean": None,
        "busy": None, "active_lease": None,
        "preparation_capability": "EXACT_MAIN_FAST_FORWARD_V1",
        "status": "BLOCKED", "known_blocker": "MANAGED_WORKSPACE_UNBOUND",
        "observed_at": observed_at,
    }
    if binding is None:
        return response
    root = Path(str(binding[0]))
    git = GitProvider()

    def read(*args: str) -> str | None:
        try:
            result = git.execute(root, "git", *args)
        except (OSError, RuntimeError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    head = read("rev-parse", "--verify", "HEAD")
    branch = read("branch", "--show-current")
    remote = read("remote", "get-url", "origin")
    status = read("status", "--porcelain", "--untracked-files=all")
    identity = trusted_github_repository_slug(remote) if remote else None
    active_lease = connection.execute(
        "SELECT 1 FROM execution_run_leases l JOIN ep_execution_runs r ON r.run_id=l.run_id "
        "WHERE r.project_id=? AND l.lease_state='ACTIVE' LIMIT 1",
        (project_id,),
    ).fetchone() is not None
    operation = False
    for name in ("index.lock", "MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD",
                 "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply"):
        marker = read("rev-parse", "--git-path", name)
        if marker is None:
            operation = True
            break
        path = Path(marker)
        if (path if path.is_absolute() else root / path).exists():
            operation = True
            break
    blocker = (
        "MANAGED_WORKSPACE_UNAVAILABLE" if head is None or status is None or branch is None
        or re.fullmatch(r"[0-9a-f]{40}", head) is None else
        "MANAGED_ORIGIN_UNTRUSTED" if identity is None else
        "MANAGED_BRANCH_UNEXPECTED" if branch != "main" else
        "MANAGED_WORKSPACE_DIRTY" if status else
        "MANAGED_GIT_OPERATION_ACTIVE" if operation else
        "MANAGED_LEASE_ACTIVE" if active_lease else None
    )
    response.update({
        "repository_identity": identity, "origin": identity,
        "head_sha": head if head and re.fullmatch(r"[0-9a-f]{40}", head) else None,
        "branch": branch, "clean": status == "" if status is not None else None,
        "busy": operation or active_lease, "active_lease": active_lease,
        "status": "READY" if blocker is None else "BLOCKED", "known_blocker": blocker,
    })
    return response
