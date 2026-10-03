"""Scoped, read-only producer evidence for one repository and consumer."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import subprocess

from .execution_repository import trusted_github_repository_slug
from .local_repository_binding import (
    LocalRepositoryBindingError, resolve_local_repository_binding,
)
from .merge_delegation import bound_github_repository
from .repository_attachment import RepositoryAttachmentError, load_repository_attachment


CONTRACT_VERSION = "ep-repository-consumer-authority/v1"
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class AuthorityReadbackError(ValueError):
    """A typed rejection; no rejected scope produces an authority claim."""

    def __init__(self, code: str, status: int = 403) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + sha256(encoded).hexdigest()


def _required_identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AuthorityReadbackError("AUTHORITY_SCOPE_INVALID", 400)
    return value


def _bound_push_repository(root: Path) -> str:
    """Resolve the single effective origin push target, including Git rewrites."""
    try:
        push_urls = subprocess.run(
            ["git", "remote", "get-url", "--push", "--all", "origin"],
            cwd=root, check=True, capture_output=True, text=True, timeout=10,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("bound GitHub push target is unavailable") from error
    if len(push_urls) != 1:
        raise ValueError("bound GitHub push target is ambiguous")
    repository = trusted_github_repository_slug(push_urls[0])
    if repository is None:
        raise ValueError("bound GitHub push target is invalid")
    return repository


def read_current(
    connection: sqlite3.Connection, *, data_root: Path, project_id: str,
    repository_id: str, consumer_id: str, expected_instance_id: str,
    expected_consumer_id: str, expected_github_repository: str,
    expected_revision: str | None = None,
    expected_digest: str | None = None,
) -> dict[str, object]:
    """Bind a bearer-derived principal to current CENTRAL and checkout facts.

    The caller opens one SQLite read transaction before authentication and
    calls this function on that same connection.  No credential material or
    local path is returned.  Every revision is scoped to one repository.
    """
    project_id = _required_identifier(project_id)
    repository_id = _required_identifier(repository_id)
    consumer_id = _required_identifier(consumer_id)
    if not isinstance(expected_instance_id, str) or not expected_instance_id:
        raise AuthorityReadbackError("AUTHORITY_INSTANCE_REQUIRED", 400)
    if not isinstance(expected_consumer_id, str) or not expected_consumer_id:
        raise AuthorityReadbackError("AUTHORITY_CONSUMER_REQUIRED", 400)
    if (not isinstance(expected_github_repository, str)
            or not _GITHUB_REPOSITORY.fullmatch(expected_github_repository)):
        raise AuthorityReadbackError("AUTHORITY_GITHUB_REPOSITORY_REQUIRED", 400)
    if expected_revision is not None and not _DIGEST.fullmatch(expected_revision):
        raise AuthorityReadbackError("AUTHORITY_REVISION_INVALID", 400)
    if expected_digest is not None and not _DIGEST.fullmatch(expected_digest):
        raise AuthorityReadbackError("AUTHORITY_DIGEST_INVALID", 400)

    instance = connection.execute("SELECT instance_id FROM ep_installations").fetchone()
    if instance is None or str(instance[0]) != expected_instance_id:
        raise AuthorityReadbackError("AUTHORITY_INSTANCE_MISMATCH")
    if consumer_id != expected_consumer_id:
        raise AuthorityReadbackError("AUTHORITY_CONSUMER_MISMATCH")
    project = connection.execute(
        "SELECT status,attachment_contract FROM ep_project_registrations WHERE project_id=?",
        (project_id,),
    ).fetchone()
    repository = connection.execute(
        "SELECT project_id,authority_repository_id,role,attachment_contract,created_at,updated_at "
        "FROM ep_repository_registrations WHERE repository_id=?",
        (repository_id,),
    ).fetchone()
    consumer = connection.execute(
        "SELECT status,updated_at FROM ep_consumer_registrations "
        "WHERE consumer_id=? AND project_id=?", (consumer_id, project_id),
    ).fetchone()
    grant = connection.execute(
        "SELECT status,created_at,updated_at FROM ep_parallel_action_repository_grants "
        "WHERE consumer_id=? AND project_id=? AND repository_id=?",
        (consumer_id, project_id, repository_id),
    ).fetchone()
    if project is None or str(project[0]) != "ACTIVE":
        raise AuthorityReadbackError("AUTHORITY_PROJECT_INACTIVE")
    if consumer is None or str(consumer[0]) != "ACTIVE":
        raise AuthorityReadbackError("AUTHORITY_CONSUMER_INACTIVE")
    if grant is None or str(grant[0]) != "ACTIVE":
        raise AuthorityReadbackError("AUTHORITY_GRANT_INACTIVE")
    if repository is None or str(repository[0]) != project_id:
        raise AuthorityReadbackError("AUTHORITY_REPOSITORY_UNAVAILABLE")
    authority = connection.execute(
        "SELECT project_id,role,authority_repository_id FROM ep_repository_registrations "
        "WHERE repository_id=?", (str(repository[1]),),
    ).fetchone()
    try:
        project_contract = json.loads(str(project[1]))
    except (TypeError, ValueError) as error:
        raise AuthorityReadbackError("AUTHORITY_PROJECT_TOPOLOGY_DRIFT") from error
    if (not isinstance(project_contract, dict)
            or project_contract.get("authority_repository_id") != str(repository[1])
            or not authority
            or tuple(str(value) for value in authority)
               != (project_id, "authority", str(repository[1]))):
        raise AuthorityReadbackError("AUTHORITY_PROJECT_TOPOLOGY_DRIFT")
    try:
        binding = resolve_local_repository_binding(
            connection, project_id=project_id, repository_id=repository_id,
            data_root=data_root,
        )
        attachment = load_repository_attachment(binding.local_root).agent_read_surface()
        registered = json.loads(str(repository[3]))
        github_repository = bound_github_repository(binding.local_root)
        push_repository = _bound_push_repository(binding.local_root)
    except (LocalRepositoryBindingError, RepositoryAttachmentError, ValueError,
            OSError, json.JSONDecodeError) as error:
        raise AuthorityReadbackError("AUTHORITY_BINDING_UNAVAILABLE") from error
    if registered != attachment or attachment["repository"]["role"] != str(repository[2]):
        raise AuthorityReadbackError("AUTHORITY_ATTACHMENT_DRIFT")
    if attachment["project"]["authority_repository_id"] != str(repository[1]):
        raise AuthorityReadbackError("AUTHORITY_ATTACHMENT_DRIFT")
    if project_contract.get("schema_version") != attachment["schema_version"]:
        raise AuthorityReadbackError("AUTHORITY_PROJECT_TOPOLOGY_DRIFT")
    if (github_repository != expected_github_repository
            or push_repository != expected_github_repository):
        raise AuthorityReadbackError("AUTHORITY_GITHUB_REPOSITORY_MISMATCH")

    binding_provenance = {
        "repository_created_at": str(repository[4]),
        "repository_updated_at": str(repository[5]),
        "attachment_digest": _digest(attachment),
        "local_root_digest": _digest(str(binding.local_root)),
        "local_binding_created_at": binding.created_at,
        "local_binding_updated_at": binding.updated_at,
        "grant_created_at": str(grant[1]),
        "grant_updated_at": str(grant[2]),
        "github_repository": github_repository,
    }
    revision = _digest(binding_provenance)
    readback: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "instance_id": str(instance[0]),
        "project_id": project_id,
        "project_status": "ACTIVE",
        "repository_id": repository_id,
        "repository_role": str(repository[2]),
        "authority_repository_id": str(repository[1]),
        "github_repository": github_repository,
        "local_repository_binding": "BOUND",
        "consumer_id": consumer_id,
        "consumer_status": "ACTIVE",
        "repository_grant": "ACTIVE",
        "submission_authorization": "PARALLEL_ACTION_INTAKE",
        "dispatch_authorized": False,
        "binding_revision": revision,
        "provenance": {
            "source": "EP_CENTRAL_AND_BOUND_ATTACHMENT",
            "attachment_schema_version": attachment["schema_version"],
            "attachment_digest": binding_provenance["attachment_digest"],
        },
    }
    authority_digest = _digest({
        **readback,
        "consumer_updated_at": str(consumer[1]),
    })
    if expected_revision is not None and expected_revision != revision:
        raise AuthorityReadbackError("AUTHORITY_REVISION_MISMATCH", 409)
    if expected_digest is not None and expected_digest != authority_digest:
        raise AuthorityReadbackError("AUTHORITY_DIGEST_MISMATCH", 409)
    readback["authority_digest"] = authority_digest
    return readback
