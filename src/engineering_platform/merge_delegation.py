"""Owner-issued, revocable Mission scope for protected merge decisions.

A delegation identifier is only a lookup key. It cannot itself merge a PR.
The runner must additionally verify the accepted Forge Mission binding and
fresh PR/head/base/review/check evidence before using the existing GitHub merge
client; GitHub branch protection remains authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable

from .execution_repository import github_repository_slug

_ID = re.compile(r"[0-9a-f]{32}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_ROLES = frozenset({"IMPLEMENTATION", "FINALIZATION", "RECONCILIATION"})
AUTONOMOUS_ASSURANCE_PROFILE = "qualification-autonomous-qs@1"
AUTONOMOUS_ASSURANCE_REPOSITORY = "pcvantol/forge-mission-qualification"


@dataclass(frozen=True)
class MergeDelegation:
    delegation_id: str
    actor_reference: str
    project_id: str
    repository_id: str
    github_repository: str
    mission_id: str
    mission_revision: str
    base_branch: str
    roles: tuple[str, ...]
    expires_at: str
    activated_at: str | None
    revoked_at: str | None
    assurance_profile_id: str = ""
    assurance_profile_revision: str = ""
    assurance_policy_digest: str = ""

    def permits(self, *, project_id: str, repository_id: str, mission_id: str,
                mission_revision: str, role: str, base_branch: str,
                observed_at: datetime | None = None) -> bool:
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        now = observed_at or datetime.now(timezone.utc)
        return (
            self.revoked_at is None and self.activated_at is not None
            and expiry.tzinfo is not None and now < expiry
            and self.project_id == project_id and self.repository_id == repository_id
            and self.mission_id == mission_id and self.mission_revision == mission_revision
            and self.base_branch == base_branch == "main" and role in self.roles
        )


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_merge_delegations (
        delegation_id TEXT PRIMARY KEY,
        actor_reference TEXT NOT NULL,
        project_id TEXT NOT NULL,
        repository_id TEXT NOT NULL,
        github_repository TEXT NOT NULL,
        mission_id TEXT NOT NULL DEFAULT '',
        mission_revision TEXT NOT NULL DEFAULT '',
        base_branch TEXT NOT NULL CHECK(base_branch='main'),
        roles TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        activated_at TEXT,
        revoked_at TEXT,
        assurance_profile_id TEXT NOT NULL DEFAULT '',
        assurance_profile_revision TEXT NOT NULL DEFAULT '',
        assurance_policy_digest TEXT NOT NULL DEFAULT ''
    )""")
    install_profile_guard(connection)


