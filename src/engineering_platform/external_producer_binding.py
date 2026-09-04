"""CENTRAL-owned identity bindings for bounded Server-owned producers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
from uuid import uuid4

from .platform_admin import require_installation_owner

DEPENDABOT = "DEPENDABOT"
GITHUB_REPOSITORY = "GITHUB_REPOSITORY"
_GITHUB = re.compile(r"^[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*$")


class ProducerBindingError(ValueError):
    """Stable, fail-closed binding result."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExternalProducerBinding:
    binding_id: str
    project_id: str
    repository_id: str
    version: int


def _validate_binding_kind(producer_type: object, external_resource_type: object) -> None:
    """Keep the first registry deliberately bounded to approved producers."""
    if producer_type != DEPENDABOT or external_resource_type != GITHUB_REPOSITORY:
        raise ProducerBindingError("UNSUPPORTED_PRODUCER_BINDING")


def normalize_github_repository(value: object) -> str:
    """Normalize an external identity only; never inspect a local Git remote."""
    if not isinstance(value, str):
        raise ProducerBindingError("INVALID_EXTERNAL_IDENTITY")
    normalized = value.strip().removesuffix("/").removesuffix(".git").casefold()
    if normalized.startswith("https://github.com/"):
        normalized = normalized.removeprefix("https://github.com/")
    if normalized.startswith("git@github.com:"):
        normalized = normalized.removeprefix("git@github.com:")
    if not _GITHUB.fullmatch(normalized):
        raise ProducerBindingError("INVALID_EXTERNAL_IDENTITY")
    return normalized


def resolve(
    connection: sqlite3.Connection,
    *,
    producer_type: str,
    external_resource_type: str,
    external_resource_identity: object,
) -> ExternalProducerBinding:
    _validate_binding_kind(producer_type, external_resource_type)
    identity = normalize_github_repository(external_resource_identity)
    rows = connection.execute("""SELECT b.binding_id,b.project_id,b.repository_id,b.version,p.status,r.project_id
        FROM ep_external_producer_bindings b JOIN ep_project_registrations p ON p.project_id=b.project_id
        JOIN ep_repository_registrations r ON r.repository_id=b.repository_id
        WHERE b.producer_type=? AND b.external_resource_type=? AND b.external_resource_identity=? AND b.status='ACTIVE'""", (producer_type, external_resource_type, identity)).fetchall()
    if len(rows) != 1:
        raise ProducerBindingError("BINDING_NOT_FOUND" if not rows else "BINDING_CONFLICT")
    binding_id, project_id, repository_id, version, project_status, repository_project = rows[0]
    if project_status != "ACTIVE":
        raise ProducerBindingError("PROJECT_INACTIVE")
    if project_id != repository_project:
        raise ProducerBindingError("REPOSITORY_NOT_AUTHORIZED")
    return ExternalProducerBinding(str(binding_id), str(project_id), str(repository_id), int(version))


