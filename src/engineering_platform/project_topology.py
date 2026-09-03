"""Durable logical project topology and Agent checkout attachment authority.

Repository declarations are the sole source of project and repository identity.
This module never receives a checkout path and therefore cannot turn local
inventory into portable topology.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .repository_attachment import RepositoryAttachmentError, parse_repository_attachment


class TopologyRegistrationError(ValueError):
    """Stable, fail-closed attachment-registration error."""


def _error(code: str) -> None:
    raise TopologyRegistrationError(code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_repository_registrations (
        repository_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES ep_project_registrations(project_id),
        authority_repository_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('authority','child')),
        attachment_contract TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS ep_repository_registrations_project_lookup ON ep_repository_registrations(project_id,repository_id)")
    connection.execute("""CREATE TABLE IF NOT EXISTS ep_agent_repository_attachments (
        agent_id TEXT NOT NULL REFERENCES ep_agent_registrations(agent_id),
        repository_id TEXT NOT NULL REFERENCES ep_repository_registrations(repository_id),
        availability TEXT NOT NULL CHECK(availability IN ('AVAILABLE','UNAVAILABLE')),
        checkout_evidence TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(agent_id,repository_id))""")
    connection.execute("CREATE INDEX IF NOT EXISTS ep_agent_repository_attachments_repository_lookup ON ep_agent_repository_attachments(repository_id,agent_id)")


def register_attachment(connection: sqlite3.Connection, *, agent_id: str, declaration: object, availability: object) -> dict[str, str]:
    """Revalidate, reconcile, and atomically persist one Agent attachment."""
    if availability != "AVAILABLE":
        _error("INVALID_ATTACHMENT_AVAILABILITY")
    try:
        attachment = parse_repository_attachment(declaration)
    except RepositoryAttachmentError as error:
        _error("MALFORMED_REPOSITORY_DECLARATION")
        raise AssertionError from error
    surface = attachment.agent_read_surface()
    canonical = json.dumps(surface, sort_keys=True, separators=(",", ":"))
    now = _now()
    existing_project = connection.execute("SELECT attachment_contract FROM ep_project_registrations WHERE project_id=?", (attachment.project_id,)).fetchone()
    if existing_project is not None:
        try:
            old = json.loads(str(existing_project[0]))
            if old.get("authority_repository_id") != attachment.authority_repository_id:
                _error("PROJECT_DECLARATION_CONFLICT")
        except (json.JSONDecodeError, AttributeError):
            _error("PROJECT_DECLARATION_CONFLICT")
    existing_repository = connection.execute("SELECT project_id,authority_repository_id,role,attachment_contract FROM ep_repository_registrations WHERE repository_id=?", (attachment.repository_id,)).fetchone()
    if existing_repository is not None:
        if tuple(existing_repository[:3]) != (attachment.project_id, attachment.authority_repository_id, attachment.repository_role):
            _error("REPOSITORY_IDENTITY_CONFLICT")
        if str(existing_repository[3]) != canonical:
            _error("REPOSITORY_DECLARATION_CONFLICT")
    authority = connection.execute("SELECT project_id,role FROM ep_repository_registrations WHERE repository_id=?", (attachment.authority_repository_id,)).fetchone()
    if authority is not None and tuple(authority) != (attachment.project_id, "authority"):
        _error("AUTHORITY_REPOSITORY_CONFLICT")
    if attachment.repository_role == "authority" and attachment.repository_id != attachment.authority_repository_id:
        _error("AUTHORITY_REPOSITORY_MISMATCH")
    project_contract = json.dumps({"schema_version": surface["schema_version"], "authority_repository_id": attachment.authority_repository_id}, sort_keys=True, separators=(",", ":"))
    connection.execute("""INSERT INTO ep_project_registrations(project_id,attachment_contract,status,created_at,updated_at)
        VALUES(?,?, 'ACTIVE', ?, ?) ON CONFLICT(project_id) DO UPDATE SET updated_at=excluded.updated_at""", (attachment.project_id, project_contract, now, now))
    connection.execute("""INSERT INTO ep_repository_registrations(repository_id,project_id,authority_repository_id,role,attachment_contract,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(repository_id) DO UPDATE SET updated_at=excluded.updated_at""", (attachment.repository_id, attachment.project_id, attachment.authority_repository_id, attachment.repository_role, canonical, now, now))
    connection.execute("""INSERT INTO ep_agent_repository_attachments(agent_id,repository_id,availability,checkout_evidence,created_at,updated_at)
        VALUES(?,?, 'AVAILABLE','declared-checkout',?,?) ON CONFLICT(agent_id,repository_id) DO UPDATE SET availability='AVAILABLE',checkout_evidence='declared-checkout',updated_at=excluded.updated_at""", (agent_id, attachment.repository_id, now, now))
    return {"project_id": attachment.project_id, "repository_id": attachment.repository_id, "availability": "AVAILABLE", "result": "REGISTERED"}


def register_server_local_topology(connection: sqlite3.Connection, *, declaration: object) -> dict[str, str]:
    """Register explicit logical Phase-P topology without an Agent attachment."""
    try:
        attachment = parse_repository_attachment(declaration)
    except RepositoryAttachmentError as error:
        _error("MALFORMED_REPOSITORY_DECLARATION")
        raise AssertionError from error
    surface = attachment.agent_read_surface()
    canonical = json.dumps(surface, sort_keys=True, separators=(",", ":"))
    now = _now()
    existing = connection.execute("SELECT project_id,authority_repository_id,role,attachment_contract FROM ep_repository_registrations WHERE repository_id=?", (attachment.repository_id,)).fetchone()
    if existing is not None and tuple(existing[:3]) != (attachment.project_id, attachment.authority_repository_id, attachment.repository_role):
        _error("REPOSITORY_IDENTITY_CONFLICT")
    if existing is not None and str(existing[3]) != canonical:
        _error("REPOSITORY_DECLARATION_CONFLICT")
    if attachment.repository_role == "authority" and attachment.repository_id != attachment.authority_repository_id:
        _error("AUTHORITY_REPOSITORY_MISMATCH")
    contract = json.dumps({"schema_version": surface["schema_version"], "authority_repository_id": attachment.authority_repository_id}, sort_keys=True, separators=(",", ":"))
    connection.execute("INSERT INTO ep_project_registrations(project_id,attachment_contract,status,created_at,updated_at) VALUES(?,?, 'ACTIVE', ?, ?) ON CONFLICT(project_id) DO UPDATE SET updated_at=excluded.updated_at", (attachment.project_id, contract, now, now))
    connection.execute("INSERT INTO ep_repository_registrations(repository_id,project_id,authority_repository_id,role,attachment_contract,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(repository_id) DO UPDATE SET updated_at=excluded.updated_at", (attachment.repository_id, attachment.project_id, attachment.authority_repository_id, attachment.repository_role, canonical, now, now))
    return {"project_id": attachment.project_id, "repository_id": attachment.repository_id, "result": "REGISTERED"}


def topology(connection: sqlite3.Connection) -> dict[str, object]:
    """Bounded, secret-free registration diagnostic."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    projects: dict[str, dict[str, Any]] = {}
    for project_id, authority_id in connection.execute("SELECT project_id,json_extract(attachment_contract, '$.authority_repository_id') FROM ep_project_registrations ORDER BY project_id"):
        projects[str(project_id)] = {"project_id": str(project_id), "authority_repository_id": str(authority_id), "repositories": []}
    rows = connection.execute("""SELECT r.project_id,r.repository_id,r.role,a.agent_id,a.availability,a.updated_at,g.state,g.last_seen_at
        FROM ep_repository_registrations r LEFT JOIN ep_agent_repository_attachments a ON a.repository_id=r.repository_id
        LEFT JOIN ep_agent_registrations g ON g.agent_id=a.agent_id ORDER BY r.project_id,r.repository_id,a.agent_id""")
    repository_entries: dict[tuple[str, str], dict[str, Any]] = {}
    for project_id, repository_id, role, agent_id, availability, updated_at, state, last_seen in rows:
        key = (str(project_id), str(repository_id))
        entry = repository_entries.setdefault(key, {"repository_id": key[1], "role": str(role), "attachments": []})
        if agent_id is not None:
            available = availability == "AVAILABLE" and state != "REVOKED" and last_seen is not None and str(last_seen) >= cutoff
            entry["attachments"].append({"agent_id": str(agent_id), "availability": "AVAILABLE" if available else "UNAVAILABLE", "last_seen_at": str(updated_at)})
    for (project_id, _), repository in repository_entries.items():
        projects[project_id]["repositories"].append(repository)
    return {"projects": list(projects.values())}
