"""Bounded, durable delivery reservations for the PA-E2 repository-only profile.

Every admission is checked again in the dispatcher's BEGIN IMMEDIATE claim.
An unreleased resource reservation is deliberately not expired on a timer:
after a crash, an operator must resolve the uncertain run before reuse.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3
import subprocess
from urllib.parse import urlparse

from .execution_context import additional_workspace_write_roots
from .execution_errors import RunnerError
from .parity_context import project_context


MAX_PROVIDER_INVOCATIONS = 2
_PREFIX = "pa-e2:"
_FOREVER = "9999-12-31T23:59:59+00:00"


class DeliveryScopeError(ValueError):
    """The target cannot be proven to use the repository-only profile."""


@dataclass(frozen=True)
class DeliveryGate:
    state: str
    resources: tuple[str, ...]
    resource_state: str
    capacity_state: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(root), *args), capture_output=True, text=True,
            check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeliveryScopeError("REPOSITORY_RESOURCE_UNAVAILABLE") from error
    if result.returncode != 0 or not result.stdout.strip():
        raise DeliveryScopeError("REPOSITORY_RESOURCE_UNAVAILABLE")
    return result.stdout.strip()


def _origin_identity(value: str) -> str:
    # A remote is part of the exclusion identity as well as the Git common
    # directory, so a second checkout of the same GitHub repository cannot
    # bypass the resource gate. No credential or path is persisted.
    if value.startswith("git@github.com:"):
        path = value.split(":", 1)[1]
    else:
        parsed = urlparse(value)
        if parsed.hostname != "github.com" or parsed.scheme not in {"https", "ssh"}:
            raise DeliveryScopeError("REPOSITORY_ORIGIN_UNQUALIFIED")
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/").lower()
    if len(path.split("/")) != 2 or not all(path.split("/")):
        raise DeliveryScopeError("REPOSITORY_ORIGIN_UNQUALIFIED")
    return "github.com/" + path


def repository_resources(root: Path) -> tuple[str, ...]:
    try:
        if additional_workspace_write_roots(root):
            raise DeliveryScopeError("PA_E2_SHARED_WRITE_SCOPE_UNQUALIFIED")
    except (RunnerError, OSError) as error:
        raise DeliveryScopeError("PA_E2_SHARED_WRITE_SCOPE_UNQUALIFIED") from error
    common = _git(root, "rev-parse", "--git-common-dir")
    try:
        common_path = (root / common).resolve(strict=True)
    except OSError as error:
        raise DeliveryScopeError("REPOSITORY_RESOURCE_UNAVAILABLE") from error
    fetch_urls = _git(root, "remote", "get-url", "--all", "origin").splitlines()
    push_urls = _git(root, "remote", "get-url", "--push", "--all", "origin").splitlines()
    if len(fetch_urls) != 1 or len(push_urls) != 1:
        raise DeliveryScopeError("REPOSITORY_ORIGIN_UNQUALIFIED")
    origins = {_origin_identity(fetch_urls[0]), _origin_identity(push_urls[0])}
    resources = {
        _PREFIX + "resource:git:" + sha256(str(common_path).encode()).hexdigest(),
        *(_PREFIX + "resource:origin:" + sha256(origin.encode()).hexdigest()
          for origin in origins),
    }
    return tuple(sorted(resources))


def policy_for_submission(connection: sqlite3.Connection, submission_id: str) -> str | None:
    row = connection.execute(
        """SELECT i.policy_digest FROM ep_parallel_action_intakes i
           JOIN ep_parallel_action_submission_links l ON l.intake_id=i.intake_id
          WHERE l.submission_id=?""", (submission_id,),
    ).fetchone()
    return str(row[0]) if row is not None else None


def gate(connection: sqlite3.Connection, *, data_root: Path, project_id: str,
         repository_id: str, run_id: str | None = None) -> DeliveryGate:
    context = project_context(connection, data_root=data_root, project_id=project_id,
                              repository_id=repository_id)
    if context.local_repository_root is None:
        raise DeliveryScopeError("LOCAL_BINDING_UNBOUND")
    resources = repository_resources(context.local_repository_root)
    resource_keys = resources + ("",) * (3 - len(resources))
    held = connection.execute(
        "SELECT 1 FROM ep_execution_leases WHERE lease_id IN (?,?,?) "
        "AND released_at IS NULL AND run_id!=? LIMIT 1",
        (*resource_keys, run_id or ""),
    ).fetchone()
    resource_held = held is not None
    # Legacy work has no PA-E2 reservation. Resolve its declared binding and
    # compare both physical Git storage and remote repository identity.
    legacy = connection.execute(
        """SELECT d.project_id,d.repository_id FROM ep_parity_lifecycle_dispatches d
           LEFT JOIN ep_parallel_action_submission_links l ON l.submission_id=d.submission_id
           LEFT JOIN ep_parallel_action_intakes i ON i.intake_id=l.intake_id
          WHERE (i.policy_digest IS NULL OR i.policy_digest!=?)
            AND (d.state IN ('CLAIMED','RUNNING') OR
                 (d.state IN ('BLOCKED','FAILED') AND d.operator_resolution='OPEN'))
            AND d.run_id!=?""",
        (_pa_e2_digest(), run_id or ""),
    ).fetchall()
    for other_project, other_repository in legacy:
        if str(other_project) == project_id:
            resource_held = True
            continue
        other = project_context(connection, data_root=data_root,
                                project_id=str(other_project), repository_id=str(other_repository))
        if other.local_repository_root is None:
            raise DeliveryScopeError("LEGACY_RESOURCE_UNAVAILABLE")
        if set(resources) & set(repository_resources(other.local_repository_root)):
            resource_held = True
    active_slots = connection.execute(
        "SELECT COUNT(*) FROM ep_execution_leases WHERE lease_id LIKE 'pa-e2:slot:%' "
        "AND released_at IS NULL AND run_id!=?", (run_id or "",),
    ).fetchone()[0]
    active_legacy = connection.execute(
        """SELECT COUNT(*) FROM ep_parity_lifecycle_dispatches d
           LEFT JOIN ep_parallel_action_submission_links l ON l.submission_id=d.submission_id
           LEFT JOIN ep_parallel_action_intakes i ON i.intake_id=l.intake_id
          WHERE d.state IN ('CLAIMED','RUNNING') AND d.run_id!=?
            AND (i.policy_digest IS NULL OR i.policy_digest!=?)""",
        (run_id or "", _pa_e2_digest()),
    ).fetchone()[0]
    capacity_full = active_slots + active_legacy >= MAX_PROVIDER_INVOCATIONS
    state = "WAITING_RESOURCE" if resource_held else (
        "WAITING_CAPACITY" if capacity_full else "READY"
    )
    return DeliveryGate(
        state, resources,
        "HELD" if resource_held else "AVAILABLE",
        "FULL" if capacity_full else "AVAILABLE",
    )


def _pa_e2_digest() -> str:
    # Late import avoids a cycle with intake/readback composition.
    from .parallel_action_admission import SUPPORTED_PA_E2_POLICY_DIGEST
    return SUPPORTED_PA_E2_POLICY_DIGEST


def reserve(connection: sqlite3.Connection, *, run_id: str, resources: tuple[str, ...]) -> None:
    """Reserve repository aliases and one provider slot in the claim transaction."""
    now = _now()
    for resource in resources:
        result = connection.execute(
            """INSERT INTO ep_execution_leases
               (lease_id,run_id,holder_id,acquired_at,expires_at,released_at)
               VALUES(?,?, 'pa-e2:repository',?,?,NULL)
               ON CONFLICT(lease_id) DO UPDATE SET run_id=excluded.run_id,
                  holder_id=excluded.holder_id,acquired_at=excluded.acquired_at,
                  expires_at=excluded.expires_at,released_at=NULL
               WHERE ep_execution_leases.released_at IS NOT NULL
                  OR ep_execution_leases.run_id=excluded.run_id""",
            (resource, run_id, now, _FOREVER),
        )
        if result.rowcount != 1:
            raise DeliveryScopeError("WAITING_RESOURCE")
    for index in range(MAX_PROVIDER_INVOCATIONS):
        slot = _PREFIX + f"slot:{index}"
        result = connection.execute(
            """INSERT INTO ep_execution_leases
               (lease_id,run_id,holder_id,acquired_at,expires_at,released_at)
               VALUES(?,?, 'pa-e2:provider',?,?,NULL)
               ON CONFLICT(lease_id) DO UPDATE SET run_id=excluded.run_id,
                  holder_id=excluded.holder_id,acquired_at=excluded.acquired_at,
                  expires_at=excluded.expires_at,released_at=NULL
               WHERE ep_execution_leases.released_at IS NOT NULL
                  OR ep_execution_leases.run_id=excluded.run_id""",
            (slot, run_id, now, _FOREVER),
        )
        if result.rowcount == 1:
            return
    raise DeliveryScopeError("WAITING_CAPACITY")


def verify_resources(connection: sqlite3.Connection, *, run_id: str, root: Path) -> None:
    held = {str(row[0]) for row in connection.execute(
        "SELECT lease_id FROM ep_execution_leases WHERE run_id=? "
        "AND lease_id LIKE 'pa-e2:resource:%' AND released_at IS NULL", (run_id,),
    )}
    if held != set(repository_resources(root)):
        raise DeliveryScopeError("REPOSITORY_RESOURCE_CHANGED")


def release_capacity(connection: sqlite3.Connection, run_id: str) -> None:
    connection.execute(
        "UPDATE ep_execution_leases SET released_at=? WHERE run_id=? "
        "AND lease_id LIKE 'pa-e2:slot:%' AND released_at IS NULL", (_now(), run_id),
    )


def release_resources(connection: sqlite3.Connection, run_id: str) -> None:
    connection.execute(
        "UPDATE ep_execution_leases SET released_at=? WHERE run_id=? "
        "AND lease_id LIKE 'pa-e2:resource:%' AND released_at IS NULL", (_now(), run_id),
    )