def register(
    connection: sqlite3.Connection,
    *,
    data_root: object,
    producer_type: str,
    external_resource_type: str,
    external_resource_identity: object,
    project_id: str,
    repository_id: str,
    reason: str,
) -> ExternalProducerBinding:
    """Register a binding through the installation-owner-only admin boundary."""
    from pathlib import Path
    actor = require_installation_owner(Path(data_root))
    _validate_binding_kind(producer_type, external_resource_type)
    if not isinstance(project_id, str) or not project_id or not isinstance(repository_id, str) or not repository_id:
        raise ProducerBindingError("INVALID_BINDING")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 512:
        raise ProducerBindingError("INVALID_BINDING")
    identity = normalize_github_repository(external_resource_identity)
    target = connection.execute(
        """SELECT p.status,r.project_id
           FROM ep_project_registrations AS p
           LEFT JOIN ep_repository_registrations AS r ON r.repository_id=?
           WHERE p.project_id=?""",
        (repository_id, project_id),
    ).fetchone()
    if target is None or target[0] != "ACTIVE" or target[1] != project_id:
        raise ProducerBindingError("REPOSITORY_NOT_AUTHORIZED")
    now, binding_id = datetime.now(timezone.utc).isoformat(), "binding-" + uuid4().hex
    try:
        connection.execute(
            """INSERT INTO ep_external_producer_bindings(
                   binding_id,producer_type,external_resource_type,external_resource_identity,
                   project_id,repository_id,status,version,created_at,created_by,updated_at,provenance
               ) VALUES(?,?,?,?,?,?, 'ACTIVE',1,?,?,?,?)""",
            (
                binding_id,
                producer_type,
                external_resource_type,
                identity,
                project_id,
                repository_id,
                now,
                actor,
                now,
                json.dumps({"reason": reason.strip()}, sort_keys=True),
            ),
        )
    except sqlite3.IntegrityError as error:
        raise ProducerBindingError("BINDING_CONFLICT") from error
    connection.execute(
        """INSERT INTO ep_external_producer_binding_audit(
               binding_id,action,actor,reason,payload,recorded_at
           ) VALUES(?,?,?,?,?,?)""",
        (
            binding_id,
            "REGISTER",
            actor,
            reason.strip(),
            json.dumps(
                {"identity": identity, "project_id": project_id, "repository_id": repository_id},
                sort_keys=True,
            ),
            now,
        ),
    )
    return ExternalProducerBinding(binding_id, project_id, repository_id, 1)


def list_bindings(connection: sqlite3.Connection, *, data_root: object) -> list[dict[str, object]]:
    """Return bounded binding metadata to the installation-owner admin only."""
    from pathlib import Path

    require_installation_owner(Path(data_root))
    rows = connection.execute(
        """SELECT binding_id,producer_type,external_resource_type,external_resource_identity,
                  project_id,repository_id,status,version,created_at,updated_at
           FROM ep_external_producer_bindings
           ORDER BY producer_type,external_resource_type,external_resource_identity"""
    ).fetchall()
    columns = (
        "binding_id",
        "producer_type",
        "external_resource_type",
        "external_resource_identity",
        "project_id",
        "repository_id",
        "status",
        "version",
        "created_at",
        "updated_at",
    )
    return [dict(zip(columns, row, strict=True)) for row in rows]


def deactivate(
    connection: sqlite3.Connection,
    *,
    data_root: object,
    binding_id: object,
    reason: str,
) -> ExternalProducerBinding:
    """Deactivate exactly one active binding; historical provenance remains intact."""
    from pathlib import Path

    actor = require_installation_owner(Path(data_root))
    if not isinstance(binding_id, str) or not binding_id or not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 512:
        raise ProducerBindingError("INVALID_BINDING")
    row = connection.execute(
        """SELECT binding_id,project_id,repository_id,version
           FROM ep_external_producer_bindings
           WHERE binding_id=? AND status='ACTIVE'""",
        (binding_id,),
    ).fetchone()
    if row is None:
        raise ProducerBindingError("BINDING_NOT_ACTIVE")
    now = datetime.now(timezone.utc).isoformat()
    next_version = int(row[3]) + 1
    connection.execute(
        """UPDATE ep_external_producer_bindings
           SET status='INACTIVE',version=?,updated_at=?
           WHERE binding_id=? AND status='ACTIVE'""",
        (next_version, now, binding_id),
    )
    connection.execute(
        """INSERT INTO ep_external_producer_binding_audit(
               binding_id,action,actor,reason,payload,recorded_at
           ) VALUES(?,?,?,?,?,?)""",
        (
            binding_id,
            "DEACTIVATE",
            actor,
            reason.strip(),
            json.dumps({"previous_version": int(row[3])}, sort_keys=True),
            now,
        ),
    )
    return ExternalProducerBinding(str(row[0]), str(row[1]), str(row[2]), next_version)