def install_profile_guard(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TRIGGER IF NOT EXISTS ep_merge_delegations_profile_immutable
        BEFORE UPDATE OF assurance_profile_id, assurance_profile_revision, assurance_policy_digest ON ep_merge_delegations
        WHEN NEW.assurance_profile_id != OLD.assurance_profile_id
          OR NEW.assurance_profile_revision != OLD.assurance_profile_revision
          OR NEW.assurance_policy_digest != OLD.assurance_policy_digest
        BEGIN SELECT RAISE(ABORT, 'merge delegation assurance profile is immutable'); END""")


def bound_github_repository(root: Path) -> str:
    """Read the actual bound origin without trusting the opaque repository ID."""
    if not root.is_absolute() or not (root / ".git").exists():
        raise ValueError("bound repository checkout is unavailable")
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=root,
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("bound GitHub origin is unavailable") from error
    if not (remote.startswith("git@github.com:") or remote.startswith("https://github.com/")):
        raise ValueError("bound origin is not a GitHub repository")
    repository = github_repository_slug(remote)
    if _REPOSITORY.fullmatch(repository) is None:
        raise ValueError("bound GitHub repository identity is invalid")
    return repository


def reserve(connection: sqlite3.Connection, *, delegation_id: str, actor_reference: str,
          project_id: str, repository_id: str, github_repository: str,
          base_branch: str, roles: Iterable[str],
          expires_at: str, assurance_profile: str | None = None,
          assurance_policy_digest: str = "") -> MergeDelegation:
    selected = tuple(sorted(set(roles)))
    if (not _ID.fullmatch(delegation_id) or not actor_reference.strip()
            or not all(isinstance(value, str) and value and len(value) <= 160 for value in (
                project_id, repository_id))
            or _REPOSITORY.fullmatch(github_repository) is None
            or base_branch != "main" or not selected or not set(selected) <= _ROLES):
        raise ValueError("merge delegation scope is invalid")
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError as error:
        raise ValueError("merge delegation expiry is invalid") from error
    now = datetime.now(timezone.utc)
    if expiry.tzinfo is None or not now < expiry <= now + timedelta(days=7):
        raise ValueError("merge delegation expiry must be within seven days")
    if assurance_profile is not None and (
        assurance_profile != AUTONOMOUS_ASSURANCE_PROFILE
        or github_repository != AUTONOMOUS_ASSURANCE_REPOSITORY
    ):
        raise ValueError("autonomous assurance profile is unavailable for this repository")
    profile_id, profile_revision = ("qualification-autonomous-qs", "1") if assurance_profile else ("", "")
    if (assurance_profile and re.fullmatch(r"sha256:[0-9a-f]{64}", assurance_policy_digest) is None
            or not assurance_profile and assurance_policy_digest):
        raise ValueError("merge delegation assurance policy digest is invalid")
    import json
    connection.execute("""INSERT INTO ep_merge_delegations
        (delegation_id,actor_reference,project_id,repository_id,github_repository,mission_id,
         mission_revision,base_branch,roles,expires_at,created_at,activated_at,revoked_at,
         assurance_profile_id,assurance_profile_revision,assurance_policy_digest)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)""", (
        delegation_id, actor_reference.strip(), project_id, repository_id, github_repository, '',
        '', base_branch, json.dumps(selected), expires_at,
        datetime.now(timezone.utc).isoformat(), profile_id, profile_revision, assurance_policy_digest,
    ))
    return MergeDelegation(delegation_id, actor_reference.strip(), project_id,
                           repository_id, github_repository, '', '', base_branch,
                           selected, expires_at, None, None, profile_id, profile_revision, assurance_policy_digest)


def activate(connection: sqlite3.Connection, *, delegation_id: str,
             mission_id: str, mission_revision: str,
             actor_reference: str, github_repository: str) -> MergeDelegation:
    if not all(isinstance(value, str) and value and len(value) <= 160 for value in (mission_id, mission_revision)):
        raise ValueError("Mission activation binding is invalid")
    grant = load(connection, delegation_id)
    if (grant is None or grant.revoked_at is not None or grant.activated_at is not None
            or grant.actor_reference != actor_reference
            or grant.github_repository != github_repository):
        raise ValueError("Merge delegation is unavailable or already activated")
    expiry = datetime.fromisoformat(grant.expires_at)
    if expiry <= datetime.now(timezone.utc):
        raise ValueError("Merge delegation has expired")
    changed = connection.execute("""UPDATE ep_merge_delegations SET mission_id=?,mission_revision=?,activated_at=?
        WHERE delegation_id=? AND activated_at IS NULL AND revoked_at IS NULL""",
        (mission_id, mission_revision, datetime.now(timezone.utc).isoformat(), delegation_id)).rowcount
    if changed != 1:
        raise ValueError("Merge delegation activation raced")
    activated = load(connection, delegation_id)
    assert activated is not None
    return activated


def load(connection: sqlite3.Connection, delegation_id: str) -> MergeDelegation | None:
    if not _ID.fullmatch(delegation_id):
        return None
    row = connection.execute("""SELECT delegation_id,actor_reference,project_id,
        repository_id,github_repository,mission_id,mission_revision,base_branch,roles,expires_at,
        activated_at,revoked_at,assurance_profile_id,assurance_profile_revision,assurance_policy_digest
        FROM ep_merge_delegations WHERE delegation_id=?""",
        (delegation_id,)).fetchone()
    if row is None:
        return None
    import json
    try:
        roles = json.loads(row[8])
    except (TypeError, ValueError):
        return None
    if not isinstance(roles, list) or not roles or any(role not in _ROLES for role in roles):
        return None
    if (row[12], row[13]) not in {("", ""), ("qualification-autonomous-qs", "1")}:
        return None
    if row[12] and row[4] != AUTONOMOUS_ASSURANCE_REPOSITORY:
        return None
    if (row[12] and re.fullmatch(r"sha256:[0-9a-f]{64}", row[14]) is None
            or not row[12] and row[14] != ""):
        return None
    return MergeDelegation(*row[:8], tuple(roles), row[9], row[10], row[11], row[12], row[13], row[14])


def revoke(connection: sqlite3.Connection, delegation_id: str, *, actor_reference: str) -> bool:
    if not _ID.fullmatch(delegation_id):
        return False
    return bool(connection.execute("""UPDATE ep_merge_delegations SET revoked_at=?
        WHERE delegation_id=? AND actor_reference=? AND revoked_at IS NULL""",
        (datetime.now(timezone.utc).isoformat(), delegation_id, actor_reference)).rowcount)
